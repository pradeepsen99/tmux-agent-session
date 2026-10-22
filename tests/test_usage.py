from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from tmux_agent_session import cli
from tmux_agent_session.usage import ClaudeUsageTracker, UsageEntry, UsageTotals


NOW = 1_800_000_000.0


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def assistant_line(message_id: str, ts: float, model: str = "claude-opus-5-5", **usage) -> str:
    return json.dumps({
        "type": "assistant",
        "timestamp": iso(ts),
        "requestId": f"req_{message_id}",
        "message": {"id": message_id, "model": model, "usage": usage},
    })


def test_entry_cost_uses_cache_ttl_split() -> None:
    entry = UsageEntry.from_usage(NOW, "claude-opus-5-5", {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
        "cache_creation": {"ephemeral_5m_input_tokens": 1_000_000, "ephemeral_1h_input_tokens": 1_000_000},
    })
    # 4 input + 20 output + 0.20 read + 5 (1.25x) + 8 (2x)
    assert entry.cost() == pytest.approx(37.20)


def test_entry_cost_handles_dated_ids_fast_mode_and_unknown_models() -> None:
    usage = {"input_tokens": 1_000_000}
    assert UsageEntry.from_usage(NOW, "claude-haiku-4-5-20251001", usage).cost() == pytest.approx(1.00)
    assert UsageEntry.from_usage(NOW, "claude-opus-5", usage).cost() == pytest.approx(5.00)
    fast = UsageEntry.from_usage(NOW, "claude-opus-5-5", {**usage, "speed": "fast"})
    assert fast.cost() == pytest.approx(8.00)
    assert UsageEntry.from_usage(NOW, "gpt-5", usage).cost() is None


def test_haiku_long_context_tier() -> None:
    short = UsageEntry.from_usage(NOW, "claude-haiku-5-5", {"input_tokens": 100_000})
    long = UsageEntry.from_usage(NOW, "claude-haiku-5-5", {"input_tokens": 200_000})
    assert short.cost() == pytest.approx(0.01)
    assert long.cost() == pytest.approx(0.10)


def test_tracker_dedupes_responses_and_honors_window(tmp_path: Path) -> None:
    now = time.time()
    project = tmp_path / "-tmp-project"
    (project / "session" / "subagents").mkdir(parents=True)
    (project / "session.jsonl").write_text("\n".join([
        json.dumps({"type": "user", "message": {"content": "hi"}}),
        # One response logged across two content-block lines.
        assistant_line("msg_a", now - 60, input_tokens=10, output_tokens=5),
        assistant_line("msg_a", now - 60, input_tokens=10, output_tokens=5),
        assistant_line("msg_old", now - 2 * 86400, input_tokens=999, output_tokens=999),
        assistant_line("msg_syn", now - 60, model="<synthetic>", input_tokens=7),
        "not json",
    ]) + "\n")
    (project / "session" / "subagents" / "agent-1.jsonl").write_text(
        assistant_line("msg_b", now - 120, model="claude-sonnet-5-5", cache_read_input_tokens=100) + "\n"
    )
    # A resumed session that replays msg_a must not double count it.
    (project / "resumed.jsonl").write_text(assistant_line("msg_a", now - 60, input_tokens=10, output_tokens=5) + "\n")

    tracker = ClaudeUsageTracker([tmp_path])
    for _ in range(2):  # second pass is served from the per-file cache
        totals = tracker.totals(now=now)
        assert (totals.input_tokens, totals.output_tokens, totals.cache_read_tokens) == (10, 5, 100)
        assert totals.total_tokens == 115
        assert totals.cost == pytest.approx((10 * 4 + 5 * 20 + 100 * 0.20) / 1_000_000)
        assert totals.unpriced_models == set()

    # Transcripts untouched for over a day are skipped entirely.
    assert tracker.totals(now=now + 3 * 86400).total_tokens == 0


def test_picker_shows_usage_in_header() -> None:
    totals = UsageTotals(input_tokens=1_000, cache_read_tokens=2_500_000, output_tokens=20_000, cost=12.345)

    async def scenario() -> None:
        app = cli.SessionPickerApp([], details_callback=lambda _rec: {}, usage_callback=lambda: totals)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            text = str(app.query_one("#usage").render())
            assert "2.5M tokens" in text
            assert "$12.35" in text
            await pilot.press("q")

    asyncio.run(scenario())
