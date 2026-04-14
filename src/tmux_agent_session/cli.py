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
import curses
import datetime as dt
import json
import re
import shlex
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import __version__


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
class TmuxPane:
    session_name: str
    window_index: str
    window_name: str
    pane_index: str
    pane_id: str
    pane_tty: str | None
    pane_current_path: str | None = None


@dataclass
class SessionRecord:
    tool: str
    session_id: str
    path: Path | None
    last_write: float | None
    cwd: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    matched_process: ProcessInfo | None = None
    tmux_pane: TmuxPane | None = None
    score: int = 0
    status: str = "stale"
    reasons: list[str] = field(default_factory=list)


SESSION_ID_PATTERNS = [
    re.compile(r"(?:--session|session_id|session)\s*[= ]\s*([A-Za-z0-9._:-]{6,})"),
    re.compile(r"\b([a-f0-9]{16,64})\b"),
]

# Ranked by utility in the picker: these fields are the most useful for deciding
# which session to jump into without having to inspect the backing files.
PICKER_METADATA_PRIMARY = [
    ("Model", ("model",)),
    ("Summary", ("summary", "title")),
    ("Approval", ("approval_policy",)),
    ("Activity", ("timestamp", "updated_at", "created_at")),
    ("Origin", ("originator", "source")),
]
PICKER_METADATA_SECONDARY = [
    ("Provider", ("model_provider",)),
    ("CLI", ("cli_version",)),
    ("Style", ("personality",)),
    ("State", ("status",)),
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


def detect_tmux_panes() -> list[TmuxPane]:
    output = run_command(
        [
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{session_name}\t#{window_index}\t#{window_name}\t#{pane_index}\t#{pane_id}\t#{pane_tty}\t#{pane_current_path}",
        ]
    )
    panes: list[TmuxPane] = []
    for raw_line in output.splitlines():
        line = raw_line.rstrip("\n")
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        (
            session_name,
            window_index,
            window_name,
            pane_index,
            pane_id,
            pane_tty,
            pane_current_path,
        ) = parts
        panes.append(
            TmuxPane(
                session_name=session_name,
                window_index=window_index,
                window_name=window_name,
                pane_index=pane_index,
                pane_id=pane_id,
                pane_tty=normalize_tty(pane_tty),
                pane_current_path=normalize_cwd(pane_current_path),
            )
        )
    return panes


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


def attach_tmux_panes(records: list[SessionRecord], panes: list[TmuxPane]) -> None:
    panes_by_tty = {pane.pane_tty: pane for pane in panes if pane.pane_tty is not None}
    for rec in records:
        if rec.matched_process is None:
            continue
        rec.tmux_pane = panes_by_tty.get(normalize_tty(rec.matched_process.tty))


def tmux_target(rec: SessionRecord) -> str:
    if rec.tmux_pane is None:
        return "—"
    return (
        f"{rec.tmux_pane.session_name}:{rec.tmux_pane.window_index}."
        f"{rec.tmux_pane.pane_index}"
    )


def focus_tmux_pane(rec: SessionRecord) -> bool:
    if rec.tmux_pane is None:
        return False

    commands = [
        ["tmux", "switch-client", "-t", rec.tmux_pane.session_name],
        [
            "tmux",
            "select-window",
            "-t",
            f"{rec.tmux_pane.session_name}:{rec.tmux_pane.window_index}",
        ],
        ["tmux", "select-pane", "-t", rec.tmux_pane.pane_id],
    ]
    for cmd in commands:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return False
    return True


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


def format_iso_ts(raw: str) -> str | None:
    text = raw.strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except ValueError:
        return text


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if sec or not parts:
        parts.append(f"{sec}s")
    return " ".join(parts)


def truncate(text: str | None, width: int) -> str:
    if not text:
        return "—"
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def pad(text: str | None, width: int) -> str:
    return truncate(text, width).ljust(width)


def metadata_text(key: str, value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if key in {"summary", "title"} and text.lower() in {"none", "null"}:
            return None
        if key in {"timestamp", "updated_at", "created_at"}:
            return format_iso_ts(text)
        return text
    if isinstance(value, (int, float, bool)):
        return str(value)
    return None


def first_metadata_value(rec: SessionRecord, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        text = metadata_text(key, rec.metadata.get(key))
        if text:
            return text
    return None


def joined_metadata_value(rec: SessionRecord, keys: tuple[str, ...]) -> str | None:
    values: list[str] = []
    for key in keys:
        text = metadata_text(key, rec.metadata.get(key))
        if text and text not in values:
            values.append(text)
    if not values:
        return None
    return " / ".join(values)


def picker_metadata_items(rec: SessionRecord, limit: int = 5) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    used_labels: set[str] = set()

    for label, keys in PICKER_METADATA_PRIMARY:
        value = (
            joined_metadata_value(rec, keys)
            if label == "Origin"
            else first_metadata_value(rec, keys)
        )
        if value:
            items.append((label, value))
            used_labels.add(label)

    for label, keys in PICKER_METADATA_SECONDARY:
        if len(items) >= limit or label in used_labels:
            break
        value = first_metadata_value(rec, keys)
        if value:
            items.append((label, value))

    return items[:limit]


def display_cwd(rec: SessionRecord) -> str | None:
    return rec.cwd or (rec.matched_process.cwd if rec.matched_process else None)


def display_model(rec: SessionRecord) -> str | None:
    model = first_metadata_value(rec, ("model",))
    if model:
        return model
    provider = first_metadata_value(rec, ("model_provider",))
    return provider


def print_table(records: list[SessionRecord]) -> None:
    headers = [
        "TOOL",
        "STATUS",
        "PID",
        "TTY",
        "TARGET",
        "CWD",
        "SESSION_ID",
        "LAST_WRITE",
    ]
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
                tmux_target(rec),
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
                "metadata": rec.metadata,
                "score": rec.score,
                "reasons": rec.reasons,
                "tmux": None
                if rec.tmux_pane is None
                else {
                    "session_name": rec.tmux_pane.session_name,
                    "window_index": rec.tmux_pane.window_index,
                    "window_name": rec.tmux_pane.window_name,
                    "pane_index": rec.tmux_pane.pane_index,
                    "pane_id": rec.tmux_pane.pane_id,
                    "pane_tty": rec.tmux_pane.pane_tty,
                    "target": tmux_target(rec),
                },
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


def build_records(args: argparse.Namespace) -> list[SessionRecord]:
    opencode_dirs = args.opencode_dir or DEFAULT_OPENCODE_DIRS

    processes = detect_processes()
    panes = detect_tmux_panes()
    records: list[SessionRecord] = []

    if args.tool in ("all", "codex"):
        records.extend(load_sessions("codex", [args.codex_dir]))
    if args.tool in ("all", "opencode"):
        records.extend(load_sessions("opencode", opencode_dirs))

    for rec in records:
        score_session(rec, processes, args.active_minutes, args.recent_hours)

    records = add_process_only_records(records, processes)
    attach_tmux_panes(records, panes)
    records = sort_records(records)

    if not args.include_stale:
        records = [r for r in records if r.status != "stale"]

    return records


def move_selection(current: int | None, selectable: list[int], step: int) -> int | None:
    if not selectable:
        return None
    if current is None or current not in selectable:
        return selectable[0]

    position = selectable.index(current)
    position = max(0, min(len(selectable) - 1, position + step))
    return selectable[position]


def render_picker_line(rec: SessionRecord, width: int) -> str:
    model_width = 12
    target_width = 10
    tool_width = 8
    status_width = 8
    session_width = 16
    fixed = tool_width + status_width + target_width + model_width + session_width + 10
    cwd_width = max(12, width - fixed)
    return "  ".join(
        [
            pad(rec.status, status_width),
            pad(rec.tool, tool_width),
            pad(tmux_target(rec), target_width),
            pad(display_model(rec), model_width),
            pad(rec.session_id, session_width),
            truncate(display_cwd(rec), cwd_width),
        ]
    )[:width]


def render_picker_header(width: int) -> str:
    return render_picker_line(
        SessionRecord(
            tool="TOOL",
            session_id="SESSION",
            path=None,
            last_write=None,
            cwd="CWD",
            metadata={"model": "MODEL"},
            status="STATUS",
        ),
        width,
    )


def safe_addnstr(
    stdscr: curses.window, y: int, x: int, text: str, width: int, attr: int = 0
) -> None:
    if width <= 0:
        return
    try:
        stdscr.addnstr(y, x, text, width, attr)
    except curses.error:
        pass


def append_detail(lines: list[str], label: str, value: str | None, width: int) -> None:
    if not value or width <= 0:
        return
    prefix = f"{label}: "
    body_width = max(8, width - len(prefix))
    wrapped = textwrap.wrap(value, body_width) or [value]
    for index, chunk in enumerate(wrapped):
        if index == 0:
            lines.append(f"{prefix}{chunk}")
        else:
            lines.append(" " * len(prefix) + chunk)


def build_picker_details(rec: SessionRecord, width: int) -> list[str]:
    lines: list[str] = []
    append_detail(lines, "Session", rec.session_id, width)
    append_detail(lines, "CWD", display_cwd(rec), width)

    if rec.tmux_pane is not None:
        tmux_bits = [tmux_target(rec)]
        if rec.tmux_pane.window_name:
            tmux_bits.append(rec.tmux_pane.window_name)
        if rec.tmux_pane.pane_tty:
            tmux_bits.append(rec.tmux_pane.pane_tty)
        append_detail(lines, "Tmux", " | ".join(tmux_bits), width)

    if rec.matched_process is not None:
        process_bits = [f"pid {rec.matched_process.pid}"]
        if rec.matched_process.tty:
            process_bits.append(rec.matched_process.tty)
        runtime = format_duration(rec.matched_process.etime_seconds)
        if runtime != "—":
            process_bits.append(runtime)
        append_detail(lines, "Process", " | ".join(process_bits), width)

    file_bits: list[str] = []
    if rec.last_write is not None:
        file_bits.append(format_ts(rec.last_write))
    if rec.path is not None:
        file_bits.append(str(rec.path))
    append_detail(lines, "File", " | ".join(file_bits), width)

    for label, value in picker_metadata_items(rec):
        append_detail(lines, label, value, width)

    if not lines:
        lines.append("No additional metadata for this session.")
    return lines


def run_picker(records: list[SessionRecord]) -> int:
    selectable = [i for i, rec in enumerate(records) if rec.tmux_pane is not None]

    def inner(stdscr: curses.window) -> int:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.keypad(True)
        try:
            curses.use_default_colors()
        except curses.error:
            pass

        status_attrs = {
            "active": curses.A_NORMAL,
            "recent": curses.A_NORMAL,
            "stale": curses.A_DIM,
        }
        if curses.has_colors():
            try:
                curses.start_color()
                curses.init_pair(1, curses.COLOR_GREEN, -1)
                curses.init_pair(2, curses.COLOR_YELLOW, -1)
                curses.init_pair(3, curses.COLOR_CYAN, -1)
                status_attrs = {
                    "active": curses.color_pair(1),
                    "recent": curses.color_pair(2),
                    "stale": curses.color_pair(3) | curses.A_DIM,
                }
            except curses.error:
                pass

        highlight_attr = curses.A_REVERSE
        dim_attr = curses.A_DIM
        title_attr = curses.A_BOLD
        header_attr = curses.A_BOLD
        selected = selectable[0] if selectable else None
        top = 0
        message = "Enter focus  j/k move  q/Esc cancel"

        while True:
            height, width = stdscr.getmaxyx()
            detail_height = 0 if height < 14 else min(9, max(6, height // 3))
            footer_y = max(0, height - 1)
            row_start = 2
            divider_y = None
            if detail_height:
                divider_y = max(row_start + 1, footer_y - detail_height - 1)
                list_height = max(1, divider_y - row_start)
            else:
                list_height = max(1, footer_y - row_start)
            if selected is not None:
                if selected < top:
                    top = selected
                elif selected >= top + list_height:
                    top = selected - list_height + 1
            else:
                top = 0

            stdscr.erase()
            selected_rec = records[selected] if selected is not None else None
            focusable_count = len(selectable)
            title = f"Session Picker  {len(records)} shown  {focusable_count} focusable"
            if selected_rec is not None:
                title = f"{title}  Selected: {selected_rec.tool} {selected_rec.status}"
            safe_addnstr(
                stdscr, 0, 0, truncate(title, width).ljust(width), width, title_attr
            )
            safe_addnstr(
                stdscr,
                1,
                0,
                render_picker_header(width).ljust(width),
                width,
                header_attr,
            )
            for row, record_index in enumerate(
                range(top, min(len(records), top + list_height)), start=1
            ):
                rec = records[record_index]
                row_y = row_start + row - 1
                attr = status_attrs.get(rec.status, curses.A_NORMAL)
                if rec.tmux_pane is None:
                    attr |= dim_attr
                elif record_index == selected:
                    attr |= highlight_attr
                safe_addnstr(
                    stdscr, row_y, 0, render_picker_line(rec, width), width, attr
                )

            if divider_y is not None:
                safe_addnstr(
                    stdscr, divider_y, 0, "-" * max(0, width - 1), width, dim_attr
                )
                if selected_rec is not None:
                    detail_lines = build_picker_details(
                        selected_rec, max(20, width - 1)
                    )
                else:
                    detail_lines = [
                        "No focusable tmux target for the visible sessions."
                    ]
                for index, line in enumerate(
                    detail_lines[: footer_y - divider_y - 1], start=1
                ):
                    safe_addnstr(stdscr, divider_y + index, 0, line.ljust(width), width)

            if not selectable:
                message = "No tmux-mapped sessions available. Press q to exit."
            footer = message
            safe_addnstr(
                stdscr,
                max(0, height - 1),
                0,
                truncate(footer, width).ljust(width),
                width,
                dim_attr,
            )
            stdscr.refresh()

            key = stdscr.getch()
            if key in (ord("q"), 27):
                return 1
            if key in (curses.KEY_UP, ord("k")):
                selected = move_selection(selected, selectable, -1)
                continue
            if key in (curses.KEY_DOWN, ord("j")):
                selected = move_selection(selected, selectable, 1)
                continue
            if key in (10, 13, curses.KEY_ENTER):
                if selected is None:
                    message = "No focusable tmux target for the visible sessions."
                    continue
                if focus_tmux_pane(records[selected]):
                    return 0
                message = f"Failed to focus {tmux_target(records[selected])}."

    try:
        return curses.wrapper(inner)
    except curses.error as exc:
        print(f"picker failed: {exc}", file=sys.stderr)
        return 1


def build_arg_parser() -> argparse.ArgumentParser:
    kwargs = {"description": "List likely active Codex and OpenCode sessions"}
    if Path(sys.argv[0]).name == "__main__.py" and __package__ == "tmux_agent_session":
        kwargs["prog"] = "python -m tmux_agent_session"
    p = argparse.ArgumentParser(**kwargs)
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
        "--pick",
        action="store_true",
        help="open an interactive picker and focus the selected tmux pane",
    )
    p.add_argument(
        "--show-reasons", action="store_true", help="show why each row was classified"
    )
    p.add_argument("--codex-dir", type=Path, default=DEFAULT_CODEX_DIR)
    p.add_argument("--opencode-dir", type=Path, action="append", default=[])
    p.add_argument(
        "--include-stale", action="store_true", help="include stale sessions"
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.pick and args.json:
        parser.error("--pick cannot be combined with --json")

    records = build_records(args)

    if args.pick:
        return run_picker(records)
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
