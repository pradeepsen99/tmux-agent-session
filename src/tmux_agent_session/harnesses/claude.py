from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

from ..models import BLANK_SESSION_TITLE, SessionCandidates, SessionRecord
from ..session_cache import SessionCache, fingerprint
from ..session_files import (
    normalize_cwd,
    safe_mtime,
    session_matches_candidates,
)


DEFAULT_CLAUDE_DIR = Path("~/.claude/projects").expanduser()
CLAUDE_HEAD_SCAN_LINES = 64
CLAUDE_TAIL_BLOCK_BYTES = 64 * 1024
CLAUDE_TAIL_MAX_BYTES = 1024 * 1024
# Raw key markers used to skip decoding head lines that cannot add a missing field.
CLAUDE_HEAD_KEY_MARKERS = {
    "sessionId": b'"sessionId"',
    "cwd": b'"cwd"',
    "gitBranch": b'"gitBranch"',
    "version": b'"version"',
    "title": b'"aiTitle"',
    "model": b'"model"',
}


def project_dir_name(cwd: str) -> str:
    """Map a cwd to its Claude project directory name.

    Claude stores transcripts under ``~/.claude/projects/<name>`` where ``name``
    is the cwd with every non-alphanumeric character replaced by ``-`` (e.g.
    ``/Users/me/ML_ENG/app`` -> ``-Users-me-ML-ENG-app``). The encoding is lossy
    so it only maps forward (cwd -> dir), which is all we need to resolve a known
    candidate cwd to its directory.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _head_fields(
    path: Path, max_lines: int = CLAUDE_HEAD_SCAN_LINES
) -> dict[str, str]:
    """Return the first value of each ``CLAUDE_HEAD_KEY_MARKERS`` field in the head.

    Head lines can carry large tool results, so a line is only decoded when it
    mentions a field that is still missing, and the scan stops once all are found.
    """
    found: dict[str, str] = {}
    try:
        with path.open("rb") as f:
            for index, raw_line in enumerate(f):
                if index >= max_lines:
                    break
                missing = [key for key in CLAUDE_HEAD_KEY_MARKERS if key not in found]
                if not missing:
                    break
                if not any(CLAUDE_HEAD_KEY_MARKERS[key] in raw_line for key in missing):
                    continue
                try:
                    entry = json.loads(raw_line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                for key in missing:
                    value = _head_field_value(entry, key)
                    if value is not None:
                        found[key] = value
    except OSError:
        pass
    return found


def _head_field_value(entry: dict[str, Any], key: str) -> str | None:
    if key == "title":
        value = entry.get("aiTitle")
    elif key == "model":
        message = entry.get("message")
        value = message.get("model") if isinstance(message, dict) else None
    else:
        value = entry.get(key)
    if not isinstance(value, str):
        return None
    # gitBranch and version were historically kept verbatim, even when blank.
    if key in ("gitBranch", "version"):
        return value
    return value.strip() or None


def _ai_title_from_line(line: bytes) -> str | None:
    if b'"aiTitle"' not in line:
        return None
    try:
        item = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    title = item.get("aiTitle") if isinstance(item, dict) else None
    if isinstance(title, str) and title.strip():
        return title.strip()
    return None


def _latest_ai_title(path: Path) -> str | None:
    """Return the most recent ``aiTitle`` by reading the transcript backwards.

    Claude re-appends an ``ai-title`` entry throughout a session and the title
    can change (e.g. after a rename), so the first one in the file goes stale.
    Reads are bounded to ``CLAUDE_TAIL_MAX_BYTES`` to keep large transcripts cheap.
    """
    try:
        with path.open("rb") as f:
            pos = f.seek(0, os.SEEK_END)
            limit = max(0, pos - CLAUDE_TAIL_MAX_BYTES)
            carry = b""
            while pos > limit:
                start = max(limit, pos - CLAUDE_TAIL_BLOCK_BYTES)
                f.seek(start)
                lines = (f.read(pos - start) + carry).split(b"\n")
                pos = start
                # The first piece may be a partial line; finish it on the next block.
                carry = lines.pop(0) if pos > 0 else b""
                for line in reversed(lines):
                    title = _ai_title_from_line(line)
                    if title is not None:
                        return title
    except OSError:
        return None
    return None


def extract_claude_session(
    path: Path, candidates: SessionCandidates | None = None
) -> SessionRecord | None:
    fields = _head_fields(path)
    session_id = fields.pop("sessionId", None)
    cwd = fields.pop("cwd", None)
    metadata: dict[str, Any] = {
        key: fields[key] for key in ("gitBranch", "version", "title", "model") if key in fields
    }

    if session_id is None and cwd is None:
        return None

    latest_title = _latest_ai_title(path)
    if latest_title is not None:
        metadata["title"] = latest_title
    # Claude writes an ai-title once a real prompt arrives, so none means blank.
    metadata.setdefault("title", BLANK_SESSION_TITLE)

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
    """Yield top-level transcript ``*.jsonl`` paths to inspect.

    Claude stores each session at ``<base>/<project>/<sessionId>.jsonl``, so a
    candidate session id is looked up by file name and a candidate cwd maps
    forward to its project directory via ``project_dir_name(cwd)``. Only an
    unfiltered listing scans every project directory. Nested files (such as
    ``<session>/subagents/*.jsonl``) reuse their parent's session id and are
    never treated as sessions of their own.
    """
    seen: set[Path] = set()

    def emit(path: Path) -> Iterable[Path]:
        if path in seen or not path.is_file():
            return
        seen.add(path)
        yield path

    if candidates is None:
        try:
            paths = sorted(
                base.glob("*/*.jsonl"),
                key=lambda path: safe_mtime(path) or 0.0,
                reverse=True,
            )
        except OSError:
            return
        for path in paths:
            yield from emit(path)
        return

    if candidates.session_ids:
        try:
            projects = [path for path in base.iterdir() if path.is_dir()]
        except OSError:
            projects = []
        for session_id in sorted(candidates.session_ids):
            for project in projects:
                yield from emit(project / f"{session_id}.jsonl")

    for cwd in sorted(candidates.cwds):
        project = base / project_dir_name(cwd)
        if not project.is_dir():
            continue
        for path in project.glob("*.jsonl"):
            yield from emit(path)


def load_sessions(
    base_paths: list[Path], candidates: SessionCandidates | None = None
) -> list[SessionRecord]:
    if candidates is not None and candidates.is_empty:
        return []

    records: list[SessionRecord] = []
    seen: set[tuple[str, str]] = set()

    cache = SessionCache("claude")
    try:
        for base in base_paths:
            if not base.exists():
                continue
            for path in _iter_session_files(base, candidates):
                stamp = fingerprint(path)
                hit, rec = cache.read(path, stamp)
                if not hit:
                    rec = extract_claude_session(path)
                    cache.write(path, stamp, rec)
                if rec is None or not session_matches_candidates(rec, candidates):
                    continue
                key = (rec.tool, rec.session_id)
                if key in seen:
                    continue
                seen.add(key)
                records.append(rec)
    finally:
        cache.close()
    return records
