"""ANSI terminal rendering of agent views."""

from __future__ import annotations

import os
import time
from datetime import datetime

from .util import char_width, human_bytes, human_count, human_duration, short_path, truncate, width

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPINNER_ASCII = "|/-\\"
SPARK = " ▁▂▃▄▅▆▇█"
SPARK_ASCII = " .:-=+*#%@"


class Theme:
    def __init__(self, color: bool = True, ascii_only: bool = False) -> None:
        self.color = color
        self.ascii = ascii_only
        if ascii_only:
            self.box = dict(tl="+", tr="+", bl="+", br="+", h="-", v="|")
            self.glyph = dict(user=">", text="*", tool="$", result="+", error="x", think="~",
                              dot="*", odot="o", branch="@", bullet="-", logo="#")
        else:
            self.box = dict(tl="╭", tr="╮", bl="╰", br="╯", h="─", v="│")
            self.glyph = dict(user="›", text="◆", tool="▶", result="✓", error="✗", think="…",
                              dot="●", odot="○", branch="⎇", bullet="·", logo="◆")

    def sgr(self, *codes) -> str:
        if not self.color or not codes:
            return ""
        return "\x1b[" + ";".join(str(c) for c in codes) + "m"

    @property
    def reset(self) -> str:
        return "\x1b[0m" if self.color else ""

    def fg(self, n: int) -> tuple:
        return (38, 5, n)


# A styled line is a list of (text, sgr-codes tuple) segments.

def fit(segs: list, w: int, theme: Theme) -> str:
    """Render segments, truncating to display width w and padding with spaces."""
    out, used = [], 0
    for i, (text, style) in enumerate(segs):
        if used >= w:
            break
        tw = width(text)
        if used + tw > w:
            text = truncate(text, w - used, "~" if theme.ascii else "…")
            tw = width(text)
        out.append(theme.sgr(*style) + text + (theme.reset if style else ""))
        used += tw
    out.append(" " * max(0, w - used))
    return "".join(out)


def spark(values, n: int, theme: Theme) -> str:
    chars = SPARK_ASCII if theme.ascii else SPARK
    vals = list(values)[-n:]
    top = max([50.0] + vals)
    s = "".join(chars[min(len(chars) - 1, int(round(v / top * (len(chars) - 1))))] for v in vals)
    return s.rjust(n, chars[0])


STATUS_STYLE = {
    "WORKING": (1, 38, 5, 46),
    "TOOL": (1, 38, 5, 220),
    "WAITING": (38, 5, 45),
    "IDLE": (38, 5, 244),
    "ENDED": (38, 5, 240),
}
STATUS_TEXT = {"WORKING": "WORKING", "TOOL": "RUNNING TOOL", "WAITING": "WAITING FOR INPUT",
               "IDLE": "IDLE", "ENDED": "ENDED"}
EVENT_STYLE = {"user": (38, 5, 75), "text": (38, 5, 255), "tool": (38, 5, 220),
               "result": (38, 5, 108), "error": (38, 5, 203), "think": (3, 38, 5, 141)}
DIM = (38, 5, 244)


def status_badge(status: str, tick: int, theme: Theme) -> tuple:
    if status in ("WORKING", "TOOL"):
        sp = SPINNER_ASCII if theme.ascii else SPINNER
        icon = sp[tick % len(sp)]
    elif status == "WAITING":
        icon = theme.glyph["dot"]
    else:
        icon = theme.glyph["odot"]
    return f" {icon} {STATUS_TEXT.get(status, status)} ", STATUS_STYLE.get(status, DIM)


def event_segs(ev, theme: Theme, now: float, label: str | None = None, agent_style=None) -> list:
    ts = datetime.fromtimestamp(ev.ts).strftime("%H:%M:%S")
    segs = [(ts + " ", DIM)]
    if label is not None:
        segs.append((label, agent_style or ()))
    segs.append((theme.glyph.get(ev.kind, "·") + " ", EVENT_STYLE.get(ev.kind, ())))
    segs.append((ev.text, EVENT_STYLE.get(ev.kind, ()) if ev.kind != "result" else DIM))
    return segs


