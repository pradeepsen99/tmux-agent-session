from __future__ import annotations

import json
import os
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

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


def _user_path(path: str) -> Path:
    return Path(os.path.expandvars(path)).expanduser()


DEFAULT_OPENCODE_DIRS = [
    _user_path("~/.local/share/opencode/opencode.db"),
    _user_path("~/Library/Application Support/opencode/opencode.db"),
    _user_path("%APPDATA%/opencode/opencode.db"),
    _user_path("~/.local/share/opencode/storage"),
    _user_path("~/Library/Application Support/opencode/storage"),
    _user_path("%APPDATA%/opencode/storage"),
]

DB_CANDIDATE_ROWS_PER_CWD = 3


def db_timestamp(value: Any) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    return value / 1000 if value > 10_000_000_000 else float(value)


@lru_cache(maxsize=4096)
def _normalize_cwd_cached(value: str) -> str | None:
    return normalize_cwd(value)


def opencode_model_metadata(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"model": raw.strip()}
        if not isinstance(data, dict):
            return {"model": raw.strip()}
    else:
        return {}

    metadata: dict[str, Any] = {}
    model = data.get("id")
    if isinstance(model, str) and model.strip():
        metadata["model"] = model.strip()
    provider = data.get("providerID") or data.get("provider")
    if isinstance(provider, str) and provider.strip():
        metadata["model_provider"] = provider.strip()
    variant = data.get("variant")
    if isinstance(variant, str) and variant.strip():
        metadata["model_variant"] = variant.strip()
    return metadata


def opencode_message_model_metadata(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}

    metadata: dict[str, Any] = {}
    metadata.update(opencode_model_metadata(data.get("model")))

    model = data.get("modelID")
    if "model" not in metadata and isinstance(model, str) and model.strip():
        metadata["model"] = model.strip()
    provider = data.get("providerID") or data.get("provider")
    if (
        "model_provider" not in metadata
        and isinstance(provider, str)
        and provider.strip()
    ):
        metadata["model_provider"] = provider.strip()
    variant = data.get("variant")
    if (
        "model_variant" not in metadata
        and isinstance(variant, str)
        and variant.strip()
    ):
        metadata["model_variant"] = variant.strip()
    return metadata


def load_opencode_message_models(
    conn: sqlite3.Connection,
    tables: set[str],
    session_ids: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    if "message" not in tables:
        return {}
    if session_ids is not None and not session_ids:
        return {}
    try:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(message)")
        }
        if not {"session_id", "data"}.issubset(columns):
            return {}
        where = ""
        params: tuple[str, ...] = ()
        if session_ids is not None:
            placeholders = ", ".join("?" for _ in session_ids)
            where = f" WHERE session_id IN ({placeholders})"
            params = tuple(session_ids)
        order = " ORDER BY time_created DESC" if "time_created" in columns else ""
        rows = conn.execute(f"SELECT session_id, data FROM message{where}{order}", params)
    except sqlite3.Error:
        return {}

    message_models: dict[str, dict[str, Any]] = {}
    for row in rows:
        session_id = row["session_id"]
        if not isinstance(session_id, str) or session_id in message_models:
            continue
        metadata = opencode_message_model_metadata(row["data"])
        if metadata.get("model"):
            message_models[session_id] = metadata
    return message_models


def _opencode_row_matches_candidates(
    row: sqlite3.Row, candidates: SessionCandidates | None
) -> bool:
    if candidates is None:
        return True
    if candidates.is_empty:
        return False

    session_id = _row_session_id(row)
    if isinstance(session_id, str) and session_id.strip() in candidates.session_ids:
        return True

    cwd = _row_normalized_cwd(row)
    return cwd is not None and cwd in candidates.cwds


def _row_session_id(row: sqlite3.Row) -> str | None:
    if "id" not in row.keys():
        return None
    value = row["id"]
    return value.strip() if isinstance(value, str) and value.strip() else None


def _row_normalized_cwd(row: sqlite3.Row) -> str | None:
    row_keys = set(row.keys())
    for key in ("directory", "path"):
        if key not in row_keys:
            continue
        value = row[key]
        if isinstance(value, str) and value.strip():
            return _normalize_cwd_cached(value.strip())
    return None


def _row_updated_ts(row: sqlite3.Row) -> float:
    row_keys = set(row.keys())
    updated = db_timestamp(row["time_updated"]) if "time_updated" in row_keys else None
    created = db_timestamp(row["time_created"]) if "time_created" in row_keys else None
    return updated or created or 0.0


