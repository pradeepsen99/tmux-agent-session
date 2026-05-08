from __future__ import annotations

import datetime as dt

from .models import ProcessInfo, SessionRecord
from .session_files import normalize_cwd


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
