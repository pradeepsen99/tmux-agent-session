from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from ..models import SessionCandidates, SessionRecord
from ..session_files import (
    normalize_cwd,
    safe_mtime,
    session_matches_candidates,
)


DEFAULT_CLAUDE_DIR = Path("~/.claude/projects").expanduser()
CLAUDE_HEAD_SCAN_LINES = 64


def project_dir_name(cwd: str) -> str:
    """Map a cwd to its Claude project directory name.

    Claude stores transcripts under ``~/.claude/projects/<name>`` where ``name``
    is the cwd with every non-alphanumeric character replaced by ``-`` (e.g.
    ``/Users/me/ML_ENG/app`` -> ``-Users-me-ML-ENG-app``). The encoding is lossy
    so it only maps forward (cwd -> dir), which is all we need to resolve a known
    candidate cwd to its directory.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _iter_jsonl_head(
    path: Path, max_lines: int = CLAUDE_HEAD_SCAN_LINES
) -> Iterable[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as f:
            for index, raw_line in enumerate(f):
                if index >= max_lines:
                    break
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


def extract_claude_session(
    path: Path, candidates: SessionCandidates | None = None
) -> SessionRecord | None:
    session_id: str | None = None
    cwd: str | None = None
    metadata: dict[str, Any] = {}

    for entry in _iter_jsonl_head(path):
        if session_id is None:
            value = entry.get("sessionId")
            if isinstance(value, str) and value.strip():
                session_id = value.strip()
        if cwd is None:
            value = entry.get("cwd")
            if isinstance(value, str) and value.strip():
                cwd = value.strip()
        for key in ("gitBranch", "version"):
            if key not in metadata and isinstance(entry.get(key), str):
                metadata[key] = entry[key]
        if "title" not in metadata:
            title = entry.get("aiTitle")
            if isinstance(title, str) and title.strip():
                metadata["title"] = title.strip()
        if "model" not in metadata:
            message = entry.get("message")
            if isinstance(message, dict):
                model = message.get("model")
                if isinstance(model, str) and model.strip():
                    metadata["model"] = model.strip()

    if session_id is None and cwd is None:
        return None

    rec = SessionRecord(
        tool="claude",
        session_id=session_id or path.stem,
        path=path,
        last_write=safe_mtime(path),
        cwd=normalize_cwd(cwd),
        metadata=metadata,
    )
    return rec if session_matches_candidates(rec, candidates) else None


def _iter_session_files(
    base: Path, candidates: SessionCandidates | None
) -> Iterable[Path]:
    """Yield transcript ``*.jsonl`` paths to inspect.

    A known candidate cwd maps forward to its project directory via
    ``project_dir_name(cwd)``, so we can resolve sessions without reverse-decoding
    the directory name. When the cwd is unknown (no candidates, or matching by
    session id) we fall back to scanning every project directory newest-first.
    """
    seen: set[Path] = set()

    def emit(path: Path) -> Iterable[Path]:
        if path in seen or not path.is_file():
            return
        seen.add(path)
        yield path

    if candidates is not None and candidates.cwds:
        for cwd in candidates.cwds:
            project = base / project_dir_name(cwd)
            if not project.is_dir():
                continue
            for path in project.glob("*.jsonl"):
                yield from emit(path)

    needs_full_scan = candidates is None or bool(candidates.session_ids)
    if not needs_full_scan:
        return

    try:
        paths = sorted(
            base.rglob("*.jsonl"),
            key=lambda path: safe_mtime(path) or 0.0,
            reverse=True,
        )
    except OSError:
        return
    for path in paths:
        yield from emit(path)


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
        for path in _iter_session_files(base, candidates):
            rec = extract_claude_session(path, candidates)
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
