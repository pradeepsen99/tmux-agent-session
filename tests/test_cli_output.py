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
    assert "SESSION_ID" not in out
    assert "abc123" not in out
    assert "work:1.2" in out
    assert "project" in out
    assert "/tmp/project" not in out
    assert "gpt-5" in out
    assert "Investigate" in out


def test_print_json_outputs_process_and_tmux_blocks(capsys) -> None:
    cli.print_json([make_record()])
    payload = json.loads(capsys.readouterr().out)

    assert payload[0]["tool"] == "codex"
    assert payload[0]["process"]["pid"] == 10
    assert payload[0]["tmux"]["target"] == "work:1.2"
    assert payload[0]["requires_user_feedback"] is False
    assert payload[0]["reasons"] == ["session id matched process command"]


def test_output_surfaces_feedback_required_state(capsys) -> None:
    rec = make_record()
    rec.status = "waiting"
    rec.requires_user_feedback = True

    cli.print_table([rec])
    table_out = capsys.readouterr().out
    cli.print_json([rec])
    payload = json.loads(capsys.readouterr().out)

    assert "waiting" in table_out
    assert "requires user" in table_out
    assert "feedback" in table_out
    assert payload[0]["requires_user_feedback"] is True


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
    assert "CWD: project" in joined
    assert "Tmux: work:1.2 | editor | ttys001" in joined
    assert "Process: pid 10 | ttys001 | 1m 1s" in joined
    assert "Model: gpt-5" in joined


def test_build_picker_details_includes_feedback_state() -> None:
    rec = make_record()
    rec.requires_user_feedback = True

    lines = cli.build_picker_details(rec, 80)

    assert "Feedback: requires user feedback" in "\n".join(lines)


def test_build_picker_details_includes_conversation_metadata() -> None:
    rec = make_record()
    rec.metadata.update({
        "title": "Fix parser", "project": "My project", "git_branch": "fix/parser",
        "last_user_prompt": "Please fix parsing", "last_message": "Fixed it",
        "last_message_role": "assistant",
    })
    joined = "\n".join(cli.build_picker_details(rec, 80))
    assert "Title: Fix parser" in joined
    assert "Project: My project" in joined
    assert "Git branch: fix/parser" in joined
    assert "Last user prompt: Please fix parsing" in joined
    assert "Last message (assistant): Fixed it" in joined


def test_details_render_message_markup_literally() -> None:
    from rich.console import Console
    rec = make_record()
    rec.metadata["last_message"] = "[red]literal[/red]\nsecond line"
    console = Console(width=120)
    with console.capture() as capture:
        console.print(cli.picker_details_renderable(rec))
    assert "[red]literal[/red]" in capture.get()
    assert "second line" in capture.get()


def test_directory_groups_keep_priority_and_disambiguate_paths() -> None:
    from tmux_agent_session.formatting import group_records_by_directory

    records = [make_record() for _ in range(6)]
    for index, rec in enumerate(records):
        rec.session_id = str(index)
    for rec, cwd in zip(records, ["/z/project", "/tmp/alpha", "/a/project", "/z/project", None, None]):
        rec.cwd = cwd
    records[4].matched_process.cwd = "/tmp/alpha"
    records[5].matched_process = None

    groups = group_records_by_directory(records)

    assert [(label, [records.index(rec) for rec in group]) for label, group in groups] == [
        ("alpha", [1, 4]),
        ("/a/project", [2]),
        ("/z/project", [0, 3]),
        ("Unknown directory", [5]),
    ]
    assert groups[2][1][0] is records[0]
    assert groups[2][1][1] is records[3]
    assert group_records_by_directory([]) == []


def test_print_table_groups_directories_and_renders_labels_literally() -> None:
    from rich.console import Console

    records = [make_record() for _ in range(3)]
    for index, (rec, cwd) in enumerate(zip(records, ["/tmp/zeta", "/tmp/[red]alpha", "/tmp/zeta"])):
        rec.cwd = cwd
        rec.metadata["summary"] = f"row-{index}"
    console = Console(width=160)
    with console.capture() as capture:
        cli.print_table(records, console=console)
    output = capture.get()
    assert "[red]alpha" in output
    assert output.count("zeta") == 1
    assert output.index("row-1") < output.index("row-0") < output.index("row-2")
