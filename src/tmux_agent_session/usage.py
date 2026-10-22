"""Rolling Claude Code token usage with estimated API cost from transcripts."""
from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .session_cache import fingerprint


USAGE_WINDOW_SECONDS = 24 * 60 * 60
USAGE_HISTORY_DAYS = 14


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens. Cache writes bill at 1.25x input (5m TTL) or 2x input (1h TTL)."""

    input: float
    output: float
    cache_read: float

    @property
    def cache_write_5m(self) -> float:
        return self.input * 1.25

    @property
    def cache_write_1h(self) -> float:
        return self.input * 2


# Anthropic first-party API list prices as of 2026-10. Keys are model id prefixes;
# the longest match wins so dated ids like claude-haiku-4-5-20251001 resolve.
CLAUDE_PRICES: dict[str, ModelPrice] = {
    "claude-fable-5-1": ModelPrice(10.00, 50.00, 0.25),
    "claude-mythos-5-1": ModelPrice(10.00, 50.00, 0.25),
    "claude-fable-5": ModelPrice(10.00, 50.00, 1.00),
    "claude-mythos-5": ModelPrice(10.00, 50.00, 1.00),
    "claude-opus-5-5": ModelPrice(4.00, 20.00, 0.20),
    "claude-opus-5": ModelPrice(5.00, 25.00, 0.50),
    "claude-opus-4-8": ModelPrice(5.00, 25.00, 0.50),
    "claude-opus-4-7": ModelPrice(5.00, 25.00, 0.50),
    "claude-opus-4-6": ModelPrice(5.00, 25.00, 0.50),
    "claude-opus-4-5": ModelPrice(5.00, 25.00, 0.50),
    "claude-sonnet-5-5": ModelPrice(2.00, 10.00, 0.20),
    "claude-sonnet-5": ModelPrice(2.00, 10.00, 0.20),
    "claude-sonnet-4-6": ModelPrice(3.00, 15.00, 0.30),
    "claude-sonnet-4-5": ModelPrice(3.00, 15.00, 0.30),
    "claude-haiku-5-5": ModelPrice(0.10, 0.50, 0.01),
    "claude-haiku-4-5": ModelPrice(1.00, 5.00, 0.10),
}
# Claude Haiku 5.5 bills prompts over 100K tokens at a higher tier.
LONG_CONTEXT_PRICES: dict[str, tuple[int, ModelPrice]] = {
    "claude-haiku-5-5": (100_000, ModelPrice(0.50, 2.50, 0.05)),
}
# Fast mode (Opus 5 / Opus 5.5) runs at 2x standard rates.
FAST_MODE_MULTIPLIER = 2.0


def _price_key(model: str) -> str | None:
    matches = [key for key in CLAUDE_PRICES if model == key or model.startswith(key + "-")]
    return max(matches, key=len) if matches else None


def _int(value: Any) -> int:
    return value if isinstance(value, int) and value > 0 else 0


@dataclass(frozen=True)
class UsageEntry:
    timestamp: float
    model: str
    input_tokens: int
    cache_write_5m_tokens: int
    cache_write_1h_tokens: int
    cache_read_tokens: int
    output_tokens: int
    fast: bool = False

    @classmethod
    def from_usage(cls, timestamp: float, model: str, usage: dict[str, Any]) -> UsageEntry:
        creation = usage.get("cache_creation")
        if isinstance(creation, dict):
            write_5m = _int(creation.get("ephemeral_5m_input_tokens"))
            write_1h = _int(creation.get("ephemeral_1h_input_tokens"))
        else:
            write_5m = _int(usage.get("cache_creation_input_tokens"))
            write_1h = 0
        return cls(
            timestamp=timestamp,
            model=model,
            input_tokens=_int(usage.get("input_tokens")),
            cache_write_5m_tokens=write_5m,
            cache_write_1h_tokens=write_1h,
            cache_read_tokens=_int(usage.get("cache_read_input_tokens")),
            output_tokens=_int(usage.get("output_tokens")),
            fast=usage.get("speed") == "fast",
        )

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_write_5m_tokens + self.cache_write_1h_tokens + self.cache_read_tokens

    def cost(self) -> float | None:
        key = _price_key(self.model)
        if key is None:
            return None
        price = CLAUDE_PRICES[key]
        long_context = LONG_CONTEXT_PRICES.get(key)
        if long_context is not None and self.prompt_tokens > long_context[0]:
            price = long_context[1]
        dollars = (
            self.input_tokens * price.input
            + self.cache_write_5m_tokens * price.cache_write_5m
            + self.cache_write_1h_tokens * price.cache_write_1h
            + self.cache_read_tokens * price.cache_read
            + self.output_tokens * price.output
        ) / 1_000_000
        return dollars * FAST_MODE_MULTIPLIER if self.fast else dollars


@dataclass
class UsageTotals:
    input_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    unpriced_models: set[str] = field(default_factory=set)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.cache_write_tokens + self.cache_read_tokens + self.output_tokens

    def add(self, entry: UsageEntry) -> None:
        self.input_tokens += entry.input_tokens
        self.cache_write_tokens += entry.cache_write_5m_tokens + entry.cache_write_1h_tokens
        self.cache_read_tokens += entry.cache_read_tokens
        self.output_tokens += entry.output_tokens
        cost = entry.cost()
        if cost is None:
            self.unpriced_models.add(entry.model)
        else:
            self.cost += cost


def _parse_timestamp(raw: Any) -> float | None:
    if not isinstance(raw, str):
        return None
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def read_usage_entries(path: Path) -> dict[tuple[str, str], UsageEntry]:
    """Map (message id, request id) -> usage for each assistant response in a transcript.

    Claude writes one line per content block, each repeating the message's usage,
    so entries are keyed to count every response once (the last line wins).
    """
    entries: dict[tuple[str, str], UsageEntry] = {}
    try:
        with path.open("rb") as f:
            for line in f:
                if b'"usage"' not in line:
                    continue
                try:
                    item = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(item, dict) or item.get("type") != "assistant":
                    continue
                message = item.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                model = message.get("model")
                timestamp = _parse_timestamp(item.get("timestamp"))
                if not isinstance(usage, dict) or not isinstance(model, str) or timestamp is None:
                    continue
                if model.startswith("<"):  # e.g. "<synthetic>" placeholder turns
                    continue
                key = (str(message.get("id") or ""), str(item.get("requestId") or item.get("uuid") or ""))
                entries[key] = UsageEntry.from_usage(timestamp, model, usage)
    except OSError:
        return {}
    return entries


def _recent_transcripts(base_paths: Iterable[Path], cutoff: float) -> Iterable[Path]:
    for base in base_paths:
        try:
            paths = list(base.rglob("*.jsonl"))
        except OSError:
            continue
        for path in paths:
            try:
                if path.stat().st_mtime >= cutoff:
                    yield path
            except OSError:
                continue


@dataclass(frozen=True)
class DailyUsage:
    day: dt.date
    totals: UsageTotals
    models: dict[str, UsageTotals] = field(default_factory=dict)


@dataclass
class UsageReport:
    """Rolling-window totals plus one bucket per local calendar day, oldest first, ending today."""

    window: UsageTotals
    days: list[DailyUsage]


class ClaudeUsageTracker:
    """Totals Claude usage over a rolling window and recent days, re-reading only transcripts that changed."""

    def __init__(
        self,
        base_paths: list[Path],
        window_seconds: int = USAGE_WINDOW_SECONDS,
        history_days: int = USAGE_HISTORY_DAYS,
    ) -> None:
        self.base_paths = base_paths
        self.window_seconds = window_seconds
        self.history_days = history_days
        self.file_cache: dict[Path, tuple[tuple | None, dict[tuple[str, str], UsageEntry]]] = {}

    def _entries(self, cutoff: float) -> Iterable[UsageEntry]:
        merged: dict[tuple[str, str], UsageEntry] = {}
        live_cache: dict[Path, tuple[tuple | None, dict[tuple[str, str], UsageEntry]]] = {}
        for path in _recent_transcripts(self.base_paths, cutoff):
            stamp = fingerprint(path)
            cached = self.file_cache.get(path)
            entries = cached[1] if cached is not None and stamp is not None and cached[0] == stamp else read_usage_entries(path)
            live_cache[path] = (stamp, entries)
            # Resumed or forked sessions copy earlier responses into new files.
            merged.update(entries)
        self.file_cache = live_cache
        return merged.values()

    def report(self, now: float | None = None) -> UsageReport:
        now = time.time() if now is None else now
        window_start = now - self.window_seconds
        today = dt.datetime.fromtimestamp(now).date()
        first_day = today - dt.timedelta(days=max(1, self.history_days) - 1)
        history_start = dt.datetime.combine(first_day, dt.time()).timestamp()

        window = UsageTotals()
        days = {
            first_day + dt.timedelta(days=offset): DailyUsage(first_day + dt.timedelta(days=offset), UsageTotals())
            for offset in range((today - first_day).days + 1)
        }
        for entry in self._entries(min(window_start, history_start)):
            if entry.timestamp >= window_start:
                window.add(entry)
            day = days.get(dt.datetime.fromtimestamp(entry.timestamp).date())
            if day is not None:
                day.totals.add(entry)
                day.models.setdefault(entry.model, UsageTotals()).add(entry)
        return UsageReport(window, list(days.values()))

    def totals(self, now: float | None = None) -> UsageTotals:
        return self.report(now).window
