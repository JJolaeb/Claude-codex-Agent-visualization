"""Incremental readers for agent session transcripts (Claude Code, Codex)."""

from __future__ import annotations

import glob
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field

from .util import clean, parse_ts

MAX_INITIAL_READ = 16 * 1024 * 1024  # bytes read the first time a transcript is opened
MAX_EVENTS = 40


@dataclass
class Event:
    ts: float
    kind: str  # user | text | tool | result | error | think
    text: str
    agent: str = ""
    session: str = ""  # transcript path


@dataclass
class Session:
    agent: str
    path: str
    session_id: str = ""
    cwd: str | None = None
    title: str = ""
    model: str = ""
    branch: str = ""
    is_subagent: bool = False
    parent_id: str = ""
    mtime: float = 0.0
    last_ts: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    context: int = 0  # prompt size of the latest model call
    pending_tools: int = 0
    events: deque = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))

    @property
    def last_event(self) -> Event | None:
        return self.events[-1] if self.events else None

    def add(self, ts: float | None, kind: str, text: str) -> None:
        text = clean(text)
        if not text:
            return
        ts = ts or self.last_ts or self.mtime or time.time()
        self.last_ts = max(self.last_ts, ts)
        self.events.append(Event(ts, kind, text, self.agent, self.path))


def _first_str(d: dict) -> str:
    for v in d.values():
        if isinstance(v, str) and v:
            return v
    return ""


def summarize_tool(name: str, inp: object) -> str:
    if not isinstance(inp, dict):
        return name
    for key in ("command", "cmd"):
        v = inp.get(key)
        if isinstance(v, list):
            v = v[-1] if len(v) >= 3 and v[1] in ("-lc", "-c") else " ".join(map(str, v))
        if isinstance(v, str) and v:
            return f"{name}: {v}"
    for key in ("file_path", "path", "notebook_path"):
        if isinstance(inp.get(key), str):
            return f"{name}: {inp[key]}"
    for key in ("pattern", "url", "query", "description", "prompt", "skill"):
        if isinstance(inp.get(key), str):
            return f"{name}: {inp[key]}"
    if isinstance(inp.get("todos"), list):
        return f"{name}: {len(inp['todos'])} todos"
    s = _first_str(inp)
    return f"{name}: {s}" if s else name


def _is_meta(text: str) -> bool:
    t = text.lstrip()
    return t.startswith("<") or t.startswith("Caveat:") or not t


# --------------------------------------------------------------------------- Claude Code

