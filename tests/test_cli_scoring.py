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


def test_pane_requires_user_feedback_detects_prompt_text() -> None:
    assert cli.pane_requires_user_feedback(
        ["\x1b[31mWaiting for user input\x1b[0m", "Continue?"]
    )
    assert cli.pane_requires_user_feedback(["Session is requiring user feedback"])
    assert cli.pane_requires_user_feedback(["approval required before running command"])
    assert not cli.pane_requires_user_feedback(["tests passed", "idle"])


def test_classify_pane_activity_updates_status_and_reason() -> None:
    rec = make_record(minutes_ago=1)
    rec.status = "active"
    rec.tmux_pane = cli.TmuxPane(
        session_name="work",
        window_index="1",
        window_name="editor",
        pane_index="0",
        pane_id="%1",
        pane_tty="ttys001",
    )

    cli.classify_pane_activity(
        [rec], lambda _rec, _limit: ["Agent needs user feedback"]
    )

    assert rec.status == "waiting"
    assert rec.requires_user_feedback is True
    assert "tmux pane appears to be waiting for user feedback" in rec.reasons


def test_classify_pane_activity_captures_duplicate_panes_once() -> None:
    pane = cli.TmuxPane(
        session_name="work",
        window_index="1",
        window_name="editor",
        pane_index="0",
        pane_id="%1",
        pane_tty="ttys001",
    )
    first = make_record(session_id="session-1", minutes_ago=1)
    second = make_record(session_id="session-2", minutes_ago=1)
    first.status = "active"
    second.status = "active"
    first.tmux_pane = pane
    second.tmux_pane = pane
    calls: list[str] = []

    def preview(rec: cli.SessionRecord, _limit: int) -> list[str]:
        calls.append(rec.session_id)
        return ["Agent needs user feedback"]

    cli.classify_pane_activity([first, second], preview)

    assert calls == ["session-1"]
    assert first.status == "waiting"
    assert second.status == "waiting"


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
    assert extra.metadata["title"] == "Blank Session"


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
    waiting = make_record(tool="opencode", session_id="waiting", minutes_ago=60)
    waiting.status = "waiting"
    waiting.score = 1

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

    sorted_records = cli.sort_records(
        [stale, recent, active_low, active_high, waiting]
    )

    assert [rec.session_id for rec in sorted_records] == [
        "waiting",
        "a",
        "c",
        "b",
        "z",
    ]


def test_build_session_candidates_uses_cwd_only_without_session_id() -> None:
    with_id = make_process(pid=1, tool="claude", cwd="/tmp/repo", session_ids=["abc123def"])
    without_id = make_process(pid=2, tool="claude", cwd="/tmp/other")

    candidates = cli.build_session_candidates([with_id, without_id])["claude"]

    assert candidates.session_ids == frozenset({"abc123def"})
    assert candidates.cwds == frozenset({cli.normalize_cwd("/tmp/other")})


BOX = "─" * 40


def test_pane_is_working_detects_claude_spinner_above_input_box() -> None:
    lines = [
        "⏺ Reading files",
        "✽ Hatching… (16s · ↓ 1.2k tokens · thought for 2s)",
        "",
        BOX,
        "❯ ",
        BOX,
        "  ⏵⏵ auto mode on (shift+tab to cycle)",
    ]

    assert cli.pane_is_working(lines)


def test_pane_is_working_ignores_finished_turn_and_scrollback_spinner() -> None:
    lines = [
        "  ⎿  ✽ Hatching… (16s · ↓ 1.2k tokens)",
        "⏺ All done.",
        "✻ Crunched for 1m 28s · done 5:26 PM",
        BOX,
        "❯ ",
        BOX,
        "  ⏵⏵ auto mode on (shift+tab to cycle)",
    ]

    assert not cli.pane_is_working(lines)


def test_pane_is_working_detects_background_agents_in_footer() -> None:
    lines = [
        "⏺ Agent \"Reuse review\" finished · 1m 40s",
        BOX,
        "❯ ",
        BOX,
        "  ⏺ main",
        "  ◯ general-purpose  Comparing evaluators     1m 45s · ↓ 114.1k tokens",
    ]

    assert cli.pane_is_working(lines)


def test_pane_is_working_detects_interrupt_hint() -> None:
    assert cli.pane_is_working(["• Working (5s • esc to interrupt)"])
    assert not cli.pane_is_working(["› ", "100% context left"])


def test_classify_pane_activity_marks_quiet_active_panes_idle() -> None:
    pane = cli.TmuxPane(
        session_name="work",
        window_index="1",
        window_name="editor",
        pane_index="0",
        pane_id="%1",
        pane_tty="ttys001",
    )
    rec = make_record(minutes_ago=1)
    rec.status = "active"
    rec.tmux_pane = pane

    cli.classify_pane_activity([rec], lambda _rec, _limit: [BOX, "❯ ", BOX])

    assert rec.status == "idle"
    assert rec.requires_user_feedback is False