def card(view, w: int, n_events: int, tick: int, theme: Theme, now: float) -> list:
    """Render one agent as a bordered card; returns rendered lines."""
    b = theme.box
    active = view.status in ("WORKING", "TOOL", "WAITING")
    color = view.kind.color if active or view.pid else 244
    border = theme.fg(color)
    inner = max(1, w - 4)

    # ---- top border: ── ● Label ── pid … ───── badge ─
    name = f" {theme.glyph['dot']} {view.kind.label} "
    who = f" pid {view.pid} " if view.pid else " log only "
    if view.nprocs > 1:
        who = who[:-1] + f" {theme.glyph['bullet']} {view.nprocs} procs "
    badge, badge_style = status_badge(view.status, tick, theme)
    fill = w - 2 - 1 - width(name) - width(who) - width(badge) - 1
    if fill < 1:
        who, fill = "", w - 2 - 1 - width(name) - width(badge) - 1
    top = fit([(b["tl"] + b["h"], border), (name, (1,) + border), (who, DIM),
               (b["h"] * max(0, fill), border), (badge, badge_style), (b["h"] + b["tr"], border)], w, theme)
    lines = [top]

    def row(segs):
        lines.append(fit([(b["v"] + " ", border)], 2, theme) + fit(segs, inner, theme)
                     + fit([(" " + b["v"], border)], 2, theme))

    s = view.session
    title = (s.title if s and s.title else "") or (short_path(view.cwd) if view.cwd else "(no transcript)")
    row([(title, (1,))])

    info = [("dir ", DIM), (short_path(view.cwd or (s.cwd if s else None)), ())]
    if s and s.branch:
        info += [(f"  {theme.glyph['branch']} ", DIM), (s.branch, (38, 5, 180))]
    if s and s.model:
        info += [("  model ", DIM), (s.model, (38, 5, 146))]
    row(info)

    stats = []
    if view.pid:
        cpu = view.cpu
        stats += [("cpu ", DIM), (spark(view.history, 16, theme), border),
                  (f" {cpu:5.1f}%" if cpu is not None else "   --%", ()),
                  ("  mem ", DIM), (human_bytes(view.rss), ()),
                  ("  up ", DIM), (human_duration(now - view.started) if view.started else "-", ())]
    if s:
        last = view.last_activity
        stats += [("  tok ", DIM), (f"{human_count(s.tokens_in)}↑ {human_count(s.tokens_out)}↓"
                                    if not theme.ascii else
                                    f"in {human_count(s.tokens_in)} out {human_count(s.tokens_out)}", ())]
        if s.context:
            stats += [("  ctx ", DIM), (human_count(s.context), ())]
        if view.subagents:
            stats += [("  subagents ", DIM), (f"{view.active_subagents}/{len(view.subagents)}", (38, 5, 141))]
        stats += [("  ", ()), (human_duration(now - last) + " ago" if last else "", DIM)]
    if stats and stats[0][0].startswith("  "):
        stats[0] = (stats[0][0][2:], stats[0][1])
    row(stats)

    if n_events > 0:
        evs = list(s.events)[-n_events:] if s else []
        for ev in evs:
            row(event_segs(ev, theme, now))
        for _ in range(n_events - len(evs)):
            row([("", ())])

    lines.append(fit([(b["bl"] + b["h"] * (w - 2) + b["br"], border)], w, theme))
    return lines