def parse_claude(sess: Session, obj: dict, usage_by_msg: dict) -> None:
    t = obj.get("type")
    ts = parse_ts(obj.get("timestamp"))
    if obj.get("cwd"):
        sess.cwd = obj["cwd"]
    if obj.get("sessionId") and not sess.session_id:
        sess.session_id = obj["sessionId"]
    if obj.get("gitBranch"):
        sess.branch = obj["gitBranch"]
    if t in ("ai-title", "summary", "custom-title"):
        sess.title = clean(obj.get("aiTitle") or obj.get("customTitle") or obj.get("summary") or sess.title)
        return
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return
    content = msg.get("content")
    if t == "assistant":
        if msg.get("model") and not str(msg["model"]).startswith("<"):
            sess.model = msg["model"]
        usage = msg.get("usage")
        if isinstance(usage, dict):
            ctx = sum(int(usage.get(k) or 0) for k in
                      ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            usage_by_msg[msg.get("id") or id(obj)] = (ctx, int(usage.get("output_tokens") or 0))
            sess.tokens_in = sum(u[0] for u in usage_by_msg.values())
            sess.tokens_out = sum(u[1] for u in usage_by_msg.values())
            sess.context = ctx
        for item in content if isinstance(content, list) else []:
            it = item.get("type")
            if it == "tool_use":
                sess.pending_tools += 1
                sess.add(ts, "tool", summarize_tool(item.get("name", "tool"), item.get("input")))
            elif it == "text":
                sess.add(ts, "text", item.get("text", ""))
            elif it in ("thinking", "redacted_thinking"):
                sess.add(ts, "think", "thinking…")
    elif t == "user":
        if isinstance(content, str):
            if not _is_meta(content):
                sess.add(ts, "user", content)
            return
        for item in content if isinstance(content, list) else []:
            it = item.get("type")
            if it == "tool_result":
                sess.pending_tools = max(0, sess.pending_tools - 1)
                body = item.get("content")
                if isinstance(body, list):
                    body = " ".join(c.get("text", "") for c in body if isinstance(c, dict))
                sess.add(ts, "error" if item.get("is_error") else "result", body or "(done)")
            elif it == "text" and not _is_meta(item.get("text", "")):
                sess.add(ts, "user", item["text"])


# --------------------------------------------------------------------------- Codex

def _codex_text(content: object) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for c in content if isinstance(content, list) else []:
        if isinstance(c, dict) and isinstance(c.get("text"), str):
            parts.append(c["text"])
    return " ".join(parts)


def parse_codex(sess: Session, obj: dict, _state: dict) -> None:
    ts = parse_ts(obj.get("timestamp"))
    t = obj.get("type")
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else None
    if payload is None:  # legacy format: items are top level, first line is metadata
        if "instructions" in obj and "id" in obj and "type" not in obj:
            sess.session_id = sess.session_id or str(obj["id"])
            return
        t, payload = "response_item", obj
    pt = payload.get("type")
    if t == "session_meta":
        sess.session_id = str(payload.get("id") or sess.session_id)
        sess.cwd = payload.get("cwd") or sess.cwd
        git = payload.get("git")
        if isinstance(git, dict) and git.get("branch"):
            sess.branch = git["branch"]
    elif t == "turn_context":
        sess.cwd = payload.get("cwd") or sess.cwd
        if payload.get("model"):
            sess.model = payload["model"]
    elif t == "event_msg" and pt == "token_count":
        info = payload.get("info")
        if isinstance(info, dict):
            total = info.get("total_token_usage") or {}
            last = info.get("last_token_usage") or {}
            sess.tokens_in = int(total.get("input_tokens") or 0)
            sess.tokens_out = int(total.get("output_tokens") or 0)
            sess.context = int(last.get("input_tokens") or sess.context)
    elif t == "response_item":
        if pt == "message":
            text = _codex_text(payload.get("content"))
            if payload.get("role") == "assistant":
                sess.add(ts, "text", text)
            elif payload.get("role") == "user" and not _is_meta(text):
                sess.add(ts, "user", text)
        elif pt in ("function_call", "custom_tool_call"):
            sess.pending_tools += 1
            raw = payload.get("arguments", payload.get("input"))
            args = raw
            if isinstance(raw, str):
                try:
                    args = json.loads(raw)
                except ValueError:
                    args = {"input": raw.splitlines()[0] if raw else ""}
            sess.add(ts, "tool", summarize_tool(payload.get("name", "tool"), args))
        elif pt == "local_shell_call":
            sess.pending_tools += 1
            action = payload.get("action") or {}
            sess.add(ts, "tool", summarize_tool("shell", action))
        elif pt in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
            sess.pending_tools = max(0, sess.pending_tools - 1)
            out = payload.get("output")
            if isinstance(out, str):
                try:
                    out = json.loads(out)
                except ValueError:
                    pass
            if isinstance(out, dict):
                code = (out.get("metadata") or {}).get("exit_code")
                text = out.get("output") or out.get("content") or ""
                sess.add(ts, "error" if code not in (None, 0) else "result", text or "(done)")
            else:
                sess.add(ts, "result", str(out or "(done)"))
        elif pt == "reasoning":
            summary = payload.get("summary")
            text = _codex_text(summary) if summary else ""
            sess.add(ts, "think", text or "thinking…")


# --------------------------------------------------------------------------- tracker

@dataclass
class _FileState:
    session: Session
    offset: int = 0
    buf: bytes = b""
    parser_state: dict = field(default_factory=dict)
    size: int = 0


def claude_home() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def codex_home() -> str:
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


SOURCES = (
    ("claude", lambda: os.path.join(claude_home(), "projects", "*", "*.jsonl"), parse_claude),
    ("claude", lambda: os.path.join(claude_home(), "projects", "*", "*", "subagents", "*.jsonl"), parse_claude),
    ("codex", lambda: os.path.join(codex_home(), "sessions", "*", "*", "*", "*.jsonl"), parse_codex),
)


class SessionTracker:
    """Discovers recently modified transcripts and tails them incrementally."""

    def __init__(self, window_minutes: float = 30.0, rescan_every: float = 3.0) -> None:
        self.window = window_minutes * 60
        self.rescan_every = rescan_every
        self.files: dict = {}
        self._parsers: dict = {}
        self._last_scan = 0.0

    def _scan(self, now: float) -> None:
        cutoff = now - self.window
        seen = set()
        for agent, pattern, parser in SOURCES:
            for path in glob.glob(pattern()):
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                if mtime < cutoff:
                    continue
                seen.add(path)
                if path not in self.files:
                    sub = os.sep + "subagents" + os.sep in path
                    sess = Session(agent=agent, path=path, is_subagent=sub, mtime=mtime)
                    if sub:
                        sess.parent_id = os.path.basename(os.path.dirname(os.path.dirname(path)))
                    self.files[path] = _FileState(sess)
                    self._parsers[path] = parser
        for path in list(self.files):
            if path not in seen:
                del self.files[path]
                self._parsers.pop(path, None)

    def _read(self, path: str, st: _FileState) -> None:
        try:
            size = os.path.getsize(path)
            st.session.mtime = os.path.getmtime(path)
        except OSError:
            return
        if size < st.offset:  # truncated / rewritten
            st.offset, st.buf = 0, b""
            st.session = Session(agent=st.session.agent, path=path, is_subagent=st.session.is_subagent,
                                 parent_id=st.session.parent_id, mtime=st.session.mtime)
            st.parser_state = {}
        if size == st.offset:
            return
        skip_partial = False
        if st.offset == 0 and size > MAX_INITIAL_READ:
            st.offset, skip_partial = size - MAX_INITIAL_READ, True
        try:
            with open(path, "rb") as f:
                f.seek(st.offset)
                data = f.read(size - st.offset)
        except OSError:
            return
        st.offset += len(data)
        data = st.buf + data
        lines = data.split(b"\n")
        st.buf = lines.pop()  # incomplete trailing line
        if skip_partial and lines:
            lines.pop(0)
        parser = self._parsers[path]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                try:
                    parser(st.session, obj, st.parser_state)
                except Exception:
                    continue
        if not st.session.session_id:
            st.session.session_id = os.path.splitext(os.path.basename(path))[0]

    def update(self) -> list:
        now = time.time()
        if now - self._last_scan >= self.rescan_every:
            self._scan(now)
            self._last_scan = now
        for path, st in list(self.files.items()):
            self._read(path, st)
        return [st.session for st in self.files.values()]


def claude_pid_sessions() -> dict:
    """Map live Claude Code pids to session ids via ~/.claude/sessions/<pid>.json."""
    out = {}
    for path in glob.glob(os.path.join(claude_home(), "sessions", "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            out[int(data["pid"])] = str(data["sessionId"])
        except Exception:
            continue
    return out
