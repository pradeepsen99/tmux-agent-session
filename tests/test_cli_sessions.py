from __future__ import annotations

import json
from pathlib import Path

from tmux_agent_session import cli


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
