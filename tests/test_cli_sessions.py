from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from tmux_agent_session import cli
from tmux_agent_session.harnesses import codex
from tmux_agent_session.harnesses import opencode


def test_read_json_file_handles_valid_invalid_and_missing(tmp_path: Path) -> None:
    valid = tmp_path / "session.json"
    valid.write_text('{"id": "abc123"}', encoding="utf-8")
    invalid = tmp_path / "broken.json"
    invalid.write_text('{"id":', encoding="utf-8")

    assert cli.read_json_file(valid) == {"id": "abc123"}
    assert cli.read_json_file(invalid) is None
    assert cli.read_json_file(tmp_path / "missing.json") is None


def test_read_jsonl_file_skips_invalid_and_non_dict_lines(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(
        "\n".join(
            [
                '{"type": "session_meta", "payload": {"id": "abc123"}}',
                "[]",
                '{"broken":',
                '{"type": "turn_context", "payload": {"cwd": "/tmp/repo"}}',
            ]
        ),
        encoding="utf-8",
    )

    assert cli.read_jsonl_file(path) == [
        {"type": "session_meta", "payload": {"id": "abc123"}},
        {"type": "turn_context", "payload": {"cwd": "/tmp/repo"}},
    ]


def test_find_session_files_discovers_nested_supported_suffixes(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    files = [
        tmp_path / "one.json",
        nested / "two.jsonl",
        nested / "three.db",
    ]
    for path in files:
        path.write_text("{}", encoding="utf-8")

    found = {path.name for path in cli.find_session_files(tmp_path)}
    assert found == {"one.json", "two.jsonl", "three.db"}


def test_extract_session_from_json_uses_known_fields(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    path.write_text("{}", encoding="utf-8")
    data = {
        "sessionId": "session-123",
        "workingDirectory": str(tmp_path),
        "title": "Debugging",
        "model": "gpt-5",
        "created_at": "2024-01-01T00:00:00Z",
        "status": "running",
    }

    rec = cli.extract_session_from_json("opencode", path, data)

    assert rec is not None
    assert rec.tool == "opencode"
    assert rec.session_id == "session-123"
    assert rec.cwd == str(tmp_path.resolve())
    assert rec.metadata == {
        "title": "Debugging",
        "model": "gpt-5",
        "created_at": "2024-01-01T00:00:00Z",
        "status": "running",
    }


def test_extract_session_records_supports_dict_list_and_other(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    path.write_text("{}", encoding="utf-8")

    dict_records = cli.extract_session_records("opencode", path, {"id": "record-one"})
    list_records = cli.extract_session_records(
        "opencode", path, [{"id": "record-one"}, {"id": "record-two"}, "skip"]
    )

    assert [rec.session_id for rec in dict_records] == ["record-one"]
    assert [rec.session_id for rec in list_records] == ["record-one", "record-two"]
    assert cli.extract_session_records("opencode", path, "skip") == []


def test_extract_opencode_sessions_reads_sqlite_model_metadata(
    tmp_path: Path,
) -> None:
    path = tmp_path / "opencode.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE session (
            id text PRIMARY KEY,
            directory text NOT NULL,
            title text NOT NULL,
            time_created integer NOT NULL,
            time_updated integer NOT NULL,
            agent text,
            model text
        )
        """
    )
    conn.execute(
        """
        INSERT INTO session (
            id, directory, title, time_created, time_updated, agent, model
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "ses_123456",
            str(tmp_path),
            "Investigate bug",
            1_700_000_000_000,
            1_700_000_123_000,
            "build",
            json.dumps(
                {"id": "gpt-5.5-fast", "providerID": "openai", "variant": "xhigh"}
            ),
        ),
    )
    conn.commit()
    conn.close()

    records = cli.extract_opencode_sessions(path)

    assert len(records) == 1
    rec = records[0]
    assert rec.session_id == "ses_123456"
    assert rec.cwd == str(tmp_path.resolve())
    assert rec.last_write == 1_700_000_123
    assert rec.metadata["title"] == "Investigate bug"
    assert rec.metadata["agent"] == "build"
    assert rec.metadata["model"] == "gpt-5.5-fast"
    assert rec.metadata["model_provider"] == "openai"
    assert rec.metadata["model_variant"] == "xhigh"


def test_extract_opencode_sessions_falls_back_to_message_model_metadata(
    tmp_path: Path,
) -> None:
    path = tmp_path / "opencode.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE session (
            id text PRIMARY KEY,
            directory text NOT NULL,
            time_updated integer NOT NULL,
            model text
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE message (
            id text PRIMARY KEY,
            session_id text NOT NULL,
            time_created integer NOT NULL,
            data text NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO session VALUES (?, ?, ?, ?)",
        ("ses_abcdef", str(tmp_path), 1_700_000_123_000, None),
    )
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?)",
        (
            "msg_older",
            "ses_abcdef",
            1,
            json.dumps({"modelID": "gpt-old", "providerID": "openai"}),
        ),
    )
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?)",
        (
            "msg_newer",
            "ses_abcdef",
            2,
            json.dumps({"modelID": "gpt-5.4", "providerID": "openai"}),
        ),
    )
    conn.commit()
    conn.close()

    records = cli.extract_opencode_sessions(path)

    assert len(records) == 1
    assert records[0].metadata["model"] == "gpt-5.4"
    assert records[0].metadata["model_provider"] == "openai"


