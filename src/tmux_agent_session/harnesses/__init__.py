from __future__ import annotations

from pathlib import Path

from ..models import SessionCandidates, SessionRecord
from . import codex, opencode


def load_sessions(
    tool: str,
    base_paths: list[Path],
    candidates: SessionCandidates | None = None,
) -> list[SessionRecord]:
    if tool == "codex":
        return codex.load_sessions(base_paths, candidates)
    if tool == "opencode":
        return opencode.load_sessions(base_paths, candidates)
    raise ValueError(f"unsupported tool: {tool}")
