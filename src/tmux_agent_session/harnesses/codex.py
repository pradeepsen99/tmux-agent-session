from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from ..models import SessionCandidates, SessionRecord
from ..session_files import (
    normalize_cwd,
    safe_mtime,
    session_matches_candidates,
)


DEFAULT_CODEX_DIR = Path("~/.codex/sessions").expanduser()
CODEX_HEAD_SCAN_LINES = 64
CODEX_TAIL_SCAN_BYTES = 128 * 1024


def _jsonl_dict_from_line(raw_line: str) -> dict[str, Any] | None:
    line = raw_line.strip()
    if not line:
        return None
    try:
        item = json.loads(line)
    except json.JSONDecodeError:
        return None
    return item if isinstance(item, dict) else None


def _iter_jsonl_head(
    path: Path, max_lines: int = CODEX_HEAD_SCAN_LINES
) -> Iterable[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as f:
            for index, raw_line in enumerate(f):
                if index >= max_lines:
                    break
                item = _jsonl_dict_from_line(raw_line)
                if item is not None:
                    yield item
    except (OSError, UnicodeDecodeError):
        return


def _iter_jsonl_tail(
    path: Path, max_bytes: int = CODEX_TAIL_SCAN_BYTES
) -> Iterable[dict[str, Any]]:
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            start = max(0, size - max_bytes)
            f.seek(start)
            data = f.read()
    except OSError:
        return

    if start > 0:
        first_newline = data.find(b"\n")
        if first_newline == -1:
            return
        data = data[first_newline + 1 :]

    text = data.decode("utf-8", errors="ignore")
    for raw_line in text.splitlines():
        item = _jsonl_dict_from_line(raw_line)
        if item is not None:
            yield item


def _extract_codex_payloads(
    path: Path,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    session_meta: dict[str, Any] | None = None
    latest_turn_context: dict[str, Any] | None = None

    for entries in (_iter_jsonl_head(path), _iter_jsonl_tail(path)):
        for entry in entries:
            entry_type = entry.get("type")
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                continue
            if entry_type == "session_meta" and session_meta is None:
                session_meta = payload
            elif entry_type == "turn_context":
                latest_turn_context = payload

    return session_meta, latest_turn_context


def find_codex_session_files(base: Path) -> list[Path]:
    if not base.exists():
        return []
    if base.is_file():
        return [base] if base.suffix == ".jsonl" else []
    try:
        paths = list(base.rglob("*.jsonl"))
    except OSError:
        return []
    return sorted(paths, key=lambda path: safe_mtime(path) or 0.0, reverse=True)


def _records_satisfy_candidates(
    records: list[SessionRecord], candidates: SessionCandidates | None
) -> bool:
    if candidates is None or candidates.is_empty:
        return False

    session_ids = {rec.session_id for rec in records if rec.session_id}
    cwds = {rec.cwd for rec in records if rec.cwd is not None}
    return candidates.session_ids.issubset(session_ids) and candidates.cwds.issubset(cwds)


def extract_codex_session(
    path: Path, candidates: SessionCandidates | None = None
) -> SessionRecord | None:
    session_meta, latest_turn_context = _extract_codex_payloads(path)

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
        for path in find_codex_session_files(base):
            rec = extract_codex_session(path, candidates)
            record_candidates = [rec] if rec is not None else []
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
        if _records_satisfy_candidates(records, candidates):
            break
    return records
