from __future__ import annotations

import asyncio

from tmux_agent_session import cli
from tmux_agent_session.picker import detail_key


def make_record(session_id: str, *, focusable: bool = True) -> cli.SessionRecord:
    pane = (
        cli.TmuxPane(
            session_name="work",
            window_index="1",
            window_name="editor",
            pane_index="2",
            pane_id=f"%{session_id}",
            pane_tty="ttys001",
            pane_current_path="/tmp/project",
        )
        if focusable
        else None
    )
    return cli.SessionRecord(
        tool="codex",
        session_id=session_id,
        path=None,
        last_write=None,
        cwd="/tmp/project",
        metadata={"model": "gpt-5", "summary": "Investigate issue"},
        status="active",
        tmux_pane=pane,
    )


def test_textual_picker_quits_with_q() -> None:
    async def scenario() -> None:
        app = cli.SessionPickerApp([make_record("abc")], details_callback=lambda _rec: {})
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert app.selected_record().session_id == "abc"
            await pilot.press("q")

        assert app.return_value == 1

    asyncio.run(scenario())


def test_textual_picker_handles_non_focusable_selection_and_focuses_target() -> None:
    async def scenario() -> None:
        records = [make_record("plain", focusable=False), make_record("target")]
        focused: list[str] = []
        app = cli.SessionPickerApp(
            records,
            focus_callback=lambda rec: focused.append(rec.session_id) or True,
            details_callback=lambda _rec: {"title": "Session title"},
        )

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert app.selected_record().session_id == "target"

            await pilot.press("k")
            await pilot.pause()
            assert app.selected_record().session_id == "plain"

            await pilot.press("enter")
            await pilot.pause()
            assert app.query_one("#message").content == (
                "No focusable tmux target for this session."
            )
            assert focused == []

            await pilot.press("j")
            await pilot.pause()
            await pilot.press("enter")

        assert focused == ["target"]
        assert app.return_value == 0

    asyncio.run(scenario())


