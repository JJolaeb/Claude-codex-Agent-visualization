"""Claude Code hook that pushes unread bus messages into the running conversation.

Configured via ~/.claude/settings.json (see `agentviz setup`). On PostToolUse /
UserPromptSubmit / SessionStart the messages are added as context; on Stop the
hook blocks the stop so Claude reads and answers them before going idle.
"""

from __future__ import annotations

import json
import sys

from .bus import Bus, format_messages
from .procs import ancestor_pids

CONTEXT_EVENTS = ("PostToolUse", "UserPromptSubmit", "SessionStart")


def find_registration(bus: Bus) -> dict | None:
    """The bus record of the agentviz MCP server that belongs to the same agent as this hook."""
    chain = ancestor_pids()
    records = [a for a in bus.agents() if a.get("agent_pid") in chain]
    if not records:
        return None
    return min(records, key=lambda a: chain.index(a["agent_pid"]))


def run(stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
        payload = json.loads(stdin.read() or "{}")
    except ValueError:
        payload = {}
    event = payload.get("hook_event_name") or ""
    try:
        bus = Bus()
        me = find_registration(bus)
        if me is None:
            return 0  # agentviz MCP server isn't connected for this agent
        msgs = bus.unread(me["name"], me.get("kind") or "")
    except Exception:
        return 0  # never break the agent because of the bus
    if not msgs:
        return 0
    text = (f"New messages for you ('{me['name']}') on the agentviz agent bus. They come from other "
            "agents or the user; reply with the send_message tool if appropriate.\n" + format_messages(msgs))
    if event == "Stop":
        out = {"decision": "block", "reason": text}
    elif event in CONTEXT_EVENTS:
        out = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    else:
        out = {"systemMessage": text}
    stdout.write(json.dumps(out, ensure_ascii=False))
    stdout.flush()
    return 0