def _limit_candidate_rows(
    rows: list[sqlite3.Row],
    candidates: SessionCandidates | None,
) -> list[sqlite3.Row]:
    if candidates is None or candidates.is_empty:
        return rows

    selected: list[sqlite3.Row] = []
    selected_ids: set[int] = set()

    def append_once(row: sqlite3.Row) -> None:
        marker = id(row)
        if marker in selected_ids:
            return
        selected_ids.add(marker)
        selected.append(row)

    for row in rows:
        session_id = _row_session_id(row)
        if session_id is not None and session_id in candidates.session_ids:
            append_once(row)

    for cwd in candidates.cwds:
        matches = [row for row in rows if _row_normalized_cwd(row) == cwd]
        matches.sort(key=_row_updated_ts, reverse=True)
        for row in matches[:DB_CANDIDATE_ROWS_PER_CWD]:
            append_once(row)

    return selected


def _records_satisfy_candidates(
    records: list[SessionRecord], candidates: SessionCandidates | None
) -> bool:
    if candidates is None or candidates.is_empty:
        return False

    session_ids = {rec.session_id for rec in records if rec.session_id}
    cwds = {rec.cwd for rec in records if rec.cwd is not None}
    return candidates.session_ids.issubset(session_ids) and candidates.cwds.issubset(cwds)


def extract_opencode_db_sessions(
    path: Path, candidates: SessionCandidates | None = None
) -> list[SessionRecord]:
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except (OSError, sqlite3.Error):
        fallback = fallback_file_record("opencode", path)
        return [fallback] if session_matches_candidates(fallback, candidates) else []

    try:
        tables = {
            row["name"]
            for row in conn.execute("PRAGMA table_list")
            if row["type"] == "table"
        }
        if "session" not in tables:
            return []

        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(session)")
        }
        selected = [
            column
            for column in (
                "id",
                "directory",
                "path",
                "title",
                "time_created",
                "time_updated",
                "agent",
                "model",
            )
            if column in columns
        ]
        if "id" not in selected:
            return []

        rows = conn.execute(f"SELECT {', '.join(selected)} FROM session").fetchall()
        rows = [row for row in rows if _opencode_row_matches_candidates(row, candidates)]
        rows = _limit_candidate_rows(rows, candidates)
        missing_model_session_ids = {
            row["id"].strip()
            for row in rows
            if isinstance(row["id"], str)
            and ("model" not in set(row.keys()) or not opencode_model_metadata(row["model"]))
        }
        message_models = load_opencode_message_models(
            conn, tables, missing_model_session_ids
        )
    except sqlite3.Error:
        fallback = fallback_file_record("opencode", path)
        return [fallback] if session_matches_candidates(fallback, candidates) else []
    finally:
        conn.close()

    records: list[SessionRecord] = []
    for row in rows:
        row_keys = set(row.keys())
        session_id = _row_session_id(row)
        if session_id is None:
            continue

        cwd = _row_normalized_cwd(row)

        metadata: dict[str, Any] = {}
        title = row["title"] if "title" in row_keys else None
        if isinstance(title, str) and title.strip():
            metadata["title"] = title.strip()
        agent = row["agent"] if "agent" in row_keys else None
        if isinstance(agent, str) and agent.strip():
            metadata["agent"] = agent.strip()
        if "model" in row_keys:
            metadata.update(opencode_model_metadata(row["model"]))
        if not metadata.get("model"):
            metadata.update(message_models.get(session_id.strip(), {}))

        updated = (
            db_timestamp(row["time_updated"]) if "time_updated" in row_keys else None
        )
        created = (
            db_timestamp(row["time_created"]) if "time_created" in row_keys else None
        )
        records.append(
            SessionRecord(
                tool="opencode",
                session_id=session_id,
                path=path,
                last_write=updated or created or safe_mtime(path),
                cwd=cwd,
                metadata=metadata,
            )
        )
    return records


def extract_opencode_sessions(
    path: Path, candidates: SessionCandidates | None = None
) -> list[SessionRecord]:
    if path.suffix == ".db":
        return extract_opencode_db_sessions(path, candidates)

    data = read_json_file(path)
    if data is None:
        fallback = fallback_file_record("opencode", path)
        return [fallback] if session_matches_candidates(fallback, candidates) else []
    return extract_matching_session_records("opencode", path, data, candidates)


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
        paths = [base] if base.is_file() else find_session_files(base)
        for path in paths:
            record_candidates = extract_opencode_sessions(path, candidates)
            if not record_candidates:
                continue
            for rec in record_candidates:
                key = (rec.tool, rec.session_id)
                if key in seen:
                    continue
                seen.add(key)
                records.append(rec)
        if _records_satisfy_candidates(records, candidates):
            break
    return records
