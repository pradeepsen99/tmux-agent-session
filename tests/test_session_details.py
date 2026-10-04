from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

from tmux_agent_session.models import SessionRecord
from tmux_agent_session import session_details as details


def record(path: Path | None, tool: str = "codex") -> SessionRecord:
    return SessionRecord(tool=tool, session_id="session-1", path=path, last_write=None)


def test_codex_details_read_title_branch_and_latest_conversation(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions" / "2026"
    sessions.mkdir(parents=True)
    path = sessions / "rollout.jsonl"
    entries = [
        {"type": "session_meta", "payload": {"git": {"branch": "original-branch"}}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Old prompt"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Latest prompt\nwith details"}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "Latest answer"},
        ]}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": "x" * 140000}},
    ]
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n{partial")
    (tmp_path / "session_index.jsonl").write_text(
        json.dumps({"id": "session-1", "thread_name": "Old title"}) + "\n" +
        json.dumps({"id": "session-1", "thread_name": "Renamed title"})
    )
    result = details.load_session_details(record(path))
    assert result == {
        "title": "Renamed title", "recorded_branch": "original-branch",
        "last_user_prompt": "Latest prompt\nwith details",
        "last_message": "Latest answer", "last_message_role": "assistant",
    }


def test_codex_skips_injected_context_and_tool_output(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    def message(role: str, text: str) -> dict:
        return {"type": "response_item", "payload": {"type": "message", "role": role,
                "content": [{"type": "input_text", "text": text}]}}
    path.write_text("\n".join(json.dumps(entry) for entry in [
        message("user", "Real prompt"), message("user", "# AGENTS.md instructions for /tmp"),
        message("developer", "Internal instructions"),
        {"type": "response_item", "payload": {"type": "function_call_output", "output": "Tool output"}},
    ]))
    result = details.load_session_details(record(path))
    assert result["last_user_prompt"] == "Real prompt"
    assert result["last_message"] == "Real prompt"


def test_opencode_details_read_ordered_text_parts_for_selected_session(tmp_path: Path) -> None:
    path = tmp_path / "opencode.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE session (id TEXT, title TEXT, project_id TEXT);
        CREATE TABLE project (id TEXT, name TEXT, worktree TEXT);
        CREATE TABLE message (id TEXT, session_id TEXT, time_created INT, data TEXT);
        CREATE TABLE part (id TEXT, message_id TEXT, time_created INT, data TEXT);
        INSERT INTO session VALUES ('session-1', 'Fix bug', 'project-1');
        INSERT INTO project VALUES ('project-1', 'My project', '/tmp/repo');
    """)
    for msg_id, session, timestamp, role in [
        ("user", "session-1", 1, "user"), ("answer", "session-1", 2, "assistant"),
        ("tool", "session-1", 3, "assistant"), ("other", "other-session", 4, "user"),
    ]:
        conn.execute("INSERT INTO message VALUES (?, ?, ?, ?)", (msg_id, session, timestamp, json.dumps({"role": role})))
    for part_id, msg_id, timestamp, payload in [
        ("p1", "user", 1, {"type": "text", "text": "Please fix it"}),
        ("p2", "answer", 3, {"type": "text", "text": "Second paragraph"}),
        ("p3", "answer", 2, {"type": "text", "text": "First paragraph"}),
        ("p4", "tool", 4, {"type": "tool", "text": "Should not appear"}),
        ("p5", "other", 5, {"type": "text", "text": "Wrong session"}),
        ("p6", "user", 6, {"type": "text", "text": "Injected", "synthetic": True}),
    ]:
        conn.execute("INSERT INTO part VALUES (?, ?, ?, ?)", (part_id, msg_id, timestamp, json.dumps(payload)))
    conn.commit()
    conn.close()
    assert details.load_session_details(record(path, "opencode")) == {
        "title": "Fix bug", "project": "My project", "last_user_prompt": "Please fix it",
        "last_message": "First paragraph\nSecond paragraph", "last_message_role": "assistant",
    }


def test_missing_or_unsupported_storage_is_harmless(tmp_path: Path) -> None:
    assert details.load_session_details(record(tmp_path / "missing.jsonl")) == {}
    assert details.load_session_details(record(tmp_path / "missing.db", "opencode")) == {}
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    assert details.load_session_details(record(path, "opencode")) == {}
    assert details.load_session_details(record(None)) == {}


def test_git_project_and_detached_head(monkeypatch) -> None:
    rec = record(None)
    rec.cwd = "/repo/subdirectory"
    answers = {("rev-parse", "--show-toplevel"): "/repo", ("rev-parse", "--short", "HEAD"): "abc123"}
    monkeypatch.setattr(details, "_git", lambda cwd, *args: answers.get(args, ""))
    assert details.load_session_details(rec) == {"project": "repo", "git_branch": "Detached HEAD (abc123)"}


def test_git_timeout_is_harmless(monkeypatch) -> None:
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1)
    monkeypatch.setattr(details.subprocess, "run", timeout)
    rec = record(None)
    rec.cwd = "/repo/subdirectory"
    assert details.load_session_details(rec) == {"project": "subdirectory"}


def test_long_messages_are_explicitly_truncated() -> None:
    result = details._excerpt("x" * (details.MAX_TEXT_CHARS + 1))
    assert result.endswith("… [truncated]")
