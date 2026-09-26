"""Relay: wake idle agents when other agents message them.

An interactive agent that has finished its turn does not notice new bus messages.
The relay watches the bus and, when an idle Claude Code / Codex agent has unread
messages, resumes that agent's session headlessly (``claude -p --resume`` /
``codex exec resume``) with a fixed prompt telling it to read and answer them.

Safety:
- the prompt is a constant; message text never goes on a command line (it is read
  by the agent through the check_messages tool), so messages cannot inject shell syntax
- at most one headless run per agent, each with a timeout
- wake limits per agent and in total per hour; hitting one pauses the relay and tells the user
- each message wakes an agent at most once
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field

from .bus import Bus

WAKE_PROMPT = ("[agentviz relay] Other agents sent you messages while you were idle. "
               "Call the agentviz check_messages tool now, handle what they ask if it is appropriate, "
               "and reply with send_message. If nothing needs a reply, stop.")

SUPPORTED = ("claude", "codex")


@dataclass
class RelayConfig:
    idle_secs: float = 30.0  # agent's transcript untouched this long => idle
    grace_secs: float = 10.0  # give the agent a chance to read a message itself first
    max_wakes_per_agent: int = 6  # per window
    max_wakes_total: int = 12  # per window
    window_secs: float = 3600.0
    run_timeout: float = 600.0
    claude_args: list = field(default_factory=list)
    codex_args: list = field(default_factory=list)


@dataclass
class Run:
    name: str
    kind: str
    proc: object
    started: float
    log_path: str
    message_ids: tuple


def build_command(kind: str, exe: str, session_id: str, config: RelayConfig) -> list:
    if kind == "claude":
        # --fork-session keeps the interactive window's transcript untouched.
        # --allowedTools takes several values, so "--" marks where the prompt starts.
        return [exe, "-p", "--resume", session_id, "--fork-session",
                "--allowedTools", "mcp__agentviz", *config.claude_args, "--", WAKE_PROMPT]
    if kind == "codex":
        return [exe, "exec", "--skip-git-repo-check", *config.codex_args, "resume", session_id, "--", WAKE_PROMPT]
    raise ValueError(kind)


def _kill_tree(proc) -> None:
    try:
        import psutil  # type: ignore
        parent = psutil.Process(proc.pid)
        for child in parent.children(recursive=True):
            child.kill()
        parent.kill()
        return
    except Exception:
        pass
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


class Relay:
    def __init__(self, bus: Bus | None = None, config: RelayConfig | None = None, popen=None, which=None) -> None:
        self.bus = bus or Bus()
        self.config = config or RelayConfig()
        self._popen = popen or subprocess.Popen
        self._which = which or shutil.which
        self.enabled = True
        self.paused_reason = ""
        self.runs: dict = {}
        self.wakes: deque = deque()  # (ts, name)
        self.woken_ids: set = set()
        self.events: deque = deque(maxlen=100)  # (seq, ts, text)
        self._seq = 0
        self.log_dir = os.path.join(self.bus.root, "relay-logs")
        os.makedirs(self.log_dir, exist_ok=True)

    # ------------------------------------------------------------------ control

    def status(self) -> str:
        if not self.enabled:
            return "relay off"
        if self.paused_reason:
            return "relay paused"
        return f"relay on · {len(self.runs)} running" if self.runs else "relay on"

    def _event(self, text: str) -> None:
        self._seq += 1
        self.events.append((self._seq, time.time(), text))

    def stop(self) -> None:
        """Kill switch: stop all headless runs and stop waking agents."""
        for run in list(self.runs.values()):
            _kill_tree(run.proc)
            self._event(f"stopped {run.name}")
        self._reap()
        self.enabled = False
        self._event("relay turned off")

    def start(self) -> None:
        self.enabled = True
        self.paused_reason = ""
        self.wakes.clear()
        self._event("relay turned on")

    def toggle(self) -> str:
        if self.enabled and not self.paused_reason:
            self.stop()
        else:
            self.start()
        return self.status()

    # ------------------------------------------------------------------ loop

    def _reap(self) -> None:
        now = time.time()
        for name, run in list(self.runs.items()):
            code = run.proc.poll()
            if code is None and now - run.started > self.config.run_timeout:
                _kill_tree(run.proc)
                self._event(f"{name}: timed out after {self.config.run_timeout:.0f}s")
                code = run.proc.poll()
                if code is None:
                    continue
            if code is None:
                continue
            self.bus.unclaim(run.proc.pid)
            if code == 0:
                self._event(f"{name}: finished")
            else:
                self._event(f"{name}: exited with code {code} (log: {run.log_path})")
            del self.runs[name]

    def _allowed(self, name: str, now: float) -> bool:
        while self.wakes and now - self.wakes[0][0] > self.config.window_secs:
            self.wakes.popleft()
        total = len(self.wakes)
        mine = sum(1 for _, n in self.wakes if n == name)
        if mine >= self.config.max_wakes_per_agent:
            reason = f"{name} was woken {mine} times within the hour"
        elif total >= self.config.max_wakes_total:
            reason = f"{total} wake-ups within the hour"
        else:
            return True
        self.paused_reason = reason
        self._event(f"paused: {reason}")
        try:
            self.bus.send("relay", "user", f"Relay paused to avoid an endless loop: {reason}. "
                                           "Press x in the dashboard (twice) or restart the relay to resume.")
        except (OSError, ValueError):
            pass
        return False

    def tick(self, views: list) -> None:
        """Check for idle agents with unread messages and wake them. Call periodically."""
        self._reap()
        if not self.enabled or self.paused_reason:
            return
        now = time.time()
        by_pid = {v.pid: v for v in views if getattr(v, "pid", None)}
        for rec in self.bus.agents():
            name, kind = rec["name"], rec.get("kind") or ""
            if kind not in SUPPORTED or name in self.runs:
                continue
            view = by_pid.get(rec.get("agent_pid"))
            if view is None or view.session is None or not view.session.session_id:
                continue
            if view.status in ("WORKING", "TOOL"):
                continue
            last = view.last_activity or 0
            if now - last < self.config.idle_secs:
                continue
            pending = [m for m in self.bus.unread(name, kind, mark=False) if m.id not in self.woken_ids]
            if not pending or now - min(m.ts for m in pending) < self.config.grace_secs:
                continue
            if not self._allowed(name, now):
                return
            self._launch(name, kind, view, pending)

    def _launch(self, name: str, kind: str, view, pending: list) -> None:
        exe = self._which(kind)
        if not exe:
            self._event(f"{name}: '{kind}' executable not found on PATH")
            self.woken_ids.update(m.id for m in pending)
            return
        argv = build_command(kind, exe, view.session.session_id, self.config)
        cwd = view.cwd or view.session.cwd or None
        if cwd and not os.path.isdir(cwd):
            cwd = None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        log_path = os.path.join(self.log_dir, f"{name.replace('@', '_').replace('#', '_')}-{stamp}.log")
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            with open(log_path, "wb") as log:  # the child keeps its own handle
                proc = self._popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=subprocess.STDOUT, **kwargs)
        except OSError as e:
            self._event(f"{name}: failed to start {kind}: {e}")
            self.woken_ids.update(m.id for m in pending)
            return
        self.bus.claim(proc.pid, name)
        ids = tuple(m.id for m in pending)
        self.woken_ids.update(ids)
        self.wakes.append((time.time(), name))
        self.runs[name] = Run(name, kind, proc, time.time(), log_path, ids)
        senders = ", ".join(sorted({m.sender for m in pending}))
        self._event(f"woke {name} for {len(pending)} message(s) from {senders}")


def config_from_args(args) -> RelayConfig:
    return RelayConfig(
        idle_secs=args.relay_idle, max_wakes_total=args.relay_max,
        max_wakes_per_agent=max(1, min(args.relay_max, args.relay_max_per_agent)),
        run_timeout=args.relay_timeout,
        claude_args=shlex.split(args.claude_args or "", posix=os.name != "nt"),
        codex_args=shlex.split(args.codex_args or "", posix=os.name != "nt"),
    )


def run_forever(collect, relay: Relay, interval: float = 2.0) -> int:
    """`agentviz relay`: headless loop that prints relay events."""
    print(f"agentviz relay running (max {relay.config.max_wakes_total} wake-ups/hour, "
          f"idle after {relay.config.idle_secs:.0f}s). Ctrl+C to stop.", flush=True)
    seen = 0
    try:
        while True:
            relay.tick(collect()[0])
            for seq, ts, text in list(relay.events):
                if seq > seen:
                    print(time.strftime("%H:%M:%S", time.localtime(ts)), text, flush=True)
                    seen = seq
            time.sleep(interval)
    except KeyboardInterrupt:
        relay.stop()
        print("relay stopped", file=sys.stderr)
    return 0