def test_extract_opencode_sessions_limits_candidate_cwd_history(
    tmp_path: Path,
) -> None:
    path = tmp_path / "opencode.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE session (
            id text PRIMARY KEY,
            directory text NOT NULL,
            time_updated integer NOT NULL,
            model text
        )
        """
    )
    for index in range(5):
        conn.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?)",
            (
                f"ses_{index}",
                str(tmp_path),
                1_700_000_000_000 + index,
                json.dumps({"id": f"gpt-{index}"}),
            ),
        )
    conn.commit()
    conn.close()

    candidates = cli.SessionCandidates(cwds=frozenset({str(tmp_path.resolve())}))

    records = cli.extract_opencode_sessions(path, candidates)

    assert [rec.session_id for rec in records] == ["ses_4", "ses_3", "ses_2"]


def test_opencode_load_sessions_skips_storage_when_db_satisfies_candidates(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "opencode.db"
    storage = tmp_path / "storage"
    storage.mkdir()
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE session (
            id text PRIMARY KEY,
            directory text NOT NULL,
            time_updated integer NOT NULL,
            model text
        )
        """
    )
    conn.execute(
        "INSERT INTO session VALUES (?, ?, ?, ?)",
        ("ses_abcdef", str(tmp_path), 1_700_000_123_000, json.dumps({"id": "gpt-5"})),
    )
    conn.commit()
    conn.close()

    def fail_find_session_files(_base: Path):
        raise AssertionError("storage should not be scanned after DB match")

    monkeypatch.setattr(opencode, "find_session_files", fail_find_session_files)
    candidates = cli.SessionCandidates(cwds=frozenset({str(tmp_path.resolve())}))

    records = opencode.load_sessions([db_path, storage], candidates)

    assert [rec.session_id for rec in records] == ["ses_abcdef"]


def test_extract_codex_session_reads_metadata_and_turn_context(tmp_path: Path) -> None:
    path = tmp_path / "rollout-fallback-id.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "codex-123",
                            "timestamp": "2024-01-01T00:00:00Z",
                            "originator": "user",
                            "cli_version": "1.2.3",
                            "source": "cli",
                            "model_provider": "openai",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "turn_context",
                        "payload": {
                            "session_id": "ignored-session-id",
                            "cwd": str(tmp_path),
                            "model": "gpt-5",
                            "approval_policy": "never",
                            "personality": "default",
                            "summary": "Investigate failing tests",
                        },
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    rec = cli.extract_codex_session(path)

    assert rec is not None
    assert rec.session_id == "codex-123"
    assert rec.cwd == str(tmp_path.resolve())
    assert rec.metadata["model"] == "gpt-5"
    assert rec.metadata["summary"] == "Investigate failing tests"
    assert rec.metadata["originator"] == "user"


