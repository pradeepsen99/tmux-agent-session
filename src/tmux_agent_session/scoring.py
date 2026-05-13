from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from .models import ProcessInfo, SessionRecord
from .session_files import normalize_cwd


ANSI_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
FEEDBACK_PROMPT_PATTERNS = [
    re.compile(
        r"\b(?:requir(?:e|es|ing|ed)|needs?)\s+(?:user\s+)?"
        r"(?:feedback|input|response|approval|confirmation)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bwaiting\s+for\s+(?:user\s+)?"
        r"(?:feedback|input|response|approval|confirmation)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:approval|confirmation|permission)\s+(?:required|needed)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:approve|confirm|continue|proceed)\?\s*$", re.IGNORECASE),
    re.compile(r"\bdo you want to\b", re.IGNORECASE),
    re.compile(r"\bpress\s+(?:enter|return|y|n|yes|no)\b", re.IGNORECASE),
]


def strip_ansi(text: str) -> str:
    return ANSI_PATTERN.sub("", text)


def pane_requires_user_feedback(lines: list[str]) -> bool:
    text = "\n".join(strip_ansi(line).strip() for line in lines if line.strip())
    if not text:
        return False
    return any(pattern.search(text) for pattern in FEEDBACK_PROMPT_PATTERNS)


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


def mark_feedback_required(
    records: list[SessionRecord],
    preview_callback: Callable[[SessionRecord, int], list[str]],
    limit: int = 30,
) -> None:
    records_by_pane_id: dict[str, SessionRecord] = {}
    for rec in records:
        if rec.tmux_pane is None or rec.tmux_pane.pane_id in records_by_pane_id:
            continue
        records_by_pane_id[rec.tmux_pane.pane_id] = rec

    if not records_by_pane_id:
        return

    previews_by_pane_id: dict[str, list[str]] = {}
    max_workers = min(8, len(records_by_pane_id))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(preview_callback, rec, limit): pane_id
            for pane_id, rec in records_by_pane_id.items()
        }
        for future, pane_id in futures.items():
            previews_by_pane_id[pane_id] = future.result()

    for rec in records:
        if rec.tmux_pane is None:
            continue
        if not pane_requires_user_feedback(
            previews_by_pane_id.get(rec.tmux_pane.pane_id, [])
        ):
            continue
        rec.requires_user_feedback = True
        rec.status = "waiting"
        reason = "tmux pane appears to be waiting for user feedback"
        if reason not in rec.reasons:
            rec.reasons.append(reason)


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


def sort_records(records: list[SessionRecord]) -> list[SessionRecord]:
    status_order = {"waiting": 0, "active": 1, "recent": 2, "stale": 3}
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
