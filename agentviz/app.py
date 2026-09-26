"""Command-line entry point and main loop."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

from . import __version__
from .model import Builder, Dismissed, feed, session_label
from .procs import Sampler
from .render import LEGACY_CONSOLE_HELP, VT_LEGACY, VT_OK, Theme, frame, windows_vt_status
from .sessions import SessionTracker, claude_pid_sessions
from .bus import Bus


class KeyReader:
    """Non-blocking single-key input for Windows (msvcrt) and POSIX terminals."""

    def __init__(self) -> None:
        self.old = None
        self.fd = None

    def __enter__(self):
        if os.name != "nt" and sys.stdin.isatty():
            import termios
            import tty
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc) -> None:
        if self.old is not None:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def get(self, timeout: float) -> str | None:
        if os.name == "nt":
            import msvcrt
            end = time.monotonic() + timeout
            while True:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("\x00", "\xe0"):  # arrow/function key prefix
                        msvcrt.getwch()
                        return None
                    return ch
                if time.monotonic() >= end:
                    return None
                time.sleep(0.03)
        if self.fd is None:
            time.sleep(timeout)
            return None
        import select
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        b = os.read(self.fd, 1)
        if b == b"\x1b":
            # Swallow the rest of an escape sequence (arrow keys etc.); a lone Esc is returned.
            rest = b""
            while select.select([self.fd], [], [], 0.01)[0]:
                rest += os.read(self.fd, 16)
            return None if rest else "\x1b"
        if b and b[0] >= 0xC0:  # multi-byte UTF-8 character (e.g. Hangul)
            need = 2 if b[0] < 0xE0 else 3 if b[0] < 0xF0 else 4
            while len(b) < need and select.select([self.fd], [], [], 0.05)[0]:
                b += os.read(self.fd, 1)
        return b.decode(errors="ignore") or None


class Collector:
    def __init__(self, window: float, show_all: bool = False) -> None:
        self.sampler = Sampler()
        self.tracker = SessionTracker(window_minutes=window)
        self.builder = Builder(window_minutes=window, show_all=show_all)
        self.bus = None
        try:
            self.bus = Bus()
        except OSError:
            pass

    def collect(self) -> tuple:
        procs = self.sampler.sample()
        sessions = self.tracker.update()
        views = self.builder.build(procs, self.sampler, sessions, claude_pid_sessions())
        messages, targets = [], []
        if self.bus is not None:
            try:
                online = self.bus.agents()
                by_pid = {a.get("agent_pid"): a["name"] for a in online if a.get("agent_pid")}
                for v in views:
                    v.bus_name = by_pid.get(v.pid, "")
                targets = [a["name"] for a in online]
                messages = self.bus.recent(30)
            except OSError:
                pass
        return views, sessions, messages, targets

    def send(self, to: str, text: str) -> str:
        if self.bus is None:
            return "message bus unavailable"
        self.bus.send("user", to, text)
        return f"sent to {to}"

    @property
    def backend(self) -> str:
        return self.sampler.backend.name


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="agentviz",
        description="Visualize running coding agents (Claude Code, Codex, Gemini CLI, …) in the terminal "
                    "and let them message each other.")
    p.add_argument("-i", "--interval", type=float, default=1.0, help="data refresh interval in seconds (default 1)")
    p.add_argument("-w", "--window", type=float, default=15.0,
                   help="show transcripts updated within this many minutes (default 15)")
    p.add_argument("--once", action="store_true", help="print a single snapshot and exit")
    p.add_argument("--json", action="store_true", help="print a JSON snapshot and exit")
    p.add_argument("--demo", action="store_true", help="show simulated agents")
    p.add_argument("--no-color", action="store_true", help="disable colors (also honours NO_COLOR)")
    p.add_argument("--ascii", action="store_true", help="use ASCII characters only")
    p.add_argument("--no-feed", action="store_true", help="hide the activity feed")
    p.add_argument("--all", action="store_true",
                   help="also show desktop apps and idle background servers (IDE extensions)")
    p.add_argument("-V", "--version", action="version", version=f"agentviz {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    m = sub.add_parser("mcp", help="run the MCP server that connects an agent to the message bus")
    m.add_argument("--name", help="name on the bus (default: <agent>@<project dir>)")
    sub.add_parser("hook", help="Claude Code hook: deliver unread messages into the conversation")
    s = sub.add_parser("send", help="send a message to agents as 'user'")
    s.add_argument("to", help="agent name, agent type (claude, codex), or * for everyone")
    s.add_argument("text", nargs="+", help="message text")
    i = sub.add_parser("messages", help="print the message log")
    i.add_argument("-n", type=int, default=20, help="number of messages (default 20)")
    i.add_argument("-f", "--follow", action="store_true", help="keep printing new messages")
    sub.add_parser("agents", help="list agents connected to the message bus")
    sub.add_parser("doctor", help="list every process detected as an agent and why it is shown or hidden")
    sub.add_parser("setup", help="show how to connect Claude Code and Codex to the message bus")
    return p.parse_args(argv)


def _setup_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def parse_compose(text: str) -> tuple:
    """'@name hello' -> ('name', 'hello'); anything else is broadcast."""
    text = text.strip()
    if text.startswith("@"):
        head, _, rest = text[1:].partition(" ")
        if head and rest.strip():
            return head, rest.strip()
    return "*", text


def run_doctor() -> int:
    """Print every agent-like process with its command line and whether the dashboard shows it."""
    from .agents import classify

    collector = Collector(15.0)
    procs = collector.sampler.sample()
    views = collector.builder.build(procs, collector.sampler, collector.tracker.update(), claude_pid_sessions())
    shown = {v.pid for v in views if v.pid}
    hidden = {pid: reason for pid, _k, reason in collector.builder.hidden}
    print(f"process backend: {collector.backend}\n")
    found = False
    for pid, p in sorted(procs.items()):
        k = classify(p.name, p.exe, p.cmdline)
        if not k or pid == os.getpid():
            continue
        found = True
        if pid in shown:
            state = "SHOWN"
        elif pid in hidden:
            state = f"HIDDEN: {hidden[pid]}"
        else:
            state = "part of another agent's process tree"
        print(f"[{k.label}] pid {pid} (parent {p.ppid}) - {state}")
        print(f"    exe : {p.exe or '-'}")
        print(f"    cmd : {' '.join(p.cmdline)[:300] or '-'}")
        print(f"    cwd : {collector.sampler.cwd(p) or '-'}")
    if not found:
        print("No agent-like processes found.")
    print("\nIf something is detected wrongly, please share this output. Use --all to show hidden ones.")
    return 0


def run_command(args) -> int:
    from .bus import format_messages

    if args.command == "mcp":
        from .mcp_server import main as mcp_main
        return mcp_main(args.name)
    if args.command == "hook":
        from .hook import run as hook_run
        return hook_run()
    if args.command == "doctor":
        return run_doctor()
    if args.command == "setup":
        from .connect import print_setup
        print_setup()
        return 0
    bus = Bus()
    if args.command == "send":
        msg = bus.send("user", args.to, " ".join(args.text))
        print(f"sent {msg.id} to {msg.to}")
        return 0
    if args.command == "agents":
        online = bus.agents()
        if not online:
            print("No agents connected. Run `agentviz setup` to see how to connect them.")
        for a in online:
            print(f"{a['name']:<28} {a.get('kind') or 'agent':<8} pid {a.get('agent_pid') or '-':<8} {a.get('cwd') or ''}")
        return 0
    if args.command == "messages":
        msgs = bus.recent(args.n)
        if msgs:
            print(format_messages(msgs))
        if args.follow:
            offset = os.path.getsize(bus.log) if os.path.exists(bus.log) else 0
            try:
                while True:
                    new, offset = bus.read_from(offset)
                    if new:
                        print(format_messages(new), flush=True)
                    time.sleep(0.5)
            except KeyboardInterrupt:
                pass
        return 0
    return 1


def run(argv=None) -> int:
    args = parse_args(argv)
    _setup_stdout()
    if args.command:
        return run_command(args)
    vt = windows_vt_status()
    legacy = vt == VT_LEGACY
    color = not args.no_color and "NO_COLOR" not in os.environ and vt == VT_OK and sys.stdout.isatty()
    # Legacy consoles usually use raster fonts without box-drawing/Hangul-width support.
    theme = Theme(color=color, ascii_only=args.ascii or legacy)
    interval = max(0.2, args.interval)

    if args.demo:
        from .demo import Demo
        source = Demo()
        backend = "demo"
    else:
        source = Collector(args.window, show_all=args.all)
        backend = source.backend
    collect = source.collect

    dismissed = Dismissed()

    def draw(state, size, **kw):
        views, sessions, messages, targets = state
        views, sessions = dismissed.apply(views, sessions)
        return frame(views, feed(sessions), session_label(sessions), size, kw.pop("tick", 0), theme,
                     backend=backend, messages=messages, targets=targets, **kw)

    if args.json or args.once:
        collect()
        time.sleep(0.5)  # second sample so CPU% can be computed
        state = collect()
        if args.json:
            print(json.dumps([v.to_dict() for v in state[0]], ensure_ascii=False, indent=2))
            return 0
        size = shutil.get_terminal_size((100, 40))
        rows = max(12, 7 * max(1, len(state[0])) + 14 + min(6, len(state[2])))
        lines = draw(state, (size.columns, rows), interval=interval, show_feed=not args.no_feed)
        body = lines[:-1]  # drop the key-help footer
        while body and not body[-1].strip():
            body.pop()
        print("\n".join(body))
        return 0

    if not sys.stdout.isatty():
        print("agentviz: stdout is not a terminal; use --once or --json", file=sys.stderr)
        return 2
    if legacy:
        print(LEGACY_CONSOLE_HELP, file=sys.stderr)
        return 2

    out = sys.stdout
    out.write("\x1b[?1049h\x1b[?25l")  # alternate screen, hide cursor
    out.flush()
    paused, show_feed, tick = False, not args.no_feed, 0
    compose, notice, notice_until = None, "", 0.0
    state = collect()
    last = time.monotonic()
    try:
        with KeyReader() as keys:
            while True:
                size = shutil.get_terminal_size((100, 30))
                if notice and time.monotonic() > notice_until:
                    notice = ""
                lines = draw(state, (size.columns, size.lines), tick=tick, paused=paused, interval=interval,
                             show_feed=show_feed, compose=compose, notice=notice)
                # Trailing padding is redundant with erase-to-end-of-line (ESC[K); dropping it keeps
                # output small and avoids stray wrapping in consoles that miscount escape codes.
                out.write("\x1b[H" + "\x1b[K\n".join(l.rstrip(" ") for l in lines) + "\x1b[K\x1b[J")
                out.flush()
                key = keys.get(0.12)
                tick += 1
                if key and compose is not None:
                    if key in ("\r", "\n"):
                        if compose.strip():
                            to, text = parse_compose(compose)
                            try:
                                notice = source.send(to, text)
                            except (OSError, ValueError) as e:
                                notice = f"send failed: {e}"
                            notice_until = time.monotonic() + 4
                            last = 0.0
                        compose = None
                    elif key == "\x1b":
                        compose = None
                    elif key in ("\x7f", "\x08"):
                        compose = compose[:-1]
                    elif key == "\x03":
                        break
                    elif key.isprintable():
                        compose += key
                elif key:
                    k = key.lower()
                    if k in ("q", "\x1b", "\x03"):
                        break
                    if k == "p":
                        paused = not paused
                    elif k in ("+", "="):
                        interval = max(0.2, round(interval / 2, 2))
                    elif k in ("-", "_"):
                        interval = min(30.0, interval * 2)
                    elif k == "f":
                        show_feed = not show_feed
                    elif k == "c":
                        n = dismissed.clear_finished(dismissed.apply(state[0], state[1])[0])
                        notice = f"cleared {n} finished" if n else "nothing finished to clear"
                        notice_until = time.monotonic() + 4
                    elif k == "u":
                        n = dismissed.restore()
                        notice = f"restored {n}" if n else "nothing to restore"
                        notice_until = time.monotonic() + 4
                    elif k == "m":
                        compose = ""
                    elif k == "r":
                        last = 0.0
                if not paused and time.monotonic() - last >= interval:
                    state = collect()
                    last = time.monotonic()
    except KeyboardInterrupt:
        pass
    finally:
        out.write("\x1b[0m\x1b[?25h\x1b[?1049l")
        out.flush()
    return 0


def main() -> None:
    sys.exit(run())
