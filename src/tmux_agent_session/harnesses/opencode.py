from __future__ import annotations

from pathlib import Path

from ..models import SessionRecord
from ..session_files import (
    extract_session_records,
    fallback_file_record,
    find_session_files,
    read_json_file,
)


DEFAULT_OPENCODE_DIRS = [
    Path("~/.local/share/opencode/storage").expanduser(),
    Path("~/Library/Application Support/opencode/storage").expanduser(),
    Path("%APPDATA%/opencode/storage").expanduser(),
]


def extract_opencode_sessions(path: Path) -> list[SessionRecord]:
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
        for path in find_session_files(base):
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
