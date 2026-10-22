"""Rolling token usage totals from Claude Code transcripts.

Claude Code appends every API response to ``~/.claude/projects/**/*.jsonl``
with a ``message.usage`` block. One response is often written several times
(once per content block), and resumed or forked sessions copy earlier history
into a new file, so entries are de-duplicated on ``message.id`` + ``requestId``
across all files.

``UsageTracker`` reads each transcript incrementally: it remembers how far it
has read and only parses newly appended lines on the next scan, so it is cheap
to call on a timer from the picker.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .session_files import safe_mtime


DEFAULT_WINDOW_HOURS = 24


def default_usage_dirs() -> list[Path]:
    """Claude transcript roots, honouring ``CLAUDE_CONFIG_DIR`` when set."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if configured:
        bases = [Path(part).expanduser() for part in configured.split(",") if part.strip()]
    else:
        bases = [Path("~/.claude").expanduser(), Path("~/.config/claude").expanduser()]
    return [base / "projects" for base in bases]


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    responses: int = 0

    @property
    def io_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cache_tokens(self) -> int:
        return self.cache_creation_tokens + self.cache_read_tokens

    @property
    def total_tokens(self) -> int:
        return self.io_tokens + self.cache_tokens

    def add(self, other: TokenUsage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.responses += other.responses

    def to_dict(self) -> dict[str, int]:
        data = asdict(self)
        data["io_tokens"] = self.io_tokens
        data["cache_tokens"] = self.cache_tokens
        data["total_tokens"] = self.total_tokens
        return data


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _parse_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_usage_line(line: bytes) -> tuple[tuple[str, str] | None, float, TokenUsage] | None:
    """Return ``(dedupe_key, timestamp, usage)`` for an assistant line with usage."""
    if b'"usage"' not in line:
        return None
    try:
        entry = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(entry, dict):
        return None
    message = entry.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return None
    ts = _parse_timestamp(entry.get("timestamp"))
    if ts is None:
        return None

    message_id = message.get("id")
    request_id = entry.get("requestId")
    key = (
        (message_id, request_id if isinstance(request_id, str) else "")
        if isinstance(message_id, str) and message_id
        else None
    )
    return key, ts, TokenUsage(
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        cache_creation_tokens=_int(usage.get("cache_creation_input_tokens")),
        cache_read_tokens=_int(usage.get("cache_read_input_tokens")),
        responses=1,
    )


@dataclass
class _FileState:
    identity: tuple[int, int]
    offset: int = 0


class UsageTracker:
    """Incrementally accumulates Claude token usage over a rolling window."""

    def __init__(
        self,
        base_paths: list[Path] | None = None,
        window_hours: float = DEFAULT_WINDOW_HOURS,
    ) -> None:
        self.base_paths = list(base_paths) if base_paths is not None else default_usage_dirs()
        self.window_seconds = window_hours * 3600
        self._files: dict[Path, _FileState] = {}
        # Each key keeps the first copy seen; later duplicates are ignored.
        self._entries: dict[tuple[str, str], tuple[float, TokenUsage]] = {}
        # The picker scans from worker threads; a slow scan can overlap the next one.
        self._lock = threading.Lock()

    def _transcripts(self, cutoff: float) -> list[Path]:
        paths: list[Path] = []
        for base in self.base_paths:
            if not base.is_dir():
                continue
            try:
                for path in base.rglob("*.jsonl"):
                    mtime = safe_mtime(path)
                    if mtime is not None and mtime >= cutoff:
                        paths.append(path)
            except OSError:
                continue
        return paths

    def _read_new_lines(self, path: Path) -> None:
        try:
            stat = path.stat()
        except OSError:
            return
        identity = (stat.st_dev, stat.st_ino)
        state = self._files.get(path)
        if state is None or state.identity != identity or stat.st_size < state.offset:
            # New, replaced, or truncated file: read from the start. Dedupe keys
            # keep re-read lines from being counted twice.
            state = _FileState(identity=identity)
            self._files[path] = state
        if stat.st_size == state.offset:
            return
        try:
            with path.open("rb") as f:
                f.seek(state.offset)
                chunk = f.read(stat.st_size - state.offset)
        except OSError:
            return
        # Leave a trailing partial line for the next scan.
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        line_offset = state.offset
        state.offset += end + 1
        for line in chunk[:end].split(b"\n"):
            parsed = parse_usage_line(line)
            if parsed is not None:
                key, ts, usage = parsed
                # Lines without a message id are keyed by position instead.
                key = key or (str(path), str(line_offset))
                self._entries.setdefault(key, (ts, usage))
            line_offset += len(line) + 1

    def scan(self, now: float | None = None) -> TokenUsage:
        """Read new transcript lines and return usage within the window."""
        now = time.time() if now is None else now
        cutoff = now - self.window_seconds
        with self._lock:
            for path in self._transcripts(cutoff):
                self._read_new_lines(path)
            self._prune(cutoff)
            return self.totals(now)

    def totals(self, now: float | None = None) -> TokenUsage:
        now = time.time() if now is None else now
        cutoff = now - self.window_seconds
        total = TokenUsage()
        for ts, usage in self._entries.values():
            if ts >= cutoff:
                total.add(usage)
        return total

    def _prune(self, cutoff: float) -> None:
        # Keep a little history past the window so a key that ages out is not
        # re-counted if its file is re-read from the start.
        horizon = cutoff - self.window_seconds
        self._entries = {k: v for k, v in self._entries.items() if v[0] >= horizon}
        self._files = {path: state for path, state in self._files.items() if path.exists()}


def claude_usage(
    base_paths: list[Path] | None = None,
    window_hours: float = DEFAULT_WINDOW_HOURS,
    now: float | None = None,
) -> TokenUsage:
    return UsageTracker(base_paths, window_hours).scan(now)


def format_tokens(count: int) -> str:
    """Compact token count: 950, 12.3k, 4.5M, 1.2B."""
    for suffix, size in (("B", 1_000_000_000), ("M", 1_000_000), ("k", 1_000)):
        if count >= size:
            value = count / size
            text = f"{value:.1f}" if value < 100 else f"{value:.0f}"
            return text.removesuffix(".0") + suffix
    return str(count)


def usage_line(usage: TokenUsage, window_hours: float = DEFAULT_WINDOW_HOURS) -> str:
    """One-line summary suitable for a tmux status bar or the picker header."""
    window = f"{window_hours:g}h"
    return (
        f"Claude {window}: {format_tokens(usage.io_tokens)} in/out"
        f" · {format_tokens(usage.cache_tokens)} cache"
    )


def print_usage(usage: TokenUsage, window_hours: float = DEFAULT_WINDOW_HOURS, console: Any | None = None) -> None:
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    output = console or Console()
    output.print(Text(f"Claude token usage  ·  last {window_hours:g}h", style="bold"))
    table = Table(box=None, padding=(0, 2), show_header=False)
    table.add_column(style="dim")
    table.add_column(justify="right")
    for label, value in (
        ("Input", usage.input_tokens),
        ("Output", usage.output_tokens),
        ("Cache write", usage.cache_creation_tokens),
        ("Cache read", usage.cache_read_tokens),
    ):
        table.add_row(label, f"{value:,}")
    table.add_row(Text("Total", style="bold"), Text(f"{usage.total_tokens:,}", style="bold"))
    table.add_row("Responses", f"{usage.responses:,}")
    output.print(table)
