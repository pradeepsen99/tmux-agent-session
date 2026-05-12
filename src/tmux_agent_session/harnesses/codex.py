from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from ..models import SessionCandidates, SessionRecord
from ..session_files import (
    extract_matching_session_records,
    fallback_file_record,
    find_session_files,
    normalize_cwd,
    read_json_file,
    safe_mtime,
    session_matches_candidates,
)


DEFAULT_CODEX_DIR = Path("~/.codex/sessions").expanduser()


def _iter_jsonl_dicts(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    yield item
    except (OSError, UnicodeDecodeError):
        return


def extract_codex_session(
    path: Path, candidates: SessionCandidates | None = None
) -> SessionRecord | None:
    session_meta: dict[str, Any] | None = None
    latest_turn_context: dict[str, Any] | None = None

    for entry in _iter_jsonl_dicts(path):
        entry_type = entry.get("type")
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        if entry_type == "session_meta" and session_meta is None:
            session_meta = payload
        elif entry_type == "turn_context":
            latest_turn_context = payload

    if session_meta is None and latest_turn_context is None:
        return None

    session_id = None
    id_candidates = [
        (session_meta or {}).get("id"),
        (latest_turn_context or {}).get("session_id"),
        path.stem.removeprefix("rollout-"),
        path.stem,
    ]
    for candidate in id_candidates:
        if isinstance(candidate, str) and candidate.strip():
            session_id = candidate.strip()
            break

    cwd = None
    for candidate in (
        (latest_turn_context or {}).get("cwd"),
        (session_meta or {}).get("cwd"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            cwd = candidate.strip()
            break

    metadata: dict[str, Any] = {}
    if session_meta:
        for key in (
            "timestamp",
            "originator",
            "cli_version",
            "source",
            "model_provider",
        ):
            if key in session_meta:
                metadata[key] = session_meta[key]
    if latest_turn_context:
        for key in ("model", "approval_policy", "personality", "summary"):
            if key in latest_turn_context:
                metadata[key] = latest_turn_context[key]

    rec = SessionRecord(
        tool="codex",
        session_id=session_id or path.stem,
        path=path,
        last_write=safe_mtime(path),
        cwd=normalize_cwd(cwd),
        metadata=metadata,
    )
    return rec if session_matches_candidates(rec, candidates) else None


def load_sessions(
    base_paths: list[Path], candidates: SessionCandidates | None = None
) -> list[SessionRecord]:
    if candidates is not None and candidates.is_empty:
        return []

    records: list[SessionRecord] = []
    seen: set[tuple[str, str]] = set()

    for base in base_paths:
        if not base.exists():
            continue
        for path in find_session_files(base):
            if path.suffix == ".jsonl":
                rec = extract_codex_session(path, candidates)
                record_candidates = [rec] if rec is not None else []
            else:
                data = read_json_file(path)
                if data is None:
                    fallback = fallback_file_record("codex", path)
                    record_candidates = (
                        [fallback]
                        if session_matches_candidates(fallback, candidates)
                        else []
                    )
                else:
                    record_candidates = extract_matching_session_records(
                        "codex", path, data, candidates
                    )
            if not record_candidates:
                continue
            for rec in record_candidates:
                key = (rec.tool, rec.session_id)
                if key in seen:
                    continue
                seen.add(key)
                records.append(rec)
    return records
