"""ANSI terminal rendering of agent views."""

from __future__ import annotations

import os
import time
from datetime import datetime

from .agents import KIND_BY_KEY
from .util import char_width, clean, human_bytes, human_count, human_duration, short_path, truncate, width

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
                              dot="*", odot="o", branch="@", bullet="-", logo="#", mail="M:", arrow="->")
        else:
            self.box = dict(tl="╭", tr="╮", bl="╰", br="╯", h="─", v="│")
            self.glyph = dict(user="›", text="◆", tool="▶", result="✓", error="✗", think="…",
                              dot="●", odot="○", branch="⎇", bullet="·", logo="◆", mail="⇄", arrow="→")  # ✉ renders as a 2-cell emoji in some terminals

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
    bus_name = getattr(view, "bus_name", "")
    if bus_name:
        who = who[:-1] + f" {theme.glyph['bullet']} {theme.glyph['mail']} {bus_name} "
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


def _name_style(name: str, theme: Theme) -> tuple:
    if name == "user":
        return (1, 38, 5, 214)
    kind = KIND_BY_KEY.get(name.split("@")[0].split("#")[0])
    return (1,) + theme.fg(kind.color) if kind else (1,)


def message_segs(msg, theme: Theme) -> list:
    ts = datetime.fromtimestamp(msg.ts).strftime("%H:%M:%S")
    to = "everyone" if msg.to.lower() in ("*", "all", "everyone") else msg.to
    return [(ts + " ", DIM), (msg.sender, _name_style(msg.sender, theme)),
            (f" {theme.glyph['arrow']} ", DIM), (to, _name_style(to, theme)), ("  ", ()),
            (clean(msg.text), (38, 5, 255))]


def compose_footer(compose: str, targets: list, w: int, theme: Theme) -> str:
    hint = "  (@name text · Enter send · Esc cancel)"
    if targets:
        hint = "  to: " + ", ".join(["@" + t for t in targets][:6]) + hint
    return fit([(f" {theme.glyph['mail']} message › ", (1, 38, 5, 214)), (compose, ()),
                ("█" if not theme.ascii else "_", (5,)), (hint, DIM)], w, theme)


def frame(views: list, events: list, labels: dict, size: tuple, tick: int, theme: Theme,
          paused: bool = False, interval: float = 1.0, show_feed: bool = True,
          backend: str = "", now: float | None = None, messages: list | None = None,
          compose: str | None = None, targets: list | None = None, notice: str = "",
          relay: str = "") -> list:
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
    head.append((f"  {sum(counts.values())} running {theme.glyph['bullet']} {busy} busy  ", ()))
    for k, n in counts.items():
        head += [(theme.glyph["dot"] + " ", theme.fg(k.color)), (f"{k.label} {'x' if theme.ascii else '×'}{n}  ", ())]
    right = datetime.fromtimestamp(now).strftime("%H:%M:%S")
    right = ("PAUSED  " if paused else "") + (f"{relay}  " if relay else "") + f"refresh {interval:g}s  {right} "
    head_w = w - width(right)
    out.append(fit(head, head_w, theme) + fit([(right, (38, 5, 214) if paused else DIM)], width(right), theme))
    out.append("")

    if compose is not None:
        footer = compose_footer(compose, targets or [], w, theme)
    else:
        footer = fit([(" q", (1,)), (" quit  ", DIM), ("p", (1,)), (" pause  ", DIM), ("+/-", (1,)),
                      (" speed  ", DIM), ("f", (1,)), (" feed  ", DIM), ("m", (1,)), (" message  ", DIM),
                      ("c", (1,)), (" clear done  ", DIM),
                      ("x", (1,)) if relay else ("", ()), (" relay  ", DIM) if relay else ("", ()),
                      (notice + "  " if notice else "", (38, 5, 214)),
                      (f"[{backend}]" if backend else "", DIM)], w, theme)
    messages = messages or []
    msg_rows = min(6, len(messages)) + 1 if messages else 0

    body_rows = rows - len(out) - 1
    if not views:
        out.append(fit([("  No active coding agents found.", (1,))], w, theme))
        out.append(fit([("  Watching for: Claude Code, Codex, Gemini CLI, Copilot CLI, Cursor Agent, "
                         "OpenCode, Aider, Qwen Code, Amp", DIM)], w, theme))
        out.append(fit([("  Tip: run with --demo to preview the dashboard.", DIM)], w, theme))
        body_rows -= 3

    # Decide how many event lines each card gets so everything fits.
    feed_min = (4 if show_feed else 0) + msg_rows
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
    if messages and remaining >= 2:
        rule = "─" if not theme.ascii else "-"
        out.append(fit([(" Messages ", (1,)), (rule * (card_w - 10), DIM)], w, theme))
        n_msgs = min(len(messages), max(1, min(6, remaining - 1 - (4 if show_feed else 0))))
        for m in messages[-n_msgs:]:
            out.append(fit(message_segs(m, theme), w, theme))
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


VT_OK, VT_LEGACY, VT_NO_CONSOLE = "ok", "legacy", "no-console"
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004


def windows_vt_status(k32=None) -> str:
    """Enable ANSI escape processing on a Windows console and report whether it works.

    Returns VT_OK (ANSI works, or not Windows), VT_LEGACY (a console that cannot process
    ANSI sequences, e.g. before Windows 10 or with "Use legacy console" enabled) or
    VT_NO_CONSOLE (stdout is redirected, not a console).
    """
    if k32 is None:
        if os.name != "nt":
            return VT_OK
        if os.environ.get("AGENTVIZ_FORCE_VT"):
            return VT_OK
    try:
        import ctypes
        if k32 is None:
            k32 = ctypes.windll.kernel32
            k32.SetConsoleOutputCP(65001)
            k32.SetConsoleCP(65001)
        h = k32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(h, ctypes.byref(mode)):
            return VT_NO_CONSOLE
        if mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING:
            return VT_OK
        if not k32.SetConsoleMode(h, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING):
            return VT_LEGACY
        # Some consoles accept the call but silently drop the flag; read it back.
        if not k32.GetConsoleMode(h, ctypes.byref(mode)) or not mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING:
            return VT_LEGACY
        return VT_OK
    except Exception:
        return VT_LEGACY


LEGACY_CONSOLE_HELP = """\
agentviz: this console cannot display the live dashboard (no ANSI/VT support).
  이 콘솔은 ANSI 색상/커서 제어를 지원하지 않아 실시간 대시보드를 표시할 수 없습니다.

  Fix / 해결 방법:
    - Use Windows Terminal, or cmd/PowerShell on Windows 10 or later
      (Windows Terminal 또는 Windows 10 이상의 cmd/PowerShell 사용)
    - If you are on Windows 10+, open the console window Properties and turn off
      "Use legacy console" (창 속성에서 "레거시 콘솔 사용" 해제 후 다시 실행)

  Meanwhile / 대신 사용할 수 있는 명령:
    agentviz --once --ascii     one plain-text snapshot (한 번만 출력)
    agentviz --json             machine-readable snapshot (JSON 출력)
    agentviz messages -f        follow agent messages (에이전트 메시지 보기)

  If your terminal does support ANSI, set AGENTVIZ_FORCE_VT=1 to skip this check.
"""


__all__ = ["Theme", "frame", "card", "fit", "spark", "windows_vt_status", "LEGACY_CONSOLE_HELP",
           "VT_OK", "VT_LEGACY", "VT_NO_CONSOLE", "char_width"]
