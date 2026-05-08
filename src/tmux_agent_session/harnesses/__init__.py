from __future__ import annotations

from pathlib import Path

from ..models import SessionRecord
from . import codex, opencode


def load_sessions(tool: str, base_paths: list[Path]) -> list[SessionRecord]:
    if tool == "codex":
        return codex.load_sessions(base_paths)
    if tool == "opencode":
        return opencode.load_sessions(base_paths)
    raise ValueError(f"unsupported tool: {tool}")
