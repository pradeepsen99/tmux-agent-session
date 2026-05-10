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
        app = cli.SessionPickerApp([make_record("abc")])
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
            preview_callback=lambda _rec, _limit: ["preview"],
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


def test_textual_picker_caches_tmux_preview_and_uses_responsive_layout() -> None:
    async def scenario() -> None:
        previews: list[tuple[str, int]] = []

        def preview_callback(rec: cli.SessionRecord, limit: int) -> list[str]:
            previews.append((rec.session_id, limit))
            return ["\x1b[31mred\x1b[0m"]

        app = cli.SessionPickerApp(
            [make_record("abc")],
            focus_callback=lambda _rec: True,
            preview_callback=preview_callback,
            preview_limit=7,
        )

        async with app.run_test(size=(70, 24)) as pilot:
            await pilot.pause()
            assert app.query_one("#body").has_class("narrow")
            assert app.preview_cache == {"%abc": ["\x1b[31mred\x1b[0m"]}
            await pilot.press("q")

        assert previews == [("abc", 7)]

    asyncio.run(scenario())
