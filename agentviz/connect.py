"""`agentviz setup`: print the commands that connect agents to the message bus."""

from __future__ import annotations

import json
import os
import shlex
import sys
import sysconfig


def base_command() -> tuple:
    """(argv list, env dict) that launches agentviz from any directory."""
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    site_dirs = {os.path.abspath(p) for p in (sysconfig.get_paths().get("purelib"),
                                               sysconfig.get_paths().get("platlib")) if p}
    env = {} if pkg_parent in site_dirs else {"PYTHONPATH": pkg_parent}
    return [sys.executable, "-m", "agentviz"], env


def _quote(args: list) -> str:
    if os.name == "nt":
        return " ".join(f'"{a}"' if " " in a else a for a in args)
    return " ".join(shlex.quote(a) for a in args)


def hook_settings(cmd: list, env: dict) -> dict:
    prefix = "".join(f"{k}={shlex.quote(v)} " for k, v in env.items()) if os.name != "nt" else ""
    if os.name == "nt" and env:
        prefix = "".join(f"set {k}={v}&& " for k, v in env.items())
    entry = [{"type": "command", "command": prefix + _quote(cmd + ["hook"]), "timeout": 10}]
    return {"hooks": {
        "PostToolUse": [{"matcher": "*", "hooks": entry}],
        "UserPromptSubmit": [{"hooks": entry}],
        "Stop": [{"hooks": entry}],
    }}


def print_setup() -> None:
    cmd, env = base_command()
    mcp = _quote(cmd + ["mcp"])
    claude_env = "".join(f"-e {k}={_quote([v])} " for k, v in env.items())
    codex_env = "".join(f"--env {k}={_quote([v])} " for k, v in env.items())
    codex_toml = (f'[mcp_servers.agentviz]\ncommand = {json.dumps(cmd[0])}\n'
                  f'args = {json.dumps(cmd[1:] + ["mcp"])}\n')
    if env:
        codex_toml += "env = { " + ", ".join(f"{k} = {json.dumps(v)}" for k, v in env.items()) + " }\n"

    print(f"""agentviz message bus — connect your agents
==========================================

1) Claude Code (MCP server, all projects):

   claude mcp add --scope user {claude_env}agentviz -- {mcp}

2) Codex (MCP server):

   codex mcp add {codex_env}agentviz -- {mcp}

   or add to ~/.codex/config.toml:

{_indent(codex_toml, 3)}
3) Optional, Claude Code only: push messages into the conversation automatically.
   Merge this into ~/.claude/settings.json ("hooks" section):

{_indent(json.dumps(hook_settings(cmd, env), indent=2), 3)}

   With the hook, Claude sees new messages after every tool call and, when it is
   about to stop, is asked to read and answer them first. Without it (and in Codex)
   agents read messages when they call the check_messages tool.

4) Restart the agents, then watch and talk to them:

   agentviz                     dashboard (press m to send a message)
   agentviz send '*' "hello"    message everyone as 'user'
   agentviz messages -f         follow the conversation
   agentviz agents              who is connected

Tip: ask an agent e.g. "codex에게 이 API 리뷰를 부탁하고 답을 기다려줘" — it will use
send_message and check_messages(wait_seconds=...) on its own.
""")


def _indent(text: str, n: int) -> str:
    return "\n".join((" " * n + line) if line else line for line in text.splitlines()) + "\n"
