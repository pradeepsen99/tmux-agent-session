from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ProcessInfo:
    pid: int
    ppid: int | None
    tty: str | None
    etime_seconds: int | None
    cwd: str | None
    command: str
    tool: str
    session_ids: list[str] = field(default_factory=list)


@dataclass
class TmuxPane:
    session_name: str
    window_index: str
    window_name: str
    pane_index: str
    pane_id: str
    pane_tty: str | None
    pane_current_path: str | None = None


@dataclass
class SessionRecord:
    tool: str
    session_id: str
    path: Path | None
    last_write: float | None
    cwd: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    matched_process: ProcessInfo | None = None
    tmux_pane: TmuxPane | None = None
    score: int = 0
    status: str = "stale"
    reasons: list[str] = field(default_factory=list)
