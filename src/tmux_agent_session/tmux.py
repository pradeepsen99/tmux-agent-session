from __future__ import annotations

import subprocess

from .commands import run_command
from .models import SessionRecord, TmuxPane
from .processes import normalize_tty
from .session_files import normalize_cwd


def detect_tmux_panes() -> list[TmuxPane]:
    output = run_command(
        [
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{session_name}\t#{window_index}\t#{window_name}\t#{pane_index}\t#{pane_id}\t#{pane_tty}\t#{pane_current_path}",
        ]
    )
    panes: list[TmuxPane] = []
    for raw_line in output.splitlines():
        line = raw_line.rstrip("\n")
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        (
            session_name,
            window_index,
            window_name,
            pane_index,
            pane_id,
            pane_tty,
            pane_current_path,
        ) = parts
        panes.append(
            TmuxPane(
                session_name=session_name,
                window_index=window_index,
                window_name=window_name,
                pane_index=pane_index,
                pane_id=pane_id,
                pane_tty=normalize_tty(pane_tty),
                pane_current_path=normalize_cwd(pane_current_path),
            )
        )
    return panes


def attach_tmux_panes(records: list[SessionRecord], panes: list[TmuxPane]) -> None:
    panes_by_tty = {pane.pane_tty: pane for pane in panes if pane.pane_tty is not None}
    for rec in records:
        if rec.matched_process is None:
            continue
        rec.tmux_pane = panes_by_tty.get(normalize_tty(rec.matched_process.tty))


def tmux_target(rec: SessionRecord) -> str:
    if rec.tmux_pane is None:
        return "—"
    return (
        f"{rec.tmux_pane.session_name}:{rec.tmux_pane.window_index}."
        f"{rec.tmux_pane.pane_index}"
    )


def focus_tmux_pane(rec: SessionRecord) -> bool:
    if rec.tmux_pane is None:
        return False

    commands = [
        ["tmux", "switch-client", "-t", rec.tmux_pane.session_name],
        [
            "tmux",
            "select-window",
            "-t",
            f"{rec.tmux_pane.session_name}:{rec.tmux_pane.window_index}",
        ],
        ["tmux", "select-pane", "-t", rec.tmux_pane.pane_id],
    ]
    for cmd in commands:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return False
    return True


def capture_tmux_pane_preview(rec: SessionRecord, limit: int = 12) -> list[str]:
    if rec.tmux_pane is None:
        return []

    try:
        result = subprocess.run(
            [
                "tmux",
                "capture-pane",
                "-p",
                "-e",
                "-t",
                rec.tmux_pane.pane_id,
                "-S",
                f"-{max(1, limit)}",
            ],
            capture_output=True,
            text=True,
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []

    lines = [line.rstrip() for line in result.stdout.splitlines()]
    if len(lines) > limit:
        lines = lines[-limit:]
    return lines
