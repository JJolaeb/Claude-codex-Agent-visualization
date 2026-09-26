"""Text-width, formatting and time helpers."""

from __future__ import annotations

import os
import re
import time
import unicodedata
from datetime import datetime

_CTRL_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x1f\x7f-\x9f]")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def clean(text: object) -> str:
    """Collapse whitespace and strip control/escape sequences from untrusted text."""
    s = str(text or "")
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = _CTRL_RE.sub("", s)
    return " ".join(s.split())


def char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def width(text: str) -> int:
    """Display width of text, ignoring ANSI color codes."""
    return sum(char_width(c) for c in _ANSI_RE.sub("", text))


def truncate(text: str, max_w: int, ellipsis: str = "…") -> str:
    """Truncate plain text (no ANSI codes) to a display width."""
    if max_w <= 0:
        return ""
    if width(text) <= max_w:
        return text
    out, w = [], 0
    limit = max_w - width(ellipsis)
    for c in text:
        cw = char_width(c)
        if w + cw > limit:
            break
        out.append(c)
        w += cw
    return "".join(out) + ellipsis


def pad(text: str, w: int) -> str:
    """Right-pad text (may contain ANSI codes) to a display width."""
    return text + " " * max(0, w - width(text))


def human_bytes(n: float | None) -> str:
    if n is None:
        return "-"
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit in ("B", "K") else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}G"


def human_count(n: float | None) -> str:
    if n is None:
        return "-"
    if n < 1000:
        return f"{n:.0f}"
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.2f}M"


def human_duration(secs: float | None) -> str:
    if secs is None or secs < 0:
        return "-"
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h:02d}h"


def ago(ts: float | None, now: float | None = None) -> str:
    if ts is None:
        return "-"
    return human_duration((now or time.time()) - ts) + " ago"


def parse_ts(value: object) -> float | None:
    """Parse an ISO-8601 timestamp (or epoch number) into epoch seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) / (1000.0 if value > 1e11 else 1.0)
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Python < 3.11 only accepts 0, 3 or 6 fractional digits.
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)\.(\d+)(.*)$", s)
    if m:
        s = f"{m.group(1)}.{m.group(2)[:6].ljust(6, '0')}{m.group(3)}"
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def short_path(path: str | None) -> str:
    if not path:
        return "-"
    home = os.path.expanduser("~")
    if home and home != "~" and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home):]
    return path


def norm_path(path: str | None) -> str | None:
    if not path:
        return None
    p = os.path.normpath(path)
    return os.path.normcase(p)
