"""Command-line entry point and main loop."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

from . import __version__
from .model import Builder, feed, session_label
from .procs import Sampler
from .render import Theme, enable_windows_vt, frame
from .sessions import SessionTracker, claude_pid_sessions


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
        if r:
            return os.read(self.fd, 1).decode(errors="ignore")
        return None


class Collector:
    def __init__(self, window: float) -> None:
        self.sampler = Sampler()
        self.tracker = SessionTracker(window_minutes=window)
        self.builder = Builder(window_minutes=window)

    def collect(self) -> tuple:
        procs = self.sampler.sample()
        sessions = self.tracker.update()
        views = self.builder.build(procs, self.sampler, sessions, claude_pid_sessions())
        return views, sessions

    @property
    def backend(self) -> str:
        return self.sampler.backend.name


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="agentviz",
        description="Visualize running coding agents (Claude Code, Codex, Gemini CLI, …) in the terminal.")
    p.add_argument("-i", "--interval", type=float, default=1.0, help="data refresh interval in seconds (default 1)")
    p.add_argument("-w", "--window", type=float, default=15.0,
                   help="show transcripts updated within this many minutes (default 15)")
    p.add_argument("--once", action="store_true", help="print a single snapshot and exit")
    p.add_argument("--json", action="store_true", help="print a JSON snapshot and exit")
    p.add_argument("--demo", action="store_true", help="show simulated agents")
    p.add_argument("--no-color", action="store_true", help="disable colors (also honours NO_COLOR)")
    p.add_argument("--ascii", action="store_true", help="use ASCII characters only")
    p.add_argument("--no-feed", action="store_true", help="hide the activity feed")
    p.add_argument("-V", "--version", action="version", version=f"agentviz {__version__}")
    return p.parse_args(argv)


def _setup_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def run(argv=None) -> int:
    args = parse_args(argv)
    _setup_stdout()
    vt_ok = enable_windows_vt()
    color = not args.no_color and "NO_COLOR" not in os.environ and vt_ok and sys.stdout.isatty()
    theme = Theme(color=color, ascii_only=args.ascii)
    interval = max(0.2, args.interval)

    if args.demo:
        from .demo import Demo
        source = Demo()
        collect, backend = source.update, "demo"
    else:
        collector = Collector(args.window)
        collect, backend = collector.collect, collector.backend

    if args.json or args.once:
        collect()
        time.sleep(0.5)  # second sample so CPU% can be computed
        views, sessions = collect()
        if args.json:
            print(json.dumps([v.to_dict() for v in views], ensure_ascii=False, indent=2))
            return 0
        size = shutil.get_terminal_size((100, 40))
        rows = max(12, 7 * max(1, len(views)) + 14)
        lines = frame(views, feed(sessions), session_label(sessions), (size.columns, rows), 0, theme,
                      interval=interval, show_feed=not args.no_feed, backend=backend)
        body = lines[:-1]  # drop the key-help footer
        while body and not body[-1].strip():
            body.pop()
        print("\n".join(body))
        return 0

    if not sys.stdout.isatty():
        print("agentviz: stdout is not a terminal; use --once or --json", file=sys.stderr)
        return 2

    out = sys.stdout
    out.write("\x1b[?1049h\x1b[?25l")  # alternate screen, hide cursor
    out.flush()
    paused, show_feed, tick = False, not args.no_feed, 0
    views, sessions = collect()
    last = time.monotonic()
    try:
        with KeyReader() as keys:
            while True:
                size = shutil.get_terminal_size((100, 30))
                lines = frame(views, feed(sessions), session_label(sessions), (size.columns, size.lines),
                              tick, theme, paused=paused, interval=interval, show_feed=show_feed,
                              backend=backend)
                out.write("\x1b[H" + "\x1b[K\n".join(lines) + "\x1b[K\x1b[J")
                out.flush()
                key = keys.get(0.12)
                tick += 1
                if key:
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
                    elif k == "r":
                        last = 0.0
                if not paused and time.monotonic() - last >= interval:
                    views, sessions = collect()
                    last = time.monotonic()
    except KeyboardInterrupt:
        pass
    finally:
        out.write("\x1b[0m\x1b[?25h\x1b[?1049l")
        out.flush()
    return 0


def main() -> None:
    sys.exit(run())
