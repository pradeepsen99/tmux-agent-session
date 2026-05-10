from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from ..models import SessionRecord
from ..session_files import (
    extract_session_records,
    fallback_file_record,
    find_session_files,
    normalize_cwd,
    read_json_file,
    safe_mtime,
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


def db_timestamp(value: Any) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    return value / 1000 if value > 10_000_000_000 else float(value)


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
    conn: sqlite3.Connection, tables: set[str]
) -> dict[str, dict[str, Any]]:
    if "message" not in tables:
        return {}
    try:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(message)")
        }
        if not {"session_id", "data"}.issubset(columns):
            return {}
        order = " ORDER BY time_created DESC" if "time_created" in columns else ""
        rows = conn.execute(f"SELECT session_id, data FROM message{order}")
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


def extract_opencode_db_sessions(path: Path) -> list[SessionRecord]:
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except (OSError, sqlite3.Error):
        return [fallback_file_record("opencode", path)]

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
        message_models = load_opencode_message_models(conn, tables)
    except sqlite3.Error:
        return [fallback_file_record("opencode", path)]
    finally:
        conn.close()

    records: list[SessionRecord] = []
    for row in rows:
        row_keys = set(row.keys())
        session_id = row["id"]
        if not isinstance(session_id, str) or not session_id.strip():
            continue

        cwd = None
        for key in ("directory", "path"):
            if key not in row_keys:
                continue
            value = row[key]
            if isinstance(value, str) and value.strip():
                cwd = value.strip()
                break

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
                session_id=session_id.strip(),
                path=path,
                last_write=updated or created or safe_mtime(path),
                cwd=normalize_cwd(cwd),
                metadata=metadata,
            )
        )
    return records


def extract_opencode_sessions(path: Path) -> list[SessionRecord]:
    if path.suffix == ".db":
        return extract_opencode_db_sessions(path)

    data = read_json_file(path)
    if data is None:
        return [fallback_file_record("opencode", path)]
    return extract_session_records("opencode", path, data)


def load_sessions(base_paths: list[Path]) -> list[SessionRecord]:
    records: list[SessionRecord] = []
    seen: set[tuple[str, str]] = set()

    for base in base_paths:
        if not base.exists():
            continue
        paths = [base] if base.is_file() else find_session_files(base)
        for path in paths:
            candidates = extract_opencode_sessions(path)
            if not candidates:
                continue
            for rec in candidates:
                key = (rec.tool, rec.session_id)
                if key in seen:
                    continue
                seen.add(key)
                records.append(rec)
    return records
