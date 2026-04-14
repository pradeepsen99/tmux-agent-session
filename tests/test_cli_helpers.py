from __future__ import annotations

from pathlib import Path
import subprocess

from tmux_agent_session import cli


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

    assert cli.display_cwd(rec) == "/tmp/from-process"
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


def test_move_selection_handles_empty_unknown_and_bounds() -> None:
    selectable = [2, 4, 6]
    assert cli.move_selection(None, selectable, 1) == 2
    assert cli.move_selection(4, selectable, 1) == 6
    assert cli.move_selection(4, selectable, -1) == 2
    assert cli.move_selection(6, selectable, 1) == 6
    assert cli.move_selection(9, selectable, 1) == 2
    assert cli.move_selection(1, [], 1) is None


def test_render_picker_line_and_header_include_expected_columns() -> None:
    rec = cli.SessionRecord(
        tool="codex",
        session_id="session-1",
        path=None,
        last_write=None,
        cwd="/tmp/project",
        metadata={"model": "gpt-5"},
        status="active",
    )

    line = cli.render_picker_line(rec, 120)
    header = cli.render_picker_header(120)

    assert "active" in line
    assert "codex" in line
    assert "gpt-5" in line
    assert "STATUS" in header
    assert "TOOL" in header


def test_picker_split_widths_uses_sidebar_only_when_terminal_is_wide_enough() -> None:
    assert cli.picker_split_widths(78) == (78, 0)

    list_width, sidebar_width = cli.picker_split_widths(100)
    assert list_width >= 44
    assert sidebar_width >= 32
    assert list_width + sidebar_width + 2 == 100
