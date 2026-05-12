from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .models import SessionCandidates, SessionRecord


def safe_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def read_json_file(path: Path) -> Any | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def read_jsonl_file(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
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
                    records.append(item)
    except OSError:
        return []
    return records


def find_session_files(base: Path) -> Iterable[Path]:
    if not base.exists():
        return []
    candidates: list[Path] = []
    for pattern in ("*.json", "**/*.json", "*.jsonl", "**/*.jsonl", "*.db", "**/*.db"):
        candidates.extend(base.glob(pattern))
    uniq: dict[str, Path] = {}
    for path in candidates:
        uniq[str(path)] = path
    return uniq.values()


def normalize_cwd(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(Path(value).expanduser().resolve())
    except OSError:
        return str(Path(value).expanduser())


def extract_session_from_json(
    tool: str, path: Path, data: dict[str, Any]
) -> SessionRecord | None:
    possible_id_keys = ["id", "session_id", "sessionId", "uuid"]
    session_id = None
    for key in possible_id_keys:
        value = data.get(key)
        if isinstance(value, str) and len(value) >= 6:
            session_id = value
            break
    if not session_id:
        session_id = path.stem

    cwd = None
    for key in ("cwd", "working_directory", "workingDirectory", "repo_path", "path"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            cwd = value
            break

    metadata = {
        k: data.get(k)
        for k in ("title", "model", "updated_at", "created_at", "status")
        if k in data
    }

    return SessionRecord(
        tool=tool,
        session_id=session_id,
        path=path,
        last_write=safe_mtime(path),
        cwd=normalize_cwd(cwd),
        metadata=metadata,
    )


def extract_session_records(tool: str, path: Path, data: Any) -> list[SessionRecord]:
    return extract_matching_session_records(tool, path, data, None)


def session_matches_candidates(
    rec: SessionRecord, candidates: SessionCandidates | None
) -> bool:
    if candidates is None:
        return True
    if candidates.is_empty:
        return False
    if rec.session_id in candidates.session_ids:
        return True
    return rec.cwd is not None and rec.cwd in candidates.cwds


def extract_matching_session_records(
    tool: str,
    path: Path,
    data: Any,
    candidates: SessionCandidates | None,
) -> list[SessionRecord]:
    if isinstance(data, dict):
        rec = extract_session_from_json(tool, path, data)
        return [rec] if rec is not None and session_matches_candidates(rec, candidates) else []

    if isinstance(data, list):
        records: list[SessionRecord] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            rec = extract_session_from_json(tool, path, item)
            if rec is not None and session_matches_candidates(rec, candidates):
                records.append(rec)
        return records

    return []


def fallback_file_record(tool: str, path: Path) -> SessionRecord:
    return SessionRecord(
        tool=tool,
        session_id=path.stem,
        path=path,
        last_write=safe_mtime(path),
    )