def test_textual_picker_caches_details_and_uses_responsive_layout() -> None:
    async def scenario() -> None:
        loaded: list[str] = []

        def details_callback(rec: cli.SessionRecord) -> dict[str, str]:
            loaded.append(rec.session_id)
            return {"title": "Fix [red]literal[/red]", "last_user_prompt": "Please fix it"}

        record = make_record("abc")
        app = cli.SessionPickerApp([record], details_callback=details_callback)
        async with app.run_test(size=(70, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.query_one("#body").has_class("narrow")
            assert app.details_cache[detail_key(record)]["title"] == "Fix [red]literal[/red]"
            assert record.metadata["last_user_prompt"] == "Please fix it"
            assert not app.query("#preview")
            app.update_selected_record()
            await pilot.press("q")
        assert loaded == ["abc"]

    asyncio.run(scenario())


def test_textual_picker_favors_session_list_on_wide_layout() -> None:
    async def scenario() -> None:
        app = cli.SessionPickerApp(
            [make_record("abc")],
            focus_callback=lambda _rec: True,
            details_callback=lambda _rec: {"title": "Session title"},
        )

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            sessions = app.query_one("#session-list")
            sidebar = app.query_one("#sidebar")
            assert sidebar.region.x == sessions.region.x + sessions.region.width
            assert sessions.region.width > sidebar.region.width
            await pilot.resize_terminal(60, 24)
            await pilot.pause()
            assert app.query_one("#body").has_class("narrow")
            assert sidebar.region.y >= sessions.region.bottom
            await pilot.press("q")

    asyncio.run(scenario())


def test_textual_picker_message_calls_out_feedback_required() -> None:
    async def scenario() -> None:
        rec = make_record("abc")
        rec.status = "waiting"
        rec.requires_user_feedback = True
        app = cli.SessionPickerApp(
            [rec],
            focus_callback=lambda _rec: True,
            details_callback=lambda _rec: {},
        )

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert app.query_one("#message").content.startswith("Feedback required.")
            await pilot.press("q")

    asyncio.run(scenario())


def test_slow_details_do_not_block_navigation_or_replace_new_selection() -> None:
    import threading

    async def scenario() -> None:
        release = threading.Event()
        started = threading.Event()
        records = [make_record("slow"), make_record("fast")]

        def load(rec: cli.SessionRecord) -> dict[str, str]:
            if rec.session_id == "slow":
                started.set()
                release.wait(timeout=5)
            return {"title": rec.session_id + " title"}

        app = cli.SessionPickerApp(records, details_callback=load)
        async with app.run_test(size=(120, 30)) as pilot:
            try:
                await asyncio.to_thread(started.wait, 2)
                await pilot.press("j")
                await pilot.pause()
                assert app.selected_record() is records[1]
                release.set()
                await app.workers.wait_for_complete()
                await pilot.pause()
                table = app.query_one("#details").content
                from rich.console import Console
                console = Console(width=100)
                with console.capture() as capture:
                    console.print(table)
                assert "fast title" in capture.get()
                assert "slow title" not in capture.get()
                await pilot.press("q")
            finally:
                release.set()

    asyncio.run(scenario())


def test_picker_groups_directories_and_focuses_the_displayed_record() -> None:
    async def scenario() -> None:
        records = [make_record("z-first"), make_record("a"), make_record("z-second")]
        for rec, cwd in zip(records, ["/tmp/zeta", "/tmp/alpha", "/tmp/zeta"]):
            rec.cwd = cwd
        focused: list[str] = []
        app = cli.SessionPickerApp(
            records,
            focus_callback=lambda rec: focused.append(rec.session_id) or True,
            details_callback=lambda rec: {"title": rec.session_id},
        )
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            table = app.query_one("#sessions")
            assert table.get_option("directory-0").prompt.plain == "alpha  1"
            assert table.get_option("directory-1").prompt.plain == "\nzeta  2"
            assert table.get_option("directory-0").disabled
            assert table.get_option("directory-1").disabled
            assert app.selected_record() is records[1]
            await pilot.press("down")
            assert app.selected_record() is records[0]
            await pilot.press("j")
            assert app.selected_record() is records[2]
            await pilot.press("k")
            assert app.selected_record() is records[0]
            await pilot.press("enter")
        assert focused == ["z-first"]
        assert [rec.session_id for rec in records] == ["z-first", "a", "z-second"]

    asyncio.run(scenario())


def test_picker_empty_and_small_layout() -> None:
    async def scenario() -> None:
        app = cli.SessionPickerApp([], details_callback=lambda _rec: {})
        async with app.run_test(size=(60, 20)) as pilot:
            await pilot.pause()
            assert app.selected_record() is None
            assert app.query_one("#body").has_class("narrow")
            await pilot.press("down", "enter")
            assert app.query_one("#message").content == "No sessions available. Press q to exit."
            await pilot.press("escape")
        assert app.return_value == 1

    asyncio.run(scenario())


def test_picker_loads_and_refreshes_in_background() -> None:
    async def scenario():
        snapshots = [[make_record("abc"), make_record("def")], [make_record("def")], []]
        app = cli.SessionPickerApp([], records_callback=lambda: snapshots.pop(0), details_callback=lambda _: {})
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.selected_record().session_id == "abc"
            await pilot.press("j")
            await pilot.press("r")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.selected_record().session_id == "def"
            await pilot.press("r")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.selected_record() is None
            await pilot.press("q")
    asyncio.run(scenario())


def test_picker_failed_refresh_prevents_stale_focus() -> None:
    async def scenario():
        def fail():
            raise OSError("unavailable")
        focused = []
        app = cli.SessionPickerApp([make_record("abc")], records_callback=fail,
                                  focus_callback=lambda rec: focused.append(rec), details_callback=lambda _: {})
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("enter")
            assert not focused
            await pilot.press("q")
    asyncio.run(scenario())


def test_picker_can_quit_while_loading() -> None:
    import threading
    async def scenario():
        release = threading.Event()
        def load():
            release.wait(5)
            return [make_record("late")]
        app = cli.SessionPickerApp([], records_callback=load)
        try:
            async with app.run_test() as pilot:
                assert app.loading
                await pilot.press("q")
                assert app.return_value == 1
        finally:
            release.set()
    asyncio.run(scenario())


def test_picker_discards_old_snapshot_and_detail_response() -> None:
    async def scenario():
        app = cli.SessionPickerApp([make_record("abc")], details_callback=lambda _: {})
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            old = app.selected_record()
            generation = app.load_generation
            app.load_generation += 1
            app.records_callback = lambda: [make_record("outdated")]
            await app.fetch_records(generation)
            assert app.selected_record() is old
            app.details_cache.clear()
            await app.fetch_details(old, detail_key(old), generation)
            assert not app.details_cache
            await pilot.press("q")
    asyncio.run(scenario())
