"""Best-effort metadata cache; never stores live process state or conversations."""
from __future__ import annotations

import json
import os
import sqlite3
from contextvars import ContextVar
from pathlib import Path

from .models import SessionRecord

CACHE_ENABLED: ContextVar[bool] = ContextVar("session_cache_enabled", default=True)
PARSER_VERSION = 1


def fingerprint(path: Path) -> tuple[int, int, int, int] | None:
    try:
        stat = path.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns
    except OSError:
        return None


class SessionCache:
    def __init__(self) -> None:
        self.conn = None
        if not CACHE_ENABLED.get():
            return
        base = Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser()
        try:
            directory = base / "tmux-agent-session"
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.conn = sqlite3.connect(directory / "metadata.sqlite3", timeout=0.05)
            self.conn.execute("CREATE TABLE IF NOT EXISTS metadata (tool TEXT, path TEXT, version INTEGER, fingerprint TEXT, payload TEXT, PRIMARY KEY(tool, path))")
        except (OSError, sqlite3.Error):
            self.close()

    def read(self, path: Path, stamp: tuple | None) -> tuple[bool, SessionRecord | None]:
        if self.conn is None or stamp is None:
            return False, None
        try:
            row = self.conn.execute("SELECT payload FROM metadata WHERE tool=? AND path=? AND version=? AND fingerprint=?", ("codex", str(path.resolve()), PARSER_VERSION, json.dumps(stamp))).fetchone()
            if row is None:
                return False, None
            data = json.loads(row[0])
            if data is None:
                return True, None
            if not isinstance(data, dict) or not isinstance(data.get("session_id"), str) or not isinstance(data.get("metadata"), dict):
                return False, None
            return True, SessionRecord(tool="codex", path=path, **data)
        except sqlite3.Error:
            self.close()
            return False, None
        except (OSError, ValueError, TypeError):
            return False, None

    def write(self, path: Path, stamp: tuple | None, rec: SessionRecord | None) -> None:
        if self.conn is None or stamp is None or fingerprint(path) != stamp:
            return
        data = None if rec is None else {"session_id": rec.session_id, "cwd": rec.cwd, "last_write": rec.last_write, "metadata": rec.metadata}
        try:
            self.conn.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?, ?, ?, ?)", ("codex", str(path.resolve()), PARSER_VERSION, json.dumps(stamp), json.dumps(data)))
        except (OSError, sqlite3.Error):
            self.close()

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.commit()
            except sqlite3.Error:
                pass
            self.conn.close()
            self.conn = None
