"""Combine process samples and session transcripts into per-agent views."""

from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass, field

from .agents import AgentKind, KIND_BY_KEY, background_reason, classify, is_electron_helper
from .sessions import Event, Session
from .util import norm_path

HISTORY = 60
WORKING_SECS = 8.0  # transcript written this recently => agent is busy
CPU_BUSY = 20.0


@dataclass
class AgentView:
    kind: AgentKind
    pid: int | None = None
    cwd: str | None = None
    cpu: float | None = None
    rss: int | None = None
    started: float | None = None
    nprocs: int = 0
    session: Session | None = None
    subagents: list = field(default_factory=list)
    status: str = "IDLE"
    history: deque = field(default_factory=lambda: deque(maxlen=HISTORY))

    @property
    def last_activity(self) -> float | None:
        times = [s.mtime for s in ([self.session] if self.session else []) + self.subagents]
        return max(times) if times else None

    @property
    def active_subagents(self) -> int:
        now = time.time()
        return sum(1 for s in self.subagents if now - s.mtime < 60)

    def to_dict(self) -> dict:
        s = self.session
        last = s.last_event if s else None
        return {
            "agent": self.kind.key, "label": self.kind.label, "pid": self.pid, "cwd": self.cwd,
            "status": self.status, "cpu_percent": self.cpu, "rss": self.rss, "started": self.started,
            "processes": self.nprocs,
            "session": None if not s else {
                "id": s.session_id, "title": s.title, "model": s.model, "branch": s.branch,
                "path": s.path, "updated": s.mtime, "tokens_in": s.tokens_in,
                "tokens_out": s.tokens_out, "context": s.context,
                "last_event": None if not last else {"ts": last.ts, "kind": last.kind, "text": last.text},
            },
            "subagents": len(self.subagents), "active_subagents": self.active_subagents,
        }


def derive_status(view: AgentView, now: float) -> str:
    last = view.last_activity
    age = (now - last) if last else None
    ev = view.session.last_event if view.session else None
    if ev and ev.kind == "tool" and age is not None and age < 600:
        return "TOOL"
    if (age is not None and age < WORKING_SECS) or (view.cpu or 0) >= CPU_BUSY:
        return "WORKING"
    if view.pid is None and (age is None or age > 120):
        return "ENDED"
    if ev and ev.kind == "text":
        return "WAITING"
    return "IDLE"


