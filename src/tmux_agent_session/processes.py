from __future__ import annotations

import re
import shlex
from pathlib import Path

from .commands import run_command
from .models import ProcessInfo


SESSION_ID_PATTERNS = [
    re.compile(r"(?:--session|session_id|session)\s*[= ]\s*([A-Za-z0-9._:-]{6,})"),
    re.compile(r"\b([a-f0-9]{16,64})\b"),
]


def parse_etime_to_seconds(raw: str) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    parts = raw.split("-")
    days = 0
    time_part = raw
    if len(parts) == 2:
        days = int(parts[0])
        time_part = parts[1]
    tparts = [int(x) for x in time_part.split(":")]
    if len(tparts) == 3:
        h, m, s = tparts
    elif len(tparts) == 2:
        h = 0
        m, s = tparts
    else:
        return None
    return days * 86400 + h * 3600 + m * 60 + s


def get_cwd(pid: int) -> str | None:
    proc_cwd = Path(f"/proc/{pid}/cwd")
    if proc_cwd.exists():
        try:
            return str(proc_cwd.resolve())
        except OSError:
            return None

    output = run_command(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
    for line in output.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def normalize_tty(value: str | None) -> str | None:
    if not value:
        return None
    return value.removeprefix("/dev/")


def extract_session_ids(command: str) -> list[str]:
    found: list[str] = []
    for pat in SESSION_ID_PATTERNS:
        for match in pat.findall(command):
            candidate = match.strip()
            if candidate not in found:
                found.append(candidate)
    return found


def detect_processes() -> list[ProcessInfo]:
    ps_output = run_command(["ps", "-axo", "pid=,ppid=,tty=,etime=,command="])
    processes: list[ProcessInfo] = []
    for line in ps_output.splitlines():
        line = line.rstrip()
        if not line:
            continue
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(.*)$", line)
        if not m:
            continue
        pid, ppid, tty, etime, command = m.groups()
        try:
            argv = shlex.split(command)
        except ValueError:
            argv = command.split()
        executable = Path(argv[0]).name.lower() if argv else ""
        tool = None
        if executable in {"codex", "codex.exe"}:
            tool = "codex"
        elif executable in {"opencode", "opencode.exe"}:
            tool = "opencode"
        if not tool:
            continue
        processes.append(
            ProcessInfo(
                pid=int(pid),
                ppid=int(ppid),
                tty=None if tty == "?" else tty,
                etime_seconds=parse_etime_to_seconds(etime),
                cwd=get_cwd(int(pid)),
                command=command,
                tool=tool,
                session_ids=extract_session_ids(command),
            )
        )
    return processes
