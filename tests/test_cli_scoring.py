from __future__ import annotations

import datetime as dt
from pathlib import Path

from tmux_agent_session import cli


def make_process(
    *,
    pid: int = 10,
    tool: str = "codex",
    tty: str | None = "ttys001",
    cwd: str | None = "/tmp/repo",
    session_ids: list[str] | None = None,
    etime_seconds: int | None = 30,
) -> cli.ProcessInfo:
    return cli.ProcessInfo(
        pid=pid,
        ppid=1,
        tty=tty,
        etime_seconds=etime_seconds,
        cwd=cwd,
        command=tool,
        tool=tool,
        session_ids=session_ids or [],
    )


def make_record(
    *,
    tool: str = "codex",
    session_id: str = "session-1",
    cwd: str | None = None,
    minutes_ago: int | None = None,
) -> cli.SessionRecord:
    last_write = None
    if minutes_ago is not None:
        last_write = dt.datetime.now().timestamp() - (minutes_ago * 60)
    return cli.SessionRecord(
        tool=tool,
        session_id=session_id,
        path=None,
        last_write=last_write,
        cwd=cwd,
    )


def test_age_minutes_handles_none_and_past_timestamp() -> None:
    now = dt.datetime.now().timestamp()
    assert cli.age_minutes(None) is None
    assert 4.9 <= cli.age_minutes(now - 300) <= 5.1


def test_score_session_marks_active_for_session_id_match() -> None:
    rec = make_record(session_id="abc123", minutes_ago=1)
    proc = make_process(session_ids=["abc123"])

    cli.score_session(rec, [proc], active_minutes=10, recent_hours=12)

    assert rec.status == "active"
    assert rec.matched_process == proc
    assert "session id matched process command" in rec.reasons
    assert "interactive tty detected" in rec.reasons
    assert "process started recently" in rec.reasons


def test_score_session_matches_by_cwd_and_marks_recent(tmp_path: Path) -> None:
    cwd = str((tmp_path / "repo").resolve())
    rec = make_record(cwd=cwd, minutes_ago=30)
    proc = make_process(session_ids=[], cwd=cwd, tty=None, etime_seconds=3600)

    cli.score_session(rec, [proc], active_minutes=10, recent_hours=12)

    assert rec.status == "recent"
    assert rec.matched_process == proc
    assert "cwd matched running process" in rec.reasons
    assert "session file updated within 12 hours" in rec.reasons


def test_score_session_stays_stale_without_matching_signals() -> None:
    rec = make_record(cwd="/tmp/other", minutes_ago=60 * 24)
    proc = make_process(cwd="/tmp/repo", tty=None, session_ids=[], etime_seconds=7200)

    cli.score_session(rec, [proc], active_minutes=10, recent_hours=12)

    assert rec.status == "stale"
    assert rec.matched_process is None


def test_add_process_only_records_adds_only_unmatched_processes() -> None:
    matched_proc = make_process(pid=1, session_ids=["session-1"])
    unmatched_proc = make_process(pid=2, session_ids=["session-2"])
    records = [
        cli.SessionRecord(
            tool="codex",
            session_id="session-1",
            path=None,
            last_write=None,
            matched_process=matched_proc,
        )
    ]

    result = cli.add_process_only_records(records, [matched_proc, unmatched_proc])

    assert len(result) == 2
    extra = result[-1]
    assert extra.session_id == "session-2"
    assert extra.status == "active"
    assert extra.score == 90
    assert "running process without a matching session file" in extra.reasons


def test_attach_tmux_panes_matches_process_tty() -> None:
    proc = make_process(tty="ttys001")
    rec = cli.SessionRecord(
        tool="codex",
        session_id="session-1",
        path=None,
        last_write=None,
        matched_process=proc,
    )
    pane = cli.TmuxPane(
        session_name="work",
        window_index="1",
        window_name="editor",
        pane_index="0",
        pane_id="%1",
        pane_tty="ttys001",
    )

    cli.attach_tmux_panes([rec], [pane])

    assert rec.tmux_pane == pane


def test_sort_records_orders_by_status_score_last_write_tool_and_id() -> None:
    stale = make_record(tool="opencode", session_id="z", minutes_ago=120)
    stale.status = "stale"
    stale.score = 1

    recent = make_record(tool="opencode", session_id="b", minutes_ago=30)
    recent.status = "recent"
    recent.score = 20

    active_low = make_record(tool="opencode", session_id="c", minutes_ago=5)
    active_low.status = "active"
    active_low.score = 80

    active_high = make_record(tool="codex", session_id="a", minutes_ago=1)
    active_high.status = "active"
    active_high.score = 100

    sorted_records = cli.sort_records([stale, recent, active_low, active_high])

    assert [rec.session_id for rec in sorted_records] == ["a", "c", "b", "z"]
