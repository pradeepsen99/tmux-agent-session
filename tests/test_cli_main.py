from __future__ import annotations

import argparse

import pytest

from tmux_agent_session import cli


def test_build_arg_parser_defaults() -> None:
    parser = cli.build_arg_parser()
    args = parser.parse_args([])

    assert args.tool == "all"
    assert args.active_minutes == 10
    assert args.recent_hours == 12
    assert args.json is False
    assert args.pick is False
    assert args.include_stale is False


def test_build_records_filters_stale_and_honors_tool_selection(monkeypatch) -> None:
    load_calls: list[str] = []
    active = cli.SessionRecord(
        tool="codex",
        session_id="active",
        path=None,
        last_write=None,
        status="active",
    )
    stale = cli.SessionRecord(
        tool="opencode",
        session_id="stale",
        path=None,
        last_write=None,
        status="stale",
    )

    monkeypatch.setattr(cli, "detect_processes", lambda: [])
    monkeypatch.setattr(cli, "detect_tmux_panes", lambda: [])

    def fake_load_sessions(tool: str, _paths):
        load_calls.append(tool)
        return [active] if tool == "codex" else [stale]

    monkeypatch.setattr(cli, "load_sessions", fake_load_sessions)
    monkeypatch.setattr(cli, "score_session", lambda rec, *_args: None)
    monkeypatch.setattr(
        cli, "add_process_only_records", lambda records, _processes: records
    )
    monkeypatch.setattr(cli, "attach_tmux_panes", lambda _records, _panes: None)
    monkeypatch.setattr(cli, "sort_records", lambda records: records)

    args = argparse.Namespace(
        tool="all",
        codex_dir=cli.DEFAULT_CODEX_DIR,
        opencode_dir=[],
        active_minutes=10,
        recent_hours=12,
        include_stale=False,
    )

    records = cli.build_records(args)

    assert load_calls == ["codex", "opencode"]
    assert [rec.session_id for rec in records] == ["active"]


def test_build_records_filters_process_only_records_by_tool(monkeypatch) -> None:
    codex_proc = cli.ProcessInfo(
        pid=1,
        ppid=0,
        tty="ttys001",
        etime_seconds=30,
        cwd="/tmp/codex",
        command="codex",
        tool="codex",
    )
    opencode_proc = cli.ProcessInfo(
        pid=2,
        ppid=0,
        tty="ttys002",
        etime_seconds=30,
        cwd="/tmp/opencode",
        command="opencode",
        tool="opencode",
    )

    monkeypatch.setattr(cli, "detect_processes", lambda: [codex_proc, opencode_proc])
    monkeypatch.setattr(cli, "detect_tmux_panes", lambda: [])
    monkeypatch.setattr(cli, "load_sessions", lambda _tool, _paths: [])

    args = argparse.Namespace(
        tool="codex",
        codex_dir=cli.DEFAULT_CODEX_DIR,
        opencode_dir=[],
        active_minutes=10,
        recent_hours=12,
        include_stale=False,
    )

    records = cli.build_records(args)

    assert [rec.tool for rec in records] == ["codex"]
    assert [rec.session_id for rec in records] == ["pid-1"]


def test_main_prints_table_and_reasons(monkeypatch, capsys) -> None:
    rec = cli.SessionRecord(
        tool="codex",
        session_id="session-1",
        path=None,
        last_write=None,
        status="active",
        score=90,
        reasons=["matched by cwd"],
    )

    monkeypatch.setattr(cli, "build_records", lambda _args: [rec])
    monkeypatch.setattr(cli.sys, "argv", ["tas", "--show-reasons"])

    assert cli.main() == 0
    out = capsys.readouterr().out
    assert "TOOL" in out
    assert "[codex] session-1 -> active (90)" in out
    assert "matched by cwd" in out


def test_main_prints_json(monkeypatch, capsys) -> None:
    rec = cli.SessionRecord(
        tool="codex",
        session_id="session-1",
        path=None,
        last_write=None,
    )

    monkeypatch.setattr(cli, "build_records", lambda _args: [rec])
    monkeypatch.setattr(cli.sys, "argv", ["tas", "--json"])

    assert cli.main() == 0
    assert '"session_id": "session-1"' in capsys.readouterr().out


def test_main_dispatches_to_picker(monkeypatch) -> None:
    monkeypatch.setattr(cli, "build_records", lambda _args: [])
    monkeypatch.setattr(cli, "run_picker", lambda records: 7 if records == [] else 1)
    monkeypatch.setattr(cli.sys, "argv", ["tas", "--pick"])

    assert cli.main() == 7


def test_main_rejects_pick_with_json(monkeypatch) -> None:
    monkeypatch.setattr(cli.sys, "argv", ["tas", "--pick", "--json"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2
