from __future__ import annotations

import asyncio

from tmux_agent_session import cli


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
            assert app.details_cache[id(record)]["title"] == "Fix [red]literal[/red]"
            assert record.metadata["last_user_prompt"] == "Please fix it"
            assert not app.query("#preview")
            app.update_selected_record()
            await pilot.press("q")
        assert loaded == ["abc"]

    asyncio.run(scenario())


def test_textual_picker_expands_details_panel_on_wide_layout() -> None:
    async def scenario() -> None:
        app = cli.SessionPickerApp(
            [make_record("abc")],
            focus_callback=lambda _rec: True,
            details_callback=lambda _rec: {"title": "Session title"},
        )

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            sessions = app.query_one("#sessions")
            sidebar = app.query_one("#sidebar")
            assert sidebar.region.x == sessions.region.x + sessions.region.width
            assert sidebar.region.width > sessions.region.width
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
