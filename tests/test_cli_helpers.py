from __future__ import annotations

from pathlib import Path
import subprocess

from tmux_agent_session import cli
from tmux_agent_session import processes


def test_parse_etime_to_seconds_supports_common_formats() -> None:
    assert cli.parse_etime_to_seconds("01:02") == 62
    assert cli.parse_etime_to_seconds("1:02:03") == 3723
    assert cli.parse_etime_to_seconds("2-03:04:05") == 183845


def test_parse_etime_to_seconds_rejects_blank_and_invalid() -> None:
    assert cli.parse_etime_to_seconds("") is None
    assert cli.parse_etime_to_seconds("12") is None


def test_normalize_tty_removes_dev_prefix() -> None:
    assert cli.normalize_tty("/dev/ttys001") == "ttys001"
    assert cli.normalize_tty("pts/1") == "pts/1"
    assert cli.normalize_tty(None) is None


def test_extract_session_ids_dedupes_and_finds_flags_and_hex_ids() -> None:
    command = (
        "codex --session abc123 --session=abc123 other 0123456789abcdef0123456789abcdef"
    )
    assert cli.extract_session_ids(command) == [
        "abc123",
        "0123456789abcdef0123456789abcdef",
    ]


def test_get_cwds_batches_lsof_lookup(monkeypatch) -> None:
    commands: list[list[str]] = []

    monkeypatch.setattr(processes, "_get_proc_cwd", lambda _pid: None)

    def fake_run_command(cmd: list[str]) -> str:
        commands.append(cmd)
        return "p123\nfcwd\nn/tmp/one\np456\nfcwd\nn/tmp/two\n"

    monkeypatch.setattr(processes, "run_command", fake_run_command)

    assert processes.get_cwds([123, 456, 123]) == {
        123: "/tmp/one",
        456: "/tmp/two",
    }
    assert commands == [["lsof", "-a", "-p", "123,456", "-d", "cwd", "-Fn"]]


def test_detect_processes_defers_cwd_lookup_by_default(monkeypatch) -> None:
    ps_output = " 123 1 ?? 01:02 codex --session abc123\n"
    monkeypatch.setattr(processes, "run_command", lambda _cmd: ps_output)
    monkeypatch.setattr(
        processes,
        "get_cwd",
        lambda _pid: (_ for _ in ()).throw(AssertionError("cwd should be deferred")),
    )

    procs = processes.detect_processes()

    assert len(procs) == 1
    assert procs[0].cwd is None
    assert procs[0].tty is None


def test_normalize_cwd_expands_home_and_resolves_path(tmp_path: Path) -> None:
    child = tmp_path / "repo"
    child.mkdir()
    assert cli.normalize_cwd(str(child)) == str(child.resolve())
    assert cli.normalize_cwd(None) is None


def test_format_iso_ts_formats_zulu_and_preserves_invalid() -> None:
    assert cli.format_iso_ts("2024-01-02T03:04:05Z") == "2024-01-02 03:04:05"
    assert cli.format_iso_ts("not-a-timestamp") == "not-a-timestamp"
    assert cli.format_iso_ts("   ") is None


def test_format_duration_covers_none_and_compound_values() -> None:
    assert cli.format_duration(None) == "—"
    assert cli.format_duration(0) == "0s"
    assert cli.format_duration(61) == "1m 1s"
    assert cli.format_duration(90061) == "1d 1h 1m 1s"


def test_truncate_and_pad_handle_empty_and_long_values() -> None:
    assert cli.truncate(None, 5) == "—"
    assert cli.truncate("abcdef", 4) == "abc…"
    assert cli.pad("abc", 5) == "abc  "


def test_metadata_helpers_filter_and_format_values() -> None:
    rec = cli.SessionRecord(
        tool="codex",
        session_id="session-1",
        path=None,
        last_write=None,
        metadata={
            "summary": "Investigate bug",
            "title": "none",
            "timestamp": "2024-01-02T03:04:05Z",
            "model": "gpt-5",
            "originator": "user",
            "source": "cli",
        },
    )

    assert cli.metadata_text("summary", "Investigate bug") == "Investigate bug"
    assert cli.metadata_text("title", "none") is None
    assert cli.first_metadata_value(rec, ("missing", "model")) == "gpt-5"
    assert cli.joined_metadata_value(rec, ("originator", "source")) == "user / cli"


def test_picker_metadata_items_prefers_primary_fields() -> None:
    rec = cli.SessionRecord(
        tool="codex",
        session_id="session-1",
        path=None,
        last_write=None,
        metadata={
            "model": "gpt-5",
            "summary": "Bug hunt",
            "approval_policy": "on-request",
            "timestamp": "2024-01-02T03:04:05Z",
            "originator": "user",
            "source": "cli",
            "model_provider": "openai",
        },
    )

    assert cli.picker_metadata_items(rec) == [
        ("Model", "gpt-5"),
        ("Summary", "Bug hunt"),
        ("Approval", "on-request"),
        ("Activity", "2024-01-02 03:04:05"),
        ("Origin", "user / cli"),
    ]


