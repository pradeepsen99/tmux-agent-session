#!/usr/bin/env python3
"""
Inspect likely active OpenCode and Codex CLI sessions.

Heuristic approach:
- detect running processes that look like codex / opencode
- inspect known session storage directories
- correlate by cwd, recency, and optional session ids found in process cmdlines
- classify sessions as active / recent / stale

This is intentionally best-effort. Neither tool appears to expose a universal,
authoritative live-session registry for outside tools.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


DEFAULT_CODEX_DIR = Path("~/.codex/sessions").expanduser()
DEFAULT_OPENCODE_DIRS = [
    Path("~/.local/share/opencode/storage").expanduser(),
    Path("~/Library/Application Support/opencode/storage").expanduser(),
    Path("%APPDATA%/opencode/storage").expanduser(),
]


@dataclass
class ProcessInfo:
    pid: int
    ppid: int | None
    tty: str | None
    etime_seconds: int | None
    cwd: str | None
    command: str
    tool: str
    session_ids: list[str] = field(default_factory=list)


@dataclass
class SessionRecord:
    tool: str
    session_id: str
    path: Path | None
    last_write: float | None
    cwd: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    matched_process: ProcessInfo | None = None
    score: int = 0
    status: str = "stale"
    reasons: list[str] = field(default_factory=list)


SESSION_ID_PATTERNS = [
    re.compile(r"(?:--session|session_id|session)\s*[= ]\s*([A-Za-z0-9._:-]{6,})"),
    re.compile(r"\b([a-f0-9]{16,64})\b"),
]


def run_command(cmd: list[str]) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return result.stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


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


def safe_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def read_json_file(path: Path) -> Any | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def read_jsonl_file(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    records.append(item)
    except OSError:
        return []
    return records


def find_session_files(base: Path) -> Iterable[Path]:
    if not base.exists():
        return []
    candidates: list[Path] = []
    for pattern in ("*.json", "**/*.json", "*.jsonl", "**/*.jsonl", "*.db", "**/*.db"):
        candidates.extend(base.glob(pattern))
    uniq: dict[str, Path] = {}
    for path in candidates:
        uniq[str(path)] = path
    return uniq.values()


def normalize_cwd(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(Path(value).expanduser().resolve())
    except OSError:
        return str(Path(value).expanduser())


def extract_session_from_json(
    tool: str, path: Path, data: dict[str, Any]
) -> SessionRecord | None:
    possible_id_keys = ["id", "session_id", "sessionId", "uuid"]
    session_id = None
    for key in possible_id_keys:
        value = data.get(key)
        if isinstance(value, str) and len(value) >= 6:
            session_id = value
            break
    if not session_id:
        session_id = path.stem

    cwd = None
    for key in ("cwd", "working_directory", "workingDirectory", "repo_path", "path"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            cwd = value
            break

    metadata = {
        k: data.get(k)
        for k in ("title", "model", "updated_at", "created_at", "status")
        if k in data
    }

    return SessionRecord(
        tool=tool,
        session_id=session_id,
        path=path,
        last_write=safe_mtime(path),
        cwd=normalize_cwd(cwd),
        metadata=metadata,
    )


def extract_session_records(tool: str, path: Path, data: Any) -> list[SessionRecord]:
    if isinstance(data, dict):
        rec = extract_session_from_json(tool, path, data)
        return [rec] if rec is not None else []

    if isinstance(data, list):
        records: list[SessionRecord] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            rec = extract_session_from_json(tool, path, item)
            if rec is not None:
                records.append(rec)
        return records

    return []


def extract_codex_session(path: Path) -> SessionRecord | None:
    entries = read_jsonl_file(path)
    if not entries:
        return None

    session_meta: dict[str, Any] | None = None
    latest_turn_context: dict[str, Any] | None = None

    for entry in entries:
        entry_type = entry.get("type")
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        if entry_type == "session_meta" and session_meta is None:
            session_meta = payload
        elif entry_type == "turn_context":
            latest_turn_context = payload

    if session_meta is None and latest_turn_context is None:
        return None

    session_id = None
    candidates = [
        (session_meta or {}).get("id"),
        (latest_turn_context or {}).get("session_id"),
        path.stem.removeprefix("rollout-"),
        path.stem,
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            session_id = candidate.strip()
            break

    cwd = None
    for candidate in (
        (latest_turn_context or {}).get("cwd"),
        (session_meta or {}).get("cwd"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            cwd = candidate.strip()
            break

    metadata: dict[str, Any] = {}
    if session_meta:
        for key in (
            "timestamp",
            "originator",
            "cli_version",
            "source",
            "model_provider",
        ):
            if key in session_meta:
                metadata[key] = session_meta[key]
    if latest_turn_context:
        for key in ("model", "approval_policy", "personality", "summary"):
            if key in latest_turn_context:
                metadata[key] = latest_turn_context[key]

    return SessionRecord(
        tool="codex",
        session_id=session_id or path.stem,
        path=path,
        last_write=safe_mtime(path),
        cwd=normalize_cwd(cwd),
        metadata=metadata,
    )


def load_sessions(tool: str, base_paths: list[Path]) -> list[SessionRecord]:
    records: list[SessionRecord] = []
    seen: set[tuple[str, str]] = set()

    for base in base_paths:
        if not base.exists():
            continue
        for path in find_session_files(base):
            if tool == "codex" and path.suffix == ".jsonl":
                rec = extract_codex_session(path)
                candidates = [rec] if rec is not None else []
            else:
                data = read_json_file(path)
                if data is None:
                    candidates = [
                        SessionRecord(
                            tool=tool,
                            session_id=path.stem,
                            path=path,
                            last_write=safe_mtime(path),
                        )
                    ]
                else:
                    candidates = extract_session_records(tool, path, data)
            if not candidates:
                continue
            for rec in candidates:
                key = (tool, rec.session_id)
                if key in seen:
                    continue
                seen.add(key)
                records.append(rec)
    return records


def add_process_only_records(
    records: list[SessionRecord], processes: list[ProcessInfo]
) -> list[SessionRecord]:
    matched_pids = {
        rec.matched_process.pid for rec in records if rec.matched_process is not None
    }

    for proc in processes:
        if proc.pid in matched_pids:
            continue
        rec = SessionRecord(
            tool=proc.tool,
            session_id=proc.session_ids[0] if proc.session_ids else f"pid-{proc.pid}",
            path=None,
            last_write=None,
            cwd=normalize_cwd(proc.cwd),
            metadata={},
            matched_process=proc,
            score=80,
            status="active",
            reasons=["running process without a matching session file"],
        )
        if proc.tty:
            rec.score += 10
            rec.reasons.append("interactive tty detected")
        records.append(rec)

    return records


def age_minutes(ts: float | None) -> float | None:
    if ts is None:
        return None
    return max(0.0, (dt.datetime.now().timestamp() - ts) / 60.0)


def score_session(
    rec: SessionRecord,
    processes: list[ProcessInfo],
    active_minutes: int,
    recent_hours: int,
) -> None:
    matching = [p for p in processes if p.tool == rec.tool]

    for proc in matching:
        if rec.session_id and rec.session_id in proc.session_ids:
            rec.matched_process = proc
            rec.score += 70
            rec.reasons.append("session id matched process command")
            break

    if rec.matched_process is None and rec.cwd:
        for proc in matching:
            if proc.cwd and normalize_cwd(proc.cwd) == rec.cwd:
                rec.matched_process = proc
                rec.score += 45
                rec.reasons.append("cwd matched running process")
                break

    mins = age_minutes(rec.last_write)
    if mins is not None:
        if mins <= active_minutes:
            rec.score += 35
            rec.reasons.append(f"session file updated within {active_minutes} minutes")
        elif mins <= recent_hours * 60:
            rec.score += 15
            rec.reasons.append(f"session file updated within {recent_hours} hours")

    if rec.matched_process is not None and rec.matched_process.tty:
        rec.score += 10
        rec.reasons.append("interactive tty detected")

    if rec.matched_process is not None:
        runtime = rec.matched_process.etime_seconds
        if runtime is not None and runtime <= active_minutes * 60:
            rec.score += 10
            rec.reasons.append("process started recently")

    if rec.score >= 70:
        rec.status = "active"
    elif rec.score >= 30:
        rec.status = "recent"
    else:
        rec.status = "stale"


def format_ts(ts: float | None) -> str:
    if ts is None:
        return "—"
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def truncate(text: str | None, width: int) -> str:
    if not text:
        return "—"
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def print_table(records: list[SessionRecord]) -> None:
    headers = ["TOOL", "STATUS", "PID", "TTY", "CWD", "SESSION_ID", "LAST_WRITE"]
    rows = []
    for rec in records:
        rows.append(
            [
                rec.tool,
                rec.status,
                str(rec.matched_process.pid) if rec.matched_process else "—",
                rec.matched_process.tty
                if rec.matched_process and rec.matched_process.tty
                else "—",
                truncate(
                    rec.cwd
                    or (rec.matched_process.cwd if rec.matched_process else None),
                    28,
                ),
                truncate(rec.session_id, 22),
                format_ts(rec.last_write),
            ]
        )

    widths = (
        [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
        if rows
        else [len(h) for h in headers]
    )
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        print("  ".join(row[i].ljust(widths[i]) for i in range(len(headers))))


def print_json(records: list[SessionRecord]) -> None:
    payload = []
    for rec in records:
        payload.append(
            {
                "tool": rec.tool,
                "status": rec.status,
                "session_id": rec.session_id,
                "path": str(rec.path) if rec.path is not None else None,
                "last_write": format_ts(rec.last_write),
                "cwd": rec.cwd,
                "score": rec.score,
                "reasons": rec.reasons,
                "process": None
                if rec.matched_process is None
                else {
                    "pid": rec.matched_process.pid,
                    "ppid": rec.matched_process.ppid,
                    "tty": rec.matched_process.tty,
                    "cwd": rec.matched_process.cwd,
                    "etime_seconds": rec.matched_process.etime_seconds,
                    "command": rec.matched_process.command,
                },
            }
        )
    print(json.dumps(payload, indent=2))


def sort_records(records: list[SessionRecord]) -> list[SessionRecord]:
    status_order = {"active": 0, "recent": 1, "stale": 2}
    return sorted(
        records,
        key=lambda r: (
            status_order.get(r.status, 99),
            -(r.score or 0),
            -(r.last_write or 0),
            r.tool,
            r.session_id,
        ),
    )


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="List likely active Codex and OpenCode sessions"
    )
    p.add_argument("--tool", choices=["all", "codex", "opencode"], default="all")
    p.add_argument(
        "--active-minutes",
        type=int,
        default=10,
        help="freshness window for active session writes",
    )
    p.add_argument(
        "--recent-hours",
        type=int,
        default=12,
        help="freshness window for recent session writes",
    )
    p.add_argument("--json", action="store_true", help="emit JSON")
    p.add_argument(
        "--show-reasons", action="store_true", help="show why each row was classified"
    )
    p.add_argument("--codex-dir", type=Path, default=DEFAULT_CODEX_DIR)
    p.add_argument("--opencode-dir", type=Path, action="append", default=[])
    p.add_argument(
        "--include-stale", action="store_true", help="include stale sessions"
    )
    return p


def main() -> int:
    args = build_arg_parser().parse_args()
    opencode_dirs = args.opencode_dir or DEFAULT_OPENCODE_DIRS

    processes = detect_processes()
    records: list[SessionRecord] = []

    if args.tool in ("all", "codex"):
        records.extend(load_sessions("codex", [args.codex_dir]))
    if args.tool in ("all", "opencode"):
        records.extend(load_sessions("opencode", opencode_dirs))

    for rec in records:
        score_session(rec, processes, args.active_minutes, args.recent_hours)

    records = add_process_only_records(records, processes)

    records = sort_records(records)
    if not args.include_stale:
        records = [r for r in records if r.status != "stale"]

    if args.json:
        print_json(records)
    else:
        print_table(records)
        if args.show_reasons:
            print()
            for rec in records:
                print(f"[{rec.tool}] {rec.session_id} -> {rec.status} ({rec.score})")
                for reason in rec.reasons:
                    print(f"  - {reason}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