def test_extract_codex_session_falls_back_to_filename_and_none_when_irrelevant(
    tmp_path: Path,
) -> None:
    fallback = tmp_path / "rollout-file-derived.jsonl"
    fallback.write_text(
        json.dumps(
            {
                "type": "turn_context",
                "payload": {"cwd": str(tmp_path / "repo")},
            }
        ),
        encoding="utf-8",
    )
    irrelevant = tmp_path / "irrelevant.jsonl"
    irrelevant.write_text(
        json.dumps({"type": "message", "payload": {"text": "hello"}}),
        encoding="utf-8",
    )

    fallback_record = cli.extract_codex_session(fallback)

    assert fallback_record is not None
    assert fallback_record.session_id == "file-derived"
    assert cli.extract_codex_session(irrelevant) is None


def test_extract_codex_session_reads_head_and_tail_without_full_scan(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    path = tmp_path / "rollout-large.jsonl"
    padding = "x" * 2048
    lines = [
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": "codex-123",
                    "timestamp": "2024-01-01T00:00:00Z",
                },
            }
        )
    ]
    lines.extend(
        json.dumps(
            {
                "type": "message",
                "payload": {"index": index, "text": padding},
            }
        )
        for index in range(220)
    )
    lines.append(
        json.dumps(
            {
                "type": "turn_context",
                "payload": {
                    "cwd": str(repo),
                    "model": "gpt-5",
                    "summary": "Latest context",
                },
            }
        )
    )
    path.write_text("\n".join(lines), encoding="utf-8")

    loads_count = 0
    original_loads = json.loads

    def counted_loads(raw: str):
        nonlocal loads_count
        loads_count += 1
        return original_loads(raw)

    monkeypatch.setattr(codex.json, "loads", counted_loads)
    candidates = cli.SessionCandidates(cwds=frozenset({str(repo.resolve())}))

    rec = codex.extract_codex_session(path, candidates)

    assert rec is not None
    assert rec.session_id == "codex-123"
    assert rec.cwd == str(repo.resolve())
    assert rec.metadata["model"] == "gpt-5"
    assert loads_count < len(lines)


def test_codex_load_sessions_stops_after_newest_candidate_match(
    tmp_path: Path, monkeypatch
) -> None:
    target_cwd = str(tmp_path.resolve())
    older = tmp_path / "rollout-older.jsonl"
    newer = tmp_path / "rollout-newer.jsonl"
    older.write_text("{}", encoding="utf-8")
    newer.write_text("{}", encoding="utf-8")
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_700_000_100, 1_700_000_100))
    scanned: list[str] = []

    def fake_extract(path: Path, candidates=None):
        scanned.append(path.name)
        if path == newer:
            return cli.SessionRecord(
                tool="codex",
                session_id="session-newer",
                path=path,
                last_write=1_700_000_100,
                cwd=target_cwd,
            )
        raise AssertionError("older matching files should not be scanned")

    monkeypatch.setattr(codex, "extract_codex_session", fake_extract)
    candidates = cli.SessionCandidates(cwds=frozenset({target_cwd}))

    records = codex.load_sessions([tmp_path], candidates)

    assert [rec.session_id for rec in records] == ["session-newer"]
    assert scanned == ["rollout-newer.jsonl"]


def test_load_sessions_handles_codex_jsonl_invalid_json_and_dedupes(
    tmp_path: Path,
) -> None:
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    (codex_dir / "rollout-first.jsonl").write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "dup-session"},
            }
        ),
        encoding="utf-8",
    )
    (codex_dir / "rollout-second.jsonl").write_text(
        json.dumps(
            {
                "type": "turn_context",
                "payload": {"session_id": "dup-session", "cwd": str(tmp_path)},
            }
        ),
        encoding="utf-8",
    )

    opencode_dir = tmp_path / "opencode"
    opencode_dir.mkdir()
    (opencode_dir / "valid.json").write_text(
        json.dumps({"id": "session-1", "cwd": str(tmp_path)}), encoding="utf-8"
    )
    (opencode_dir / "invalid.json").write_text('{"id":', encoding="utf-8")

    codex_records = cli.load_sessions("codex", [codex_dir])
    opencode_records = cli.load_sessions("opencode", [opencode_dir])

    assert [rec.session_id for rec in codex_records] == ["dup-session"]
    assert {rec.session_id for rec in opencode_records} == {"session-1", "invalid"}