class Builder:
    """Keeps per-agent CPU history across refreshes."""

    def __init__(self, window_minutes: float = 15.0, show_all: bool = False) -> None:
        self.show_all = show_all
        self.hidden: list = []  # (pid, kind, reason) of agent-like processes not shown
        self.histories: dict = {}
        self.window = window_minutes * 60
        self.own_pid = os.getpid()

    def build(self, procs: dict, sampler, sessions: list, pid_sessions: dict | None = None) -> list:
        now = time.time()
        pid_sessions = pid_sessions or {}

        # Electron/Chromium apps (e.g. the Claude or Codex desktop apps) are GUI processes:
        # their helpers carry --type=..., which also identifies the main process as their parent.
        electron = set()
        for pid, p in procs.items():
            if is_electron_helper(p.cmdline):
                electron.update((pid, p.ppid))

        self.hidden = []
        kinds = {}
        for pid, p in procs.items():
            if pid == self.own_pid:
                continue
            k = classify(p.name, p.exe, p.cmdline)
            if not k:
                continue
            if pid in electron and not self.show_all:
                self.hidden.append((pid, k, "desktop app (Electron)"))
                continue
            kinds[pid] = k

        # Fold agent processes whose ancestor is the same agent into that ancestor.
        roots = []
        for pid, k in kinds.items():
            cur, seen, is_root = procs[pid].ppid, set(), True
            while cur in procs and cur not in seen and cur > 1:
                seen.add(cur)
                if kinds.get(cur) is k:
                    is_root = False
                    break
                cur = procs[cur].ppid
            if is_root:
                roots.append(pid)

        children: dict = {}
        for pid, p in procs.items():
            children.setdefault(p.ppid, []).append(pid)

        def subtree(root: int) -> list:
            out, stack = {}, [root]
            while stack:
                pid = stack.pop()
                if pid in out:
                    continue
                out[pid] = None
                stack.extend(children.get(pid, []))
            return list(out)

        main = [s for s in sessions if not s.is_subagent]
        by_id = {s.session_id: s for s in main}
        subs: dict = {}
        for s in sessions:
            if s.is_subagent:
                subs.setdefault(s.parent_id, []).append(s)
        used = set()

        views = []
        for pid in sorted(roots, key=lambda r: procs[r].create_time or 0):
            p, k = procs[pid], kinds[pid]
            tree = subtree(pid)
            cpus = [procs[t].cpu_percent for t in tree if procs[t].cpu_percent is not None]
            v = AgentView(
                kind=k, pid=pid, cwd=sampler.cwd(p) if sampler else None,
                cpu=sum(cpus) if cpus else None,
                rss=sum(procs[t].rss or 0 for t in tree) or None,
                started=p.create_time, nprocs=len(tree),
            )
            sess = by_id.get(pid_sessions.get(pid, ""))
            if sess is None or sess.agent != k.key:
                cwd = norm_path(v.cwd)
                cands = [s for s in main if s.agent == k.key and s.path not in used
                         and (cwd is None or norm_path(s.cwd) == cwd)
                         and s.mtime >= (p.create_time or 0) - 5]
                sess = max(cands, key=lambda s: s.mtime, default=None)
            if sess is not None:
                used.add(sess.path)
                v.session = sess
                v.subagents = subs.get(sess.session_id, [])
            reason = background_reason(p.cmdline)
            if reason and sess is None and not self.show_all:
                self.hidden.append((pid, k, reason))
                continue
            views.append(v)

        # Transcripts with no matching live process (e.g. agent in a container / other host).
        for s in main:
            if s.path in used or now - s.mtime > self.window:
                continue
            k = KIND_BY_KEY.get(s.agent)
            if k is None:
                continue
            views.append(AgentView(kind=k, cwd=s.cwd, session=s, subagents=subs.get(s.session_id, [])))

        live = set()
        for v in views:
            key = ("pid", v.pid, v.started) if v.pid else ("log", v.session.path)
            live.add(key)
            hist = self.histories.setdefault(key, deque(maxlen=HISTORY))
            hist.append(v.cpu or 0.0)
            v.history = hist
            v.status = derive_status(v, now)
        self.histories = {k: h for k, h in self.histories.items() if k in live}

        order = {"TOOL": 0, "WORKING": 0, "WAITING": 1, "IDLE": 2, "ENDED": 3}
        views.sort(key=lambda v: (v.pid is None, order.get(v.status, 9), -(v.last_activity or 0)))
        return views


def is_finished(view: AgentView) -> bool:
    """A card whose agent process is gone (only its transcript is left)."""
    return view.pid is None


class Dismissed:
    """Finished sessions the user cleared from the dashboard.

    A cleared session reappears if its transcript is written to again (e.g. resumed).
    """

    def __init__(self) -> None:
        self.items: dict = {}  # transcript path -> mtime when cleared

    def clear_finished(self, views: list) -> int:
        n = 0
        for v in views:
            if is_finished(v) and v.session is not None:
                for s in [v.session] + list(v.subagents):
                    self.items[s.path] = s.mtime
                n += 1
        return n

    def restore(self) -> int:
        n = len(self.items)
        self.items.clear()
        return n

    def hidden(self, session) -> bool:
        cleared = self.items.get(session.path)
        return cleared is not None and session.mtime <= cleared

    def apply(self, views: list, sessions: list) -> tuple:
        views = [v for v in views if not (v.session is not None and is_finished(v) and self.hidden(v.session))]
        sessions = [s for s in sessions if not self.hidden(s)]
        return views, sessions


def feed(sessions: list, limit: int = 50) -> list:
    """Most recent events across all sessions, newest first."""
    events: list = []
    for s in sessions:
        events.extend(s.events)
    events.sort(key=lambda e: e.ts, reverse=True)
    return events[:limit]


def session_label(sessions: list) -> dict:
    """Map transcript path -> short display label (title or working-dir basename)."""
    out = {}
    for s in sessions:
        name = s.title or (os.path.basename(s.cwd.rstrip("/\\")) if s.cwd else "") or s.session_id[:8]
        out[s.path] = ("↳ " if s.is_subagent else "") + name
    return out


__all__ = ["AgentView", "Builder", "Event", "derive_status", "feed", "session_label"]
