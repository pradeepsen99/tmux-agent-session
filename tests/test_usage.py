from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import time
from pathlib import Path

import pytest

from tmux_agent_session import cli
from tmux_agent_session.usage import (
    TokenUsage,
    UsageTracker,
    claude_usage,
    default_usage_dirs,
    format_tokens,
    parse_usage_line,
    usage_line,
)


NOW = dt.datetime(2026, 10, 21, 12, 0, tzinfo=dt.timezone.utc).timestamp()


def iso(hours_ago: float) -> str:
    moment = dt.datetime.fromtimestamp(NOW - hours_ago * 3600, tz=dt.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def assistant(message_id: str | None, request_id: str | None, hours_ago: float, **usage: int) -> str:
    message: dict = {"role": "assistant", "model": "claude-opus", "usage": usage}
    if message_id is not None:
        message["id"] = message_id
    entry: dict = {"type": "assistant", "timestamp": iso(hours_ago), "message": message}
    if request_id is not None:
        entry["requestId"] = request_id
    return json.dumps(entry)


def write_lines(path: Path, *lines: str, mode: str = "w") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
    os.utime(path, (NOW, NOW))


def test_parse_usage_line_extracts_counts_and_key() -> None:
    line = assistant("m1", "r1", 1, input_tokens=3, output_tokens=4,
                     cache_creation_input_tokens=5, cache_read_input_tokens=6).encode()

    key, ts, usage = parse_usage_line(line)

    assert key == ("m1", "r1")
    assert ts == pytest.approx(NOW - 3600)
    assert (usage.input_tokens, usage.output_tokens) == (3, 4)
    assert (usage.cache_creation_tokens, usage.cache_read_tokens) == (5, 6)
    assert usage.responses == 1


@pytest.mark.parametrize("line", [
    b"not json",
    b'{"type": "user", "message": {"role": "user", "content": "usage"}}',
    b'{"message": {"usage": {"input_tokens": 1}}}',
    b'{"timestamp": "garbage", "message": {"usage": {"input_tokens": 1}}}',
])
def test_parse_usage_line_skips_unusable_lines(line: bytes) -> None:
    assert parse_usage_line(line) is None


def test_usage_dedupes_filters_window_and_includes_subagents(tmp_path: Path) -> None:
    base = tmp_path / "projects"
    write_lines(
        base / "-tmp-app" / "session.jsonl",
        assistant("m1", "r1", 1, input_tokens=10, output_tokens=100, cache_read_input_tokens=1000),
        # Same response written again for a second content block.
        assistant("m1", "r1", 1, input_tokens=10, output_tokens=100, cache_read_input_tokens=1000),
        assistant("m0", "r0", 30, input_tokens=999_999),
        "not json",
        json.dumps({"type": "user", "timestamp": iso(1), "message": {"role": "user", "content": "hi"}}),
    )
    write_lines(
        base / "-tmp-app" / "session" / "subagents" / "agent.jsonl",
        assistant("m2", "r2", 2, input_tokens=5, output_tokens=50, cache_creation_input_tokens=7),
    )
    # A resumed session copies earlier history into a new file.
    write_lines(base / "-tmp-app" / "resumed.jsonl",
                assistant("m1", "r1", 1, input_tokens=10, output_tokens=100, cache_read_input_tokens=1000))

    usage = claude_usage([base], window_hours=24, now=NOW)

    assert usage == TokenUsage(input_tokens=15, output_tokens=150, cache_creation_tokens=7,
                               cache_read_tokens=1000, responses=2)
    assert usage.io_tokens == 165
    assert usage.total_tokens == 1172


def test_usage_ignores_files_untouched_within_window(tmp_path: Path) -> None:
    path = tmp_path / "projects" / "old" / "session.jsonl"
    write_lines(path, assistant("m1", "r1", 1, input_tokens=10))
    os.utime(path, (NOW - 48 * 3600, NOW - 48 * 3600))

    assert claude_usage([tmp_path / "projects"], now=NOW).responses == 0


def test_usage_counts_id_less_lines_once(tmp_path: Path) -> None:
    base = tmp_path / "projects"
    path = base / "p" / "s.jsonl"
    write_lines(path, assistant(None, None, 1, output_tokens=8))
    tracker = UsageTracker([base])

    assert tracker.scan(NOW).output_tokens == 8
    os.utime(path, (NOW + 1, NOW + 1))
    assert tracker.scan(NOW + 1).output_tokens == 8


def test_tracker_reads_only_appended_lines_and_rolls_window(tmp_path: Path) -> None:
    base = tmp_path / "projects"
    path = base / "p" / "s.jsonl"
    write_lines(path, assistant("m1", "r1", 23.5, output_tokens=10))
    tracker = UsageTracker([base], window_hours=24)
    assert tracker.scan(NOW).output_tokens == 10

    # A partial trailing line waits until it is complete.
    with path.open("a", encoding="utf-8") as f:
        f.write(assistant("m2", "r2", 0, output_tokens=5) + "\n" + '{"partial": ')
    os.utime(path, (NOW, NOW))
    assert tracker.scan(NOW).output_tokens == 15

    with path.open("a", encoding="utf-8") as f:
        f.write("true}\n")
    # An hour later the first response has left the window.
    later = NOW + 3600
    assert tracker.scan(later) == TokenUsage(output_tokens=5, responses=1)


def test_tracker_rereads_truncated_files(tmp_path: Path) -> None:
    base = tmp_path / "projects"
    path = base / "p" / "s.jsonl"
    write_lines(path, assistant("m1", "r1", 1, input_tokens=1), assistant("m2", "r2", 1, input_tokens=2))
    tracker = UsageTracker([base])
    assert tracker.scan(NOW).input_tokens == 3

    write_lines(path, assistant("m3", "r3", 0, input_tokens=4))
    assert tracker.scan(NOW).input_tokens == 7


def test_default_usage_dirs_honors_claude_config_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", f"{tmp_path / 'a'},{tmp_path / 'b'}")
    assert default_usage_dirs() == [tmp_path / "a" / "projects", tmp_path / "b" / "projects"]

    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    dirs = default_usage_dirs()
    assert dirs[0] == Path("~/.claude/projects").expanduser()
    assert dirs[1] == Path("~/.config/claude/projects").expanduser()


@pytest.mark.parametrize("count, text", [
    (0, "0"), (950, "950"), (1000, "1k"), (12_345, "12.3k"),
    (4_500_000, "4.5M"), (123_400_000, "123M"), (2_000_000_000, "2B"),
])
def test_format_tokens(count: int, text: str) -> None:
    assert format_tokens(count) == text


def test_usage_line() -> None:
    usage = TokenUsage(input_tokens=200_000, output_tokens=1_000_000, cache_read_tokens=48_000_000)
    assert usage_line(usage) == "Claude 24h: 1.2M in/out · 48M cache"


def test_main_usage_outputs(monkeypatch, capsys, tmp_path: Path) -> None:
    base = tmp_path / "projects"
    # The CLI uses the real clock, so stamp this response "now".
    hours_ago = (NOW - time.time()) / 3600
    write_lines(base / "p" / "s.jsonl", assistant("m1", "r1", hours_ago, input_tokens=1200, output_tokens=34))
    os.utime(base / "p" / "s.jsonl")

    monkeypatch.setattr(cli.sys, "argv", ["tas", "--usage", "--json", "--claude-dir", str(base)])
    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["input_tokens"] == 1200
    assert payload["io_tokens"] == 1234
    assert payload["window_hours"] == 24

    monkeypatch.setattr(cli.sys, "argv", ["tas", "--usage-line", "--claude-dir", str(base)])
    assert cli.main() == 0
    assert capsys.readouterr().out.strip() == "Claude 24h: 1.2k in/out · 0 cache"

    monkeypatch.setattr(cli.sys, "argv", ["tas", "--usage", "--claude-dir", str(base)])
    assert cli.main() == 0
    out = capsys.readouterr().out
    assert "last 24h" in out
    assert "1,234" in out


@pytest.mark.parametrize("argv", [["--pick", "--usage"], ["--usage", "--usage-hours", "0"]])
def test_main_rejects_invalid_usage_flags(monkeypatch, argv: list[str]) -> None:
    monkeypatch.setattr(cli.sys, "argv", ["tas", *argv])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 2


def test_main_passes_usage_callback_to_picker(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(cli, "run_picker", lambda records, **kwargs: captured.update(kwargs) or 0)
    monkeypatch.setattr(cli.sys, "argv", ["tas", "--pick"])

    assert cli.main() == 0
    assert callable(captured["usage_callback"])


def test_picker_shows_usage_in_header() -> None:
    async def scenario() -> None:
        calls: list[int] = []

        def usage() -> str:
            calls.append(1)
            return "Claude 24h: 1.2M in/out · 48M cache"

        app = cli.SessionPickerApp([], details_callback=lambda _rec: {}, usage_callback=usage)
        async with app.run_test(size=(120, 30)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert str(app.query_one("#usage").content) == "Claude 24h: 1.2M in/out · 48M cache"
            await pilot.press("r")
            await app.workers.wait_for_complete()
            assert len(calls) == 2
            await pilot.press("q")

    asyncio.run(scenario())


def test_picker_survives_usage_errors() -> None:
    async def scenario() -> None:
        def broken() -> str:
            raise OSError("unreadable")

        app = cli.SessionPickerApp([], details_callback=lambda _rec: {}, usage_callback=broken)
        async with app.run_test(size=(120, 30)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert str(app.query_one("#usage").content) == ""
            await pilot.press("q")
        assert app.return_value == 1

    asyncio.run(scenario())
