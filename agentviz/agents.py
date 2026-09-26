"""Known coding-agent CLIs and how to recognise their processes."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentKind:
    key: str
    label: str
    color: int  # xterm-256 color index
    names: tuple = ()  # executable / script basenames (lowercase, no extension)
    name_prefixes: tuple = ()  # e.g. "codex-" for codex-x86_64-unknown-linux-musl
    packages: tuple = ()  # substrings that identify the agent anywhere in the command line
    excludes: tuple = field(default=())  # substrings that disqualify a match (desktop apps etc.)


# Substrings that mark GUI/desktop builds rather than the terminal agents.
_DESKTOP = (".app/Contents/", "AnthropicClaude", "Claude Helper", "\\Programs\\claude\\", "Codex.app")

KINDS: tuple = (
    AgentKind("claude", "Claude Code", 209, names=("claude",),
              packages=("@anthropic-ai/claude-code",), excludes=_DESKTOP),
    AgentKind("codex", "Codex", 42, names=("codex",), name_prefixes=("codex-",),
              packages=("@openai/codex",), excludes=_DESKTOP),
    AgentKind("gemini", "Gemini CLI", 75, names=("gemini",), packages=("@google/gemini-cli",)),
    AgentKind("copilot", "Copilot CLI", 141, packages=("@github/copilot",)),
    AgentKind("cursor", "Cursor Agent", 252, names=("cursor-agent",)),
    AgentKind("opencode", "OpenCode", 220, names=("opencode",), packages=("opencode-ai",)),
    AgentKind("aider", "Aider", 114, names=("aider",), packages=("aider-chat",)),
    AgentKind("qwen", "Qwen Code", 177, packages=("@qwen-code/qwen-code",)),
    AgentKind("amp", "Amp", 203, packages=("@sourcegraph/amp",)),
)

KIND_BY_KEY = {k.key: k for k in KINDS}

INTERPRETERS = ("node", "nodejs", "bun", "deno", "python", "python3", "pypy3", "uv", "npx")


def _base(path: str) -> str:
    b = os.path.basename(path.replace("\\", "/")).lower()
    for ext in (".exe", ".cmd", ".bat", ".js", ".mjs", ".cjs", ".py", ".ps1"):
        if b.endswith(ext):
            b = b[: -len(ext)]
            break
    return b


def _is_interpreter(base: str) -> bool:
    return base in INTERPRETERS or (base.startswith("python") and base[6:].replace(".", "").isdigit())


def _name_matches(kind: AgentKind, base: str) -> bool:
    return base in kind.names or any(base.startswith(p) for p in kind.name_prefixes)


def classify(name: str, exe: str | None, cmdline: list | None) -> AgentKind | None:
    """Return the agent kind a process belongs to, or None."""
    cmdline = [str(a) for a in (cmdline or []) if a is not None]
    joined = " ".join([exe or ""] + cmdline)
    bases = {_base(name or "")}
    if exe:
        bases.add(_base(exe))
    if cmdline:
        bases.add(_base(cmdline[0]))
    interp = any(_is_interpreter(b) for b in bases)
    # For `node /path/to/claude ...` style launches, look at the first script arguments.
    script_bases = set()
    if interp:
        for arg in cmdline[1:4]:
            if not arg.startswith("-"):
                script_bases.add(_base(arg))
                break

    for kind in KINDS:
        if any(x in joined for x in kind.excludes):
            continue
        if any(_name_matches(kind, b) for b in bases if not _is_interpreter(b)):
            return kind
        if kind.packages and any(p in joined for p in kind.packages):
            return kind
        if any(_name_matches(kind, b) for b in script_bases):
            return kind
    return None
