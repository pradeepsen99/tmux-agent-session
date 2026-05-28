from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

from .commands import run_command
from .models import ProcessInfo


SESSION_ID_PATTERNS = [
    re.compile(
        r"(?:--session|--resume|session_id|session)\s*[= ]\s*([A-Za-z0-9._:-]{6,})"
    ),
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
    cwd = _get_proc_cwd(pid)
    if cwd is not None:
        return cwd

    return get_cwds([pid]).get(pid)


def _get_proc_cwd(pid: int) -> str | None:
    proc_cwd = Path(f"/proc/{pid}/cwd")
    if proc_cwd.exists():
        try:
            return str(proc_cwd.resolve())
        except OSError:
            return None
    return None


def _parse_lsof_cwds(output: str) -> dict[int, str]:
    cwds: dict[int, str] = {}
    current_pid: int | None = None
    for line in output.splitlines():
        if line.startswith("p"):
            try:
                current_pid = int(line[1:])
            except ValueError:
                current_pid = None
        elif line.startswith("n") and current_pid is not None:
            cwds[current_pid] = line[1:]
    return cwds


def get_cwds(pids: list[int]) -> dict[int, str]:
    cwds: dict[int, str] = {}
    unresolved: list[int] = []
    for pid in dict.fromkeys(pids):
        cwd = _get_proc_cwd(pid)
        if cwd is None:
            unresolved.append(pid)
        else:
            cwds[pid] = cwd

    if not unresolved:
        return cwds

    output = run_command(
        [
            "lsof",
            "-a",
            "-p",
            ",".join(str(pid) for pid in unresolved),
            "-d",
            "cwd",
            "-Fn",
        ]
    )
    cwds.update(_parse_lsof_cwds(output))
    return cwds


def resolve_process_cwds(processes: list[ProcessInfo]) -> None:
    missing = [proc for proc in processes if proc.cwd is None]
    if not missing:
        return

    cwd_by_pid = get_cwds([proc.pid for proc in missing])
    for proc in missing:
        proc.cwd = cwd_by_pid.get(proc.pid)


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


def detect_processes(resolve_cwd: bool = False) -> list[ProcessInfo]:
    ps_output = run_command(["ps", "-axo", "pid=,ppid=,tty=,etime=,command="])
    processes: list[ProcessInfo] = []
    for line in ps_output.splitlines():
        line = line.rstrip()
        if not line:
            continue
        parts = line.split(None, 4)
        if len(parts) != 5:
            continue
        pid, ppid, tty, etime, command = parts
        lowered_command = command.lower()
        if (
            "codex" not in lowered_command
            and "opencode" not in lowered_command
            and "cursor-agent" not in lowered_command
            and "claude" not in lowered_command
        ):
            continue
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
        elif executable in {"cursor-agent", "cursor-agent.exe"} or (
            "cursor-agent" in lowered_command and "worker-server" not in lowered_command
        ):
            # The cursor-agent launcher execs a bare `agent` binary whose command
            # line still references the cursor-agent install path. Skip the
            # detached worker-server child so only interactive sessions surface.
            tool = "cursor-agent"
        elif executable in {"claude", "claude.exe"}:
            tool = "claude"
        if not tool:
            continue
        processes.append(
            ProcessInfo(
                pid=int(pid),
                ppid=int(ppid),
                tty=None if tty in {"?", "??"} else tty,
                etime_seconds=parse_etime_to_seconds(etime),
                cwd=get_cwd(int(pid)) if resolve_cwd else None,
                command=command,
                tool=tool,
                session_ids=extract_session_ids(command),
            )
        )

    enrich_claude_sessions(processes)
    return processes


def claude_live_sessions() -> dict[int, dict]:
    """Map pid -> live session entry from ``claude agents --json``.

    Claude does not place a session id on its command line, so this is the only
    reliable bridge from a running ``claude`` process to its session id. Returns
    an empty mapping when the command is unavailable or its output is not the
    expected JSON array.
    """
    output = run_command(["claude", "agents", "--json"])
    if not output or not output.strip():
        return {}
    try:
        entries = json.loads(output)
    except json.JSONDecodeError:
        return {}
    if not isinstance(entries, list):
        return {}

    sessions: dict[int, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        pid = entry.get("pid")
        if isinstance(pid, int):
            sessions[pid] = entry
    return sessions


def enrich_claude_sessions(processes: list[ProcessInfo]) -> None:
    """Fill in session ids (and cwd) for detected ``claude`` processes.

    Only invokes ``claude agents --json`` when at least one claude process is
    present, so there is no startup cost when Claude is not running.
    """
    claude_procs = [proc for proc in processes if proc.tool == "claude"]
    if not claude_procs:
        return

    sessions = claude_live_sessions()
    if not sessions:
        return

    for proc in claude_procs:
        entry = sessions.get(proc.pid)
        if entry is None:
            continue
        session_id = entry.get("sessionId")
        if isinstance(session_id, str) and session_id not in proc.session_ids:
            proc.session_ids.append(session_id)
        if proc.cwd is None:
            cwd = entry.get("cwd")
            if isinstance(cwd, str) and cwd.strip():
                proc.cwd = cwd
