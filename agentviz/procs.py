"""Cross-platform process sampling.

Uses psutil when installed; otherwise falls back to /proc (Linux), `ps` (macOS/BSD)
or PowerShell CIM queries (Windows).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field

try:  # optional dependency
    import psutil  # type: ignore
except Exception:  # pragma: no cover - depends on environment
    psutil = None


@dataclass
class ProcInfo:
    pid: int
    ppid: int
    name: str
    exe: str | None = None
    cmdline: list = field(default_factory=list)
    cpu_time: float | None = None  # cumulative user+system seconds
    cpu_percent: float | None = None
    rss: int | None = None
    create_time: float | None = None


class _Backend:
    name = "none"

    def list(self) -> dict:
        raise NotImplementedError

    def cwd(self, pid: int) -> str | None:
        return None


class PsutilBackend(_Backend):
    name = "psutil"

    def list(self) -> dict:
        out = {}
        attrs = ["pid", "ppid", "name", "exe", "cmdline", "cpu_times", "memory_info", "create_time"]
        for p in psutil.process_iter(attrs):
            i = p.info
            ct = i.get("cpu_times")
            mem = i.get("memory_info")
            out[i["pid"]] = ProcInfo(
                pid=i["pid"], ppid=i.get("ppid") or 0, name=i.get("name") or "",
                exe=i.get("exe"), cmdline=i.get("cmdline") or [],
                cpu_time=(ct.user + ct.system) if ct else None,
                rss=mem.rss if mem else None, create_time=i.get("create_time"),
            )
        return out

    def cwd(self, pid: int) -> str | None:
        try:
            return psutil.Process(pid).cwd()
        except Exception:
            return None


class ProcfsBackend(_Backend):
    name = "procfs"

    def __init__(self) -> None:
        self.hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        self.page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
        self.btime = 0.0
        try:
            with open("/proc/stat") as f:
                for line in f:
                    if line.startswith("btime"):
                        self.btime = float(line.split()[1])
        except OSError:
            pass

    def list(self) -> dict:
        out = {}
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                with open(f"/proc/{pid}/stat", "rb") as f:
                    stat = f.read().decode(errors="replace")
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    raw = f.read()
            except OSError:
                continue
            lp, rp = stat.find("("), stat.rfind(")")
            name = stat[lp + 1:rp]
            fields = stat[rp + 2:].split()
            try:
                ppid = int(fields[1])
                cpu = (int(fields[11]) + int(fields[12])) / self.hz
                start = self.btime + int(fields[19]) / self.hz
                rss = int(fields[21]) * self.page
            except (IndexError, ValueError):
                continue
            cmd = [a.decode(errors="replace") for a in raw.split(b"\0") if a]
            try:
                exe = os.readlink(f"/proc/{pid}/exe")
            except OSError:
                exe = None
            out[pid] = ProcInfo(pid, ppid, name, exe, cmd, cpu, None, rss, start)
        return out

    def cwd(self, pid: int) -> str | None:
        try:
            return os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            return None


def _parse_etime(s: str) -> float:
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    parts = [int(p) for p in s.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts
    return days * 86400 + h * 3600 + m * 60 + sec


class PsBackend(_Backend):
    name = "ps"

    def list(self) -> dict:
        out = {}
        now = time.time()
        res = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,pcpu=,rss=,etime=,args="],
                             capture_output=True, text=True, errors="replace")
        for line in res.stdout.splitlines():
            parts = line.split(None, 5)
            if len(parts) < 6:
                continue
            try:
                pid, ppid = int(parts[0]), int(parts[1])
                pcpu, rss = float(parts[2]), int(parts[3]) * 1024
                start = now - _parse_etime(parts[4])
            except ValueError:
                continue
            cmd = parts[5].split()
            out[pid] = ProcInfo(pid, ppid, os.path.basename(cmd[0]) if cmd else "", cmd[0] if cmd else None,
                                cmd, None, pcpu, rss, start)
        return out

    def cwd(self, pid: int) -> str | None:
        try:
            res = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                                 capture_output=True, text=True, timeout=2)
        except Exception:
            return None
        for line in res.stdout.splitlines():
            if line.startswith("n"):
                return line[1:]
        return None


class WindowsBackend(_Backend):
    name = "powershell"
    _SCRIPT = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Get-CimInstance Win32_Process | ForEach-Object { [pscustomobject]@{"
        "p=$_.ProcessId;pp=$_.ParentProcessId;n=$_.Name;e=$_.ExecutablePath;c=$_.CommandLine;"
        "w=$_.WorkingSetSize;t=($_.KernelModeTime+$_.UserModeTime);"
        "s=if($_.CreationDate){([DateTimeOffset]$_.CreationDate).ToUnixTimeSeconds()}else{0}} }"
        " | ConvertTo-Json -Compress"
    )

    def list(self) -> dict:
        res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", self._SCRIPT],
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            rows = json.loads((res.stdout or "").lstrip("\ufeff") or "[]")
        except ValueError:
            return {}
        if isinstance(rows, dict):
            rows = [rows]
        out = {}
        for r in rows:
            cmd = r.get("c") or ""
            out[r["p"]] = ProcInfo(
                pid=r["p"], ppid=r.get("pp") or 0, name=r.get("n") or "", exe=r.get("e"),
                cmdline=_split_windows_cmdline(cmd), cpu_time=(r.get("t") or 0) / 1e7,
                rss=r.get("w"), create_time=float(r.get("s") or 0) or None,
            )
        return out


def _split_windows_cmdline(cmd: str) -> list:
    args, cur, quoted = [], [], False
    for ch in cmd:
        if ch == '"':
            quoted = not quoted
        elif ch in " \t" and not quoted:
            if cur:
                args.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        args.append("".join(cur))
    return args


def make_backend() -> _Backend:
    if psutil is not None:
        return PsutilBackend()
    if sys.platform.startswith("linux") and os.path.isdir("/proc"):
        return ProcfsBackend()
    if os.name == "nt":
        return WindowsBackend()
    return PsBackend()


class Sampler:
    """Takes process snapshots and derives CPU% from cumulative CPU time deltas."""

    def __init__(self, backend: _Backend | None = None) -> None:
        self.backend = backend or make_backend()
        self._prev: dict = {}
        self._prev_t: float | None = None
        self._cwd_cache: dict = {}

    def sample(self) -> dict:
        now = time.monotonic()
        try:
            procs = self.backend.list()
        except Exception:
            procs = {}
        dt = (now - self._prev_t) if self._prev_t else None
        for pid, p in procs.items():
            prev = self._prev.get(pid)
            if p.cpu_percent is None and p.cpu_time is not None and prev is not None and dt:
                if prev.create_time == p.create_time and prev.cpu_time is not None:
                    p.cpu_percent = max(0.0, (p.cpu_time - prev.cpu_time) / dt * 100.0)
        self._prev, self._prev_t = procs, now
        # Drop cached cwd entries of processes that exited.
        self._cwd_cache = {k: v for k, v in self._cwd_cache.items() if k[0] in procs}
        return procs

    def cwd(self, p: ProcInfo) -> str | None:
        key = (p.pid, p.create_time)
        if key not in self._cwd_cache:
            self._cwd_cache[key] = self.backend.cwd(p.pid)
        return self._cwd_cache[key]
