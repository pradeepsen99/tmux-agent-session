from __future__ import annotations

import datetime as dt
import json
from typing import Any

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .models import SessionRecord
from .tmux import tmux_target


# Ranked by utility in the picker: these fields are the most useful for deciding
# which session to jump into without having to inspect the backing files.
PICKER_METADATA_PRIMARY = [
    ("Model", ("model",)),
    ("Summary", ("summary", "title")),
    ("Approval", ("approval_policy",)),
    ("Activity", ("timestamp", "updated_at", "created_at")),
    ("Origin", ("originator", "source")),
]
PICKER_METADATA_SECONDARY = [
    ("Provider", ("model_provider",)),
    ("CLI", ("cli_version",)),
    ("Style", ("personality",)),
    ("State", ("status",)),
]


def format_ts(ts: float | None) -> str:
    if ts is None:
        return "—"
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def format_iso_ts(raw: str) -> str | None:
    text = raw.strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except ValueError:
        return text


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if sec or not parts:
        parts.append(f"{sec}s")
    return " ".join(parts)


def truncate(text: str | None, width: int) -> str:
    if not text:
        return "—"
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def pad(text: str | None, width: int) -> str:
    return truncate(text, width).ljust(width)


def metadata_text(key: str, value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if key in {"summary", "title"} and text.lower() in {"none", "null"}:
            return None
        if key in {"timestamp", "updated_at", "created_at"}:
            return format_iso_ts(text)
        return text
    if isinstance(value, (int, float, bool)):
        return str(value)
    return None


def first_metadata_value(rec: SessionRecord, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        text = metadata_text(key, rec.metadata.get(key))
        if text:
            return text
    return None


def joined_metadata_value(rec: SessionRecord, keys: tuple[str, ...]) -> str | None:
    values: list[str] = []
    for key in keys:
        text = metadata_text(key, rec.metadata.get(key))
        if text and text not in values:
            values.append(text)
    if not values:
        return None
    return " / ".join(values)


def picker_metadata_items(rec: SessionRecord, limit: int = 5) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    used_labels: set[str] = set()

    for label, keys in PICKER_METADATA_PRIMARY:
        value = (
            joined_metadata_value(rec, keys)
            if label == "Origin"
            else first_metadata_value(rec, keys)
        )
        if value:
            items.append((label, value))
            used_labels.add(label)

    for label, keys in PICKER_METADATA_SECONDARY:
        if len(items) >= limit or label in used_labels:
            break
        value = first_metadata_value(rec, keys)
        if value:
            items.append((label, value))

    return items[:limit]


def display_cwd(rec: SessionRecord) -> str | None:
    return rec.cwd or (rec.matched_process.cwd if rec.matched_process else None)


def display_model(rec: SessionRecord) -> str | None:
    model = first_metadata_value(rec, ("model",))
    if model:
        return model
    provider = first_metadata_value(rec, ("model_provider",))
    return provider


STATUS_STYLES = {
    "active": "bold green",
    "recent": "yellow",
    "stale": "dim cyan",
}


def status_text(status: str) -> Text:
    return Text(status, style=STATUS_STYLES.get(status, ""))


def process_summary(rec: SessionRecord) -> str:
    if rec.matched_process is None:
        return "—"
    parts = [f"pid {rec.matched_process.pid}"]
    if rec.matched_process.tty:
        parts.append(rec.matched_process.tty)
    runtime = format_duration(rec.matched_process.etime_seconds)
    if runtime != "—":
        parts.append(runtime)
    return " ".join(parts)


def record_details(rec: SessionRecord) -> str:
    details: list[str] = []
    model = display_model(rec)
    if model:
        details.append(f"model {model}")
    process = process_summary(rec)
    if process != "—":
        details.append(process)
    updated = format_ts(rec.last_write)
    if updated != "—":
        details.append(updated)
    summary = first_metadata_value(rec, ("summary", "title"))
    if summary:
        details.append(summary)
    return " | ".join(details) if details else "—"


def print_table(
    records: list[SessionRecord], console: Console | None = None
) -> None:
    table = Table(
        box=box.ASCII,
        expand=True,
        show_lines=False,
        header_style="bold",
    )
    table.add_column("TOOL", no_wrap=True)
    table.add_column("STATUS", no_wrap=True)
    table.add_column("TARGET", no_wrap=True)
    table.add_column("SESSION_ID", no_wrap=True, max_width=24, overflow="ellipsis")
    table.add_column("CWD", ratio=1, min_width=10, overflow="ellipsis")
    table.add_column("DETAILS", ratio=2, min_width=18, overflow="fold")

    for rec in records:
        table.add_row(
            rec.tool,
            status_text(rec.status),
            tmux_target(rec),
            rec.session_id,
            display_cwd(rec) or "—",
            record_details(rec),
        )

    (console or Console()).print(table)


def print_json(records: list[SessionRecord]) -> None:
    payload = []
    for rec in records:
        payload.append(
            {
                "tool": rec.tool,
                "status": rec.status,
                "session_id": rec.session_id,
                "path": str(rec.path) if rec.path is not None else None,
                "last_write": format_ts(rec.last_write),
                "cwd": rec.cwd,
                "metadata": rec.metadata,
                "score": rec.score,
                "reasons": rec.reasons,
                "tmux": None
                if rec.tmux_pane is None
                else {
                    "session_name": rec.tmux_pane.session_name,
                    "window_index": rec.tmux_pane.window_index,
                    "window_name": rec.tmux_pane.window_name,
                    "pane_index": rec.tmux_pane.pane_index,
                    "pane_id": rec.tmux_pane.pane_id,
                    "pane_tty": rec.tmux_pane.pane_tty,
                    "target": tmux_target(rec),
                },
                "process": None
                if rec.matched_process is None
                else {
                    "pid": rec.matched_process.pid,
                    "ppid": rec.matched_process.ppid,
                    "tty": rec.matched_process.tty,
                    "cwd": rec.matched_process.cwd,
                    "etime_seconds": rec.matched_process.etime_seconds,
                    "command": rec.matched_process.command,
                },
            }
        )
    print(json.dumps(payload, indent=2))
