from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from ..models import SessionCandidates, SessionRecord
from ..session_files import (
    normalize_cwd,
    safe_mtime,
    session_matches_candidates,
)


DEFAULT_CURSOR_DIR = Path("~/.cursor/chats").expanduser()


def _workspace_hash(cwd: str) -> str:
    return hashlib.md5(cwd.encode("utf-8")).hexdigest()


def _decode_meta_blob(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        decoded = bytes.fromhex(value).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    try:
        item = json.loads(decoded)
    except json.JSONDecodeError:
        return None
    return item if isinstance(item, dict) else None


def _read_session_meta(path: Path) -> dict[str, Any] | None:
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except (OSError, sqlite3.Error):
        return None

    try:
        rows = conn.execute("SELECT value FROM meta").fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    for row in rows:
        meta = _decode_meta_blob(row["value"])
        if meta is not None and meta.get("agentId"):
            return meta
    return None


def _ms_to_iso(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    seconds = value / 1000 if value > 10_000_000_000 else float(value)
    try:
        return dt.datetime.fromtimestamp(seconds).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def extract_cursor_session(
    path: Path,
    cwd: str | None = None,
    candidates: SessionCandidates | None = None,
) -> SessionRecord | None:
    meta = _read_session_meta(path)
    if meta is None:
        return None

    session_id = meta.get("agentId")
    if not isinstance(session_id, str) or not session_id.strip():
        # The session directory is named after the agent id.
        session_id = path.parent.name
    session_id = session_id.strip()

    metadata: dict[str, Any] = {}
    name = meta.get("name")
    if isinstance(name, str) and name.strip():
        metadata["title"] = name.strip()
    mode = meta.get("mode")
    if isinstance(mode, str) and mode.strip():
        metadata["mode"] = mode.strip()
    created_at = _ms_to_iso(meta.get("createdAt"))
    if created_at:
        metadata["created_at"] = created_at

    rec = SessionRecord(
        tool="cursor-agent",
        session_id=session_id,
        path=path,
        last_write=safe_mtime(path),
        cwd=normalize_cwd(cwd) if cwd else None,
        metadata=metadata,
    )
    return rec if session_matches_candidates(rec, candidates) else None


def _iter_session_dbs(
    base: Path, candidates: SessionCandidates | None
) -> Iterable[tuple[Path, str | None]]:
    """Yield (store.db, cwd_hint) pairs to inspect.

    A known candidate cwd maps forward to its workspace directory via
    ``md5(cwd)``, so we can resolve sessions without reverse-hashing. When the
    cwd is unknown (no candidates, or matching purely by session id) we fall
    back to scanning every workspace directory with an empty cwd hint.
    """
    seen: set[Path] = set()

    def emit(store_db: Path, cwd_hint: str | None) -> Iterable[tuple[Path, str | None]]:
        if store_db in seen or not store_db.is_file():
            return
        seen.add(store_db)
        yield store_db, cwd_hint

    if candidates is not None and candidates.cwds:
        for cwd in candidates.cwds:
            workspace = base / _workspace_hash(cwd)
            if not workspace.is_dir():
                continue
            for session_dir in workspace.iterdir():
                if session_dir.is_dir():
                    yield from emit(session_dir / "store.db", cwd)

    needs_full_scan = candidates is None or bool(candidates.session_ids)
    if not needs_full_scan:
        return

    try:
        store_dbs = sorted(
            base.rglob("store.db"),
            key=lambda path: safe_mtime(path) or 0.0,
            reverse=True,
        )
    except OSError:
        return
    for store_db in store_dbs:
        yield from emit(store_db, None)


def _records_satisfy_candidates(
    records: list[SessionRecord], candidates: SessionCandidates | None
) -> bool:
    if candidates is None or candidates.is_empty:
        return False

    session_ids = {rec.session_id for rec in records if rec.session_id}
    cwds = {rec.cwd for rec in records if rec.cwd is not None}
    return candidates.session_ids.issubset(session_ids) and candidates.cwds.issubset(cwds)


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
        for store_db, cwd in _iter_session_dbs(base, candidates):
            rec = extract_cursor_session(store_db, cwd, candidates)
            if rec is None:
                continue
            key = (rec.tool, rec.session_id)
            if key in seen:
                continue
            seen.add(key)
            records.append(rec)
        if _records_satisfy_candidates(records, candidates):
            break
    return records
