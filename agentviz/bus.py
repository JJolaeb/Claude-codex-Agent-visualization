"""File-based message bus that lets agents (and the user) talk to each other.

Layout under ``$AGENTVIZ_HOME`` (default ``~/.agentviz``)::

    bus/messages.jsonl        append-only log, one JSON message per line
    bus/agents/<name>.json    presence records written by connected agents
    bus/cursors/<name>.json   per-agent read offset into messages.jsonl
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass

from .agents import classify

PRESENCE_TTL = 120.0  # seconds without heartbeat before an agent counts as gone
BACKLOG = 3600.0  # a newly connected agent still receives messages sent this recently
BACKLOG_BYTES = 1024 * 1024
BROADCAST = ("*", "all", "everyone")


def home() -> str:
    return os.environ.get("AGENTVIZ_HOME") or os.path.join(os.path.expanduser("~"), ".agentviz")


def bus_dir() -> str:
    return os.path.join(home(), "bus")


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.@-]", "_", name)[:80] or "agent"


@dataclass
class Message:
    id: str
    ts: float
    sender: str
    to: str
    text: str
    reply_to: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Message | None":
        try:
            return cls(str(d["id"]), float(d["ts"]), str(d["sender"]), str(d["to"]),
                       str(d["text"]), str(d.get("reply_to") or ""))
        except (KeyError, TypeError, ValueError):
            return None

    def addressed_to(self, name: str, kind: str = "") -> bool:
        to = self.to.lower()
        if self.sender == name:
            return False
        me = name.lower()
        # "codex" reaches every codex agent: match the agent type or the part before "@".
        return (to in BROADCAST or to == me or to == me.split("@")[0]
                or (bool(kind) and to == kind.lower()))


class Bus:
    def __init__(self, root: str | None = None) -> None:
        self.root = root or bus_dir()
        self.log = os.path.join(self.root, "messages.jsonl")
        self.agents_dir = os.path.join(self.root, "agents")
        self.cursors_dir = os.path.join(self.root, "cursors")
        for d in (self.root, self.agents_dir, self.cursors_dir):
            os.makedirs(d, exist_ok=True)

    # ------------------------------------------------------------------ messages

    def send(self, sender: str, to: str, text: str, reply_to: str = "") -> Message:
        text = str(text).strip()
        if not text:
            raise ValueError("message text is empty")
        if len(text) > 20000:
            raise ValueError("message too long (max 20000 characters)")
        msg = Message(id=f"{int(time.time() * 1000):x}-{secrets.token_hex(3)}", ts=time.time(),
                      sender=sender, to=(to or "*").strip(), text=text, reply_to=reply_to or "")
        line = json.dumps(asdict(msg), ensure_ascii=False) + "\n"
        # One write() on an O_APPEND file keeps concurrent writers from interleaving lines.
        fd = os.open(self.log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        return msg

    def read_from(self, offset: int) -> tuple:
        """Return (messages, new_offset) for complete lines after byte offset."""
        try:
            size = os.path.getsize(self.log)
        except OSError:
            return [], 0
        if size < offset:  # log was truncated or replaced
            offset = 0
        if size == offset:
            return [], offset
        with open(self.log, "rb") as f:
            f.seek(offset)
            data = f.read(size - offset)
        end = data.rfind(b"\n")
        if end < 0:
            return [], offset
        out = []
        for line in data[: end + 1].splitlines():
            try:
                m = Message.from_dict(json.loads(line))
            except ValueError:
                m = None
            if m:
                out.append(m)
        return out, offset + end + 1

    def recent(self, limit: int = 50, max_bytes: int = 256 * 1024) -> list:
        try:
            size = os.path.getsize(self.log)
        except OSError:
            return []
        start = max(0, size - max_bytes)
        msgs, _ = self.read_from(start)
        if start:  # first line may be partial
            msgs = msgs[1:]
        return msgs[-limit:]

    # ------------------------------------------------------------------ cursors

    def _cursor_path(self, name: str) -> str:
        return os.path.join(self.cursors_dir, _safe(name) + ".json")

    def _get_cursor(self, name: str) -> int | None:
        try:
            with open(self._cursor_path(name), encoding="utf-8") as f:
                return int(json.load(f)["offset"])
        except Exception:
            return None

    def _set_cursor(self, name: str, offset: int) -> None:
        tmp = self._cursor_path(name) + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"offset": offset}, f)
        os.replace(tmp, self._cursor_path(name))

    def unread(self, name: str, kind: str = "", mark: bool = True) -> list:
        cursor = self._get_cursor(name)
        if cursor is None:
            # First read for this name: deliver only the recent backlog, not the whole history.
            try:
                size = os.path.getsize(self.log)
            except OSError:
                size = 0
            start = max(0, size - BACKLOG_BYTES)
            msgs, offset = self.read_from(start)
            if start:
                msgs = msgs[1:]
            cutoff = time.time() - BACKLOG
            msgs = [m for m in msgs if m.ts >= cutoff]
        else:
            msgs, offset = self.read_from(cursor)
        if mark:
            self._set_cursor(name, offset)
        return [m for m in msgs if m.addressed_to(name, kind)]

    def wait(self, name: str, kind: str = "", timeout: float = 0.0, poll: float = 0.5) -> list:
        end = time.monotonic() + max(0.0, timeout)
        while True:
            msgs = self.unread(name, kind)
            if msgs or time.monotonic() >= end:
                return msgs
            time.sleep(poll)

    # ------------------------------------------------------------------ presence

    def announce(self, name: str, **info) -> None:
        rec = dict(info, name=name, heartbeat=time.time())
        path = os.path.join(self.agents_dir, _safe(name) + ".json")
        tmp = path + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
        os.replace(tmp, path)

    def retire(self, name: str) -> None:
        try:
            os.remove(os.path.join(self.agents_dir, _safe(name) + ".json"))
        except OSError:
            pass

    def agents(self, include_stale: bool = False) -> list:
        out, now = [], time.time()
        for fn in sorted(os.listdir(self.agents_dir)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.agents_dir, fn), encoding="utf-8") as f:
                    rec = json.load(f)
            except Exception:
                continue
            alive = now - float(rec.get("heartbeat") or 0) < PRESENCE_TTL
            if alive or include_stale:
                rec["alive"] = alive
                out.append(rec)
        return out


# ---------------------------------------------------------------------- identity

def detect_identity(procs: dict | None = None, start_pid: int | None = None) -> dict:
    """Find the agent process that launched us by walking up the process tree.

    Returns {"kind", "agent_pid", "cwd", "name"}; kind is "" when no agent ancestor exists.
    """
    from .procs import make_backend

    backend = make_backend()
    if procs is None:
        try:
            procs = backend.list()
        except Exception:
            procs = {}
    pid = start_pid if start_pid is not None else os.getppid()
    seen = set()
    while pid in procs and pid not in seen and pid > 0:
        seen.add(pid)
        p = procs[pid]
        k = classify(p.name, p.exe, p.cmdline)
        if k:
            cwd = backend.cwd(pid) or os.getcwd()
            base = os.path.basename(cwd.rstrip("/\\")) or "root"
            return {"kind": k.key, "agent_pid": pid, "cwd": cwd, "name": f"{k.key}@{_safe(base)}"}
        pid = p.ppid
    cwd = os.getcwd()
    base = os.path.basename(cwd.rstrip("/\\")) or "root"
    return {"kind": "", "agent_pid": None, "cwd": cwd, "name": f"agent@{_safe(base)}"}


def unique_name(bus: Bus, wanted: str, agent_pid: int | None) -> str:
    """Avoid colliding with a different live agent that already uses `wanted`."""
    taken = {a["name"]: a.get("agent_pid") for a in bus.agents()}
    if wanted not in taken or taken[wanted] == agent_pid:
        return wanted
    i = 2
    while f"{wanted}#{i}" in taken and taken[f"{wanted}#{i}"] != agent_pid:
        i += 1
    return f"{wanted}#{i}"


def format_messages(msgs: list) -> str:
    lines = []
    for m in msgs:
        t = time.strftime("%H:%M:%S", time.localtime(m.ts))
        to = "everyone" if m.to.lower() in BROADCAST else m.to
        lines.append(f"[{t}] {m.sender} -> {to} (id {m.id}): {m.text}")
    return "\n".join(lines)