def frame(views: list, events: list, labels: dict, size: tuple, tick: int, theme: Theme,
          paused: bool = False, interval: float = 1.0, show_feed: bool = True,
          backend: str = "", now: float | None = None) -> list:
    now = now or time.time()
    cols, rows = size
    w = max(20, cols - 1)  # avoid writing into the last column (auto-wrap)
    out = []

    # ---- header
    counts: dict = {}
    for v in views:
        if v.pid:
            counts[v.kind] = counts.get(v.kind, 0) + 1
    busy = sum(1 for v in views if v.status in ("WORKING", "TOOL"))
    head = [(f" {theme.glyph['logo']} AGENTVIZ ", (1, 7) if theme.color else ())]
    head.append((f"  {sum(counts.values())} running · {busy} busy  ", ()))
    for k, n in counts.items():
        head += [(theme.glyph["dot"] + " ", theme.fg(k.color)), (f"{k.label} {'x' if theme.ascii else '×'}{n}  ", ())]
    right = datetime.fromtimestamp(now).strftime("%H:%M:%S")
    right = ("PAUSED  " if paused else "") + f"refresh {interval:g}s  {right} "
    head_w = w - width(right)
    out.append(fit(head, head_w, theme) + fit([(right, (38, 5, 214) if paused else DIM)], width(right), theme))
    out.append("")

    footer = fit([(" q", (1,)), (" quit  ", DIM), ("p", (1,)), (" pause  ", DIM), ("+/-", (1,)),
                  (" speed  ", DIM), ("f", (1,)), (" feed  ", DIM),
                  (f"[{backend}]" if backend else "", DIM)], w, theme)

    body_rows = rows - len(out) - 1
    if not views:
        out.append(fit([("  No active coding agents found.", (1,))], w, theme))
        out.append(fit([("  Watching for: Claude Code, Codex, Gemini CLI, Copilot CLI, Cursor Agent, "
                         "OpenCode, Aider, Qwen Code, Amp", DIM)], w, theme))
        out.append(fit([("  Tip: run with --demo to preview the dashboard.", DIM)], w, theme))
        body_rows -= 3

    # Decide how many event lines each card gets so everything fits.
    feed_min = 4 if show_feed else 0
    n = len(views)
    per_card = 0
    if n:
        base = 5  # borders + title + info + stats
        for k in (3, 2, 1, 0):
            if n * (base + k) + feed_min <= body_rows:
                per_card = k
                break
        fits = max(1, (body_rows - feed_min) // (base + per_card)) if body_rows > base else 0
    else:
        fits = 0

    card_w = min(w, 120)
    shown = 0
    for v in views[:fits]:
        out.extend(card(v, card_w, per_card, tick, theme, now))
        shown += 1
    if shown < n:
        out.append(fit([(f"  +{n - shown} more agent(s) - enlarge the window", DIM)], w, theme))

    remaining = rows - len(out) - 1
    if show_feed and remaining >= 3:
        out.append(fit([(" Activity ", (1,)), ("─" * (card_w - 10) if not theme.ascii else "-" * (card_w - 10),
                                                DIM)], w, theme))
        colors = {v.session.path: v.kind for v in views if v.session}
        for v in views:
            for sub in v.subagents:
                colors[sub.path] = v.kind
        lw = max((width(l) for l in labels.values()), default=0)
        lw = min(lw, 22)
        for ev in events[: remaining - 1]:
            kind = colors.get(ev.session)
            label = labels.get(ev.session, "")
            label = truncate(label, lw) + " " * (lw - width(truncate(label, lw))) + " "
            out.append(fit(event_segs(ev, theme, now, label, theme.fg(kind.color) if kind else DIM), w, theme))

    out = out[: rows - 1]
    while len(out) < rows - 1:
        out.append("")
    out.append(footer)
    return out


def enable_windows_vt() -> bool:
    """Enable ANSI escape processing and UTF-8 output on Windows consoles."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetConsoleOutputCP(65001)
        k32.SetConsoleCP(65001)
        h = k32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:
        return False


__all__ = ["Theme", "frame", "card", "fit", "spark", "enable_windows_vt", "char_width"]
