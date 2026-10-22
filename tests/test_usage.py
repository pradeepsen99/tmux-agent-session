from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from pathlib import Path

import pytest

from tmux_agent_session import cli
from tmux_agent_session.usage import ClaudeUsageTracker, DailyUsage, UsageEntry, UsageReport, UsageTotals
from tmux_agent_session.usage_view import bar_cells, day_row, model_breakdown, usage_footnote, usage_overview


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

    # The rolling window moves on even while the transcript stays in the daily history.
    assert tracker.totals(now=now + 3 * 86400).total_tokens == 0


def local_noon(days_ago: int) -> float:
    day = dt.date.today() - dt.timedelta(days=days_ago)
    return dt.datetime.combine(day, dt.time(12)).timestamp()


def test_report_buckets_usage_by_local_day(tmp_path: Path) -> None:
    (tmp_path / "session.jsonl").write_text("\n".join([
        assistant_line("msg_today", local_noon(0), input_tokens=10),
        assistant_line("msg_today_2", local_noon(0) - 3600, output_tokens=5),
        assistant_line("msg_two_days", local_noon(2), model="claude-sonnet-5-5", cache_read_input_tokens=100),
        assistant_line("msg_too_old", local_noon(20), input_tokens=999),
    ]) + "\n")

    report = ClaudeUsageTracker([tmp_path], history_days=7).report(now=local_noon(0) + 60)
    assert [day.day for day in report.days] == [dt.date.today() - dt.timedelta(days=n) for n in range(6, -1, -1)]
    tokens = {day.day: day.totals.total_tokens for day in report.days}
    assert tokens[dt.date.today()] == 15
    assert tokens[dt.date.today() - dt.timedelta(days=2)] == 100
    assert sum(tokens.values()) == 115
    assert report.window.total_tokens == 15
    by_day = {day.day: day for day in report.days}
    today = by_day[dt.date.today()].models
    assert set(today) == {"claude-opus-5-5"} and today["claude-opus-5-5"].total_tokens == 15
    assert set(by_day[dt.date.today() - dt.timedelta(days=2)].models) == {"claude-sonnet-5-5"}
    assert by_day[dt.date.today() - dt.timedelta(days=1)].models == {}


def test_bar_cells_track_cumulative_total() -> None:
    assert bar_cells([50, 50], 100, 10) == [5, 5]
    assert bar_cells([1, 1, 1], 3, 10) == [3, 4, 3]
    assert sum(bar_cells([7, 0, 13, 2], 40, 33)) == round(22 / 40 * 33)
    assert bar_cells([5], 0, 10) == [0]


def sample_report() -> UsageReport:
    today = dt.date.today()
    heavy = UsageTotals(input_tokens=1_000, cache_read_tokens=2_500_000, output_tokens=20_000, cost=12.345)
    light = UsageTotals(input_tokens=400_000, output_tokens=100_000, cost=3.5)
    days = [DailyUsage(today - dt.timedelta(days=n), UsageTotals()) for n in range(13, -1, -1)]
    days[-1] = DailyUsage(today, heavy, {
        "claude-opus-5-5": UsageTotals(input_tokens=1_000, cache_read_tokens=2_000_000, output_tokens=20_000, cost=11.0),
        "claude-haiku-5-5-20260901": UsageTotals(cache_read_tokens=500_000, cost=1.345),
    })
    days[-3] = DailyUsage(today - dt.timedelta(days=2), light, {"claude-sonnet-5-5": light})
    return UsageReport(window=heavy, days=days)


def render_text(*renderables, width: int = 100) -> str:
    from rich.console import Console

    console = Console(width=width, record=True)
    for renderable in renderables:
        console.print(renderable)
    return console.export_text()


def test_usage_renders_tiles_daily_rows_and_model_breakdown() -> None:
    report = sample_report()
    today = report.days[-1].day
    text = render_text(usage_overview(report), usage_footnote(report))
    assert "Last 14 days" in text and "Busiest day" in text
    assert "$15.85" in text  # 14-day total

    scale = max(day.totals.total_tokens for day in report.days)
    assert "› Today" in render_text(day_row(report.days[-1], scale, today, selected=True))
    yesterday = render_text(day_row(report.days[-2], scale, today))
    assert "Yesterday" in yesterday and "›" not in yesterday
    assert "500.0K" in render_text(day_row(report.days[-3], scale, today))

    breakdown = render_text(model_breakdown(report.days[-1], today), width=50)
    assert breakdown.index("opus-5-5") < breakdown.index("haiku-5-5")  # heaviest model first
    assert "20260901" not in breakdown
    assert "80%" in breakdown and "$11.00" in breakdown
    assert "No Claude usage" in render_text(model_breakdown(report.days[-2], today))


def test_picker_shows_usage_in_header_and_usage_tab() -> None:
    report = sample_report()

    async def scenario() -> None:
        app = cli.SessionPickerApp([], details_callback=lambda _rec: {}, usage_callback=lambda: report)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            text = str(app.query_one("#usage").render())
            assert "2.5M tokens" in text
            assert "$12.35" in text

            tabs = app.query_one("#tabs")
            assert tabs.active == "sessions-tab"
            await pilot.click("#tabs-list Tab#--content-tab-usage-tab")
            await pilot.pause()
            assert tabs.active == "usage-tab"
            assert app.focused is app.query_one("#usage-days")
            detail = lambda: render_text(app.query_one("#usage-detail").content)
            assert "opus-5-5" in detail()
            await pilot.press("j")
            assert "No Claude usage" in detail()
            await pilot.press("down")
            assert "100.0K" in detail()
            await pilot.press("1")
            assert tabs.active == "sessions-tab"
            await pilot.press("2")
            assert tabs.active == "usage-tab"
            await pilot.press("q")

    asyncio.run(scenario())
