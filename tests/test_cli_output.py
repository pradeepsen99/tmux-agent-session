from __future__ import annotations

import json

from tmux_agent_session import cli


def make_record() -> cli.SessionRecord:
    proc = cli.ProcessInfo(
        pid=10,
        ppid=1,
        tty="ttys001",
        etime_seconds=61,
        cwd="/tmp/project",
        command="codex --session abc123",
        tool="codex",
        session_ids=["abc123"],
    )
    pane = cli.TmuxPane(
        session_name="work",
        window_index="1",
        window_name="editor",
        pane_index="2",
        pane_id="%3",
        pane_tty="ttys001",
        pane_current_path="/tmp/project",
    )
    return cli.SessionRecord(
        tool="codex",
        session_id="abc123",
        path=None,
        last_write=0,
        cwd="/tmp/project",
        metadata={"model": "gpt-5", "summary": "Investigate issue"},
        matched_process=proc,
        tmux_pane=pane,
        score=100,
        status="active",
        reasons=["session id matched process command"],
    )


def test_print_table_outputs_headers_and_row(capsys) -> None:
    cli.print_table([make_record()])
    out = capsys.readouterr().out

    assert "TOOL" in out
    assert "STATUS" in out
    assert "abc123" in out
    assert "work:1.2" in out
    assert "gpt-5" in out
    assert "Investigate" in out


def test_print_json_outputs_process_and_tmux_blocks(capsys) -> None:
    cli.print_json([make_record()])
    payload = json.loads(capsys.readouterr().out)

    assert payload[0]["tool"] == "codex"
    assert payload[0]["process"]["pid"] == 10
    assert payload[0]["tmux"]["target"] == "work:1.2"
    assert payload[0]["reasons"] == ["session id matched process command"]


def test_append_detail_wraps_multiline_values() -> None:
    lines: list[str] = []
    cli.append_detail(lines, "Summary", "word " * 10, 20)

    assert lines
    assert lines[0].startswith("Summary: ")
    assert len(lines) > 1


def test_build_picker_details_includes_core_fields() -> None:
    lines = cli.build_picker_details(make_record(), 80)

    joined = "\n".join(lines)
    assert "Session: abc123" in joined
    assert "CWD: /tmp/project" in joined
    assert "Tmux: work:1.2 | editor | ttys001" in joined
    assert "Process: pid 10 | ttys001 | 1m 1s" in joined
    assert "Model: gpt-5" in joined


def test_build_picker_details_includes_pane_preview() -> None:
    lines = cli.build_picker_details(
        make_record(), 80, pane_preview=["$ ls", "README.md"]
    )

    joined = "\n".join(lines)
    assert "Preview: $ ls" in joined
    assert "README.md" in joined