def test_display_helpers_and_tmux_target_use_best_available_values() -> None:
    proc = cli.ProcessInfo(
        pid=10,
        ppid=1,
        tty="ttys001",
        etime_seconds=30,
        cwd="/tmp/from-process",
        command="codex",
        tool="codex",
    )
    pane = cli.TmuxPane(
        session_name="work",
        window_index="1",
        window_name="editor",
        pane_index="2",
        pane_id="%3",
        pane_tty="ttys001",
    )
    rec = cli.SessionRecord(
        tool="codex",
        session_id="session-1",
        path=None,
        last_write=None,
        metadata={"model_provider": "openai"},
        matched_process=proc,
        tmux_pane=pane,
    )

    assert cli.display_cwd(rec) == "from-process"
    assert cli.display_model(rec) == "openai"
    assert cli.tmux_target(rec) == "work:1.2"


def test_capture_tmux_pane_preview_uses_pane_id_and_limits_output(monkeypatch) -> None:
    pane = cli.TmuxPane(
        session_name="work",
        window_index="1",
        window_name="editor",
        pane_index="2",
        pane_id="%3",
        pane_tty="ttys001",
    )
    rec = cli.SessionRecord(
        tool="codex",
        session_id="session-1",
        path=None,
        last_write=None,
        tmux_pane=pane,
    )

    def fake_run(cmd, capture_output, text):
        assert cmd == [
            "tmux",
            "capture-pane",
            "-p",
            "-e",
            "-t",
            "%3",
            "-S",
            "-3",
        ]
        assert capture_output is True
        assert text is True
        return subprocess.CompletedProcess(cmd, 0, stdout="one\ntwo\nthree\nfour\n")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli.capture_tmux_pane_preview(rec, limit=3) == ["two", "three", "four"]


def test_capture_tmux_pane_preview_returns_empty_on_failure_or_missing_pane(
    monkeypatch,
) -> None:
    rec = cli.SessionRecord(
        tool="codex",
        session_id="session-1",
        path=None,
        last_write=None,
    )
    assert cli.capture_tmux_pane_preview(rec) == []

    pane = cli.TmuxPane(
        session_name="work",
        window_index="1",
        window_name="editor",
        pane_index="2",
        pane_id="%3",
        pane_tty="ttys001",
    )
    rec.tmux_pane = pane

    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, stdout=""),
    )

    assert cli.capture_tmux_pane_preview(rec) == []

    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("tmux missing")),
    )

    assert cli.capture_tmux_pane_preview(rec) == []


def test_move_selection_handles_empty_unknown_and_bounds() -> None:
    selectable = [2, 4, 6]
    assert cli.move_selection(None, selectable, 1) == 2
    assert cli.move_selection(4, selectable, 1) == 6
    assert cli.move_selection(4, selectable, -1) == 2
    assert cli.move_selection(6, selectable, 1) == 6
    assert cli.move_selection(9, selectable, 1) == 2
    assert cli.move_selection(1, [], 1) is None


def test_picker_row_cells_include_expected_columns() -> None:
    rec = cli.SessionRecord(
        tool="codex",
        session_id="session-1",
        path=None,
        last_write=None,
        cwd="/tmp/project",
        metadata={"model": "gpt-5"},
        status="active",
    )

    cells = cli.picker_row_cells(rec)

    assert cells == (
        "active",
        "codex",
        "—",
        "gpt-5",
        "project",
    )
    assert cli.PICKER_METADATA_PRIMARY[0][0] == "Model"


def test_first_focusable_index_prefers_tmux_backed_records() -> None:
    plain = cli.SessionRecord(
        tool="codex",
        session_id="plain",
        path=None,
        last_write=None,
    )
    focusable = cli.SessionRecord(
        tool="codex",
        session_id="focusable",
        path=None,
        last_write=None,
        tmux_pane=cli.TmuxPane(
            session_name="work",
            window_index="1",
            window_name="editor",
            pane_index="2",
            pane_id="%3",
            pane_tty="ttys001",
        ),
    )

    assert cli.first_focusable_index([plain, focusable]) == 1
    assert cli.first_focusable_index([plain]) == 0
    assert cli.first_focusable_index([]) is None


def test_rich_picker_row_cells_dim_non_focusable_rows() -> None:
    rec = cli.SessionRecord(
        tool="codex",
        session_id="session-1",
        path=None,
        last_write=None,
        cwd="/tmp/project",
        metadata={"model": "gpt-5"},
        status="active",
    )

    cells = cli.rich_picker_row_cells(rec)

    assert [cell.plain for cell in cells] == list(cli.picker_row_cells(rec))
    assert str(cells[1].style) == "dim"
