from __future__ import annotations

import textwrap
from collections.abc import Callable

from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from .formatting import (
    display_cwd,
    display_model,
    format_duration,
    format_ts,
    picker_metadata_items,
    status_text,
)
from .models import SessionRecord
from .tmux import capture_tmux_pane_preview, focus_tmux_pane, tmux_target


PICKER_COLUMNS = ("Status", "Tool", "Target", "Model", "Session", "CWD")


def move_selection(current: int | None, selectable: list[int], step: int) -> int | None:
    if not selectable:
        return None
    if current is None or current not in selectable:
        return selectable[0]

    position = selectable.index(current)
    position = max(0, min(len(selectable) - 1, position + step))
    return selectable[position]


def first_focusable_index(records: list[SessionRecord]) -> int | None:
    for index, rec in enumerate(records):
        if rec.tmux_pane is not None:
            return index
    return 0 if records else None


def picker_row_cells(rec: SessionRecord) -> tuple[str, str, str, str, str, str]:
    return (
        rec.status,
        rec.tool,
        tmux_target(rec),
        display_model(rec) or "—",
        rec.session_id,
        display_cwd(rec) or "—",
    )


def rich_picker_row_cells(rec: SessionRecord) -> list[Text]:
    status, tool, target, model, session_id, cwd = picker_row_cells(rec)
    base_style = "dim" if rec.tmux_pane is None else ""
    status_cell = status_text(status)
    if base_style:
        status_cell.stylize(base_style)
    return [
        status_cell,
        Text(tool, style=base_style),
        Text(target, style=base_style),
        Text(model, style=base_style),
        Text(session_id, style=base_style),
        Text(cwd, style=base_style),
    ]


def append_detail(lines: list[str], label: str, value: str | None, width: int) -> None:
    if not value or width <= 0:
        return
    prefix = f"{label}: "
    body_width = max(8, width - len(prefix))
    wrapped = textwrap.wrap(value, body_width) or [value]
    for index, chunk in enumerate(wrapped):
        if index == 0:
            lines.append(f"{prefix}{chunk}")
        else:
            lines.append(" " * len(prefix) + chunk)


def picker_detail_items(rec: SessionRecord) -> list[tuple[str, str]]:
    items = [("Session", rec.session_id)]
    cwd = display_cwd(rec)
    if cwd:
        items.append(("CWD", cwd))

    if rec.tmux_pane is not None:
        tmux_bits = [tmux_target(rec)]
        if rec.tmux_pane.window_name:
            tmux_bits.append(rec.tmux_pane.window_name)
        if rec.tmux_pane.pane_tty:
            tmux_bits.append(rec.tmux_pane.pane_tty)
        items.append(("Tmux", " | ".join(tmux_bits)))

    if rec.matched_process is not None:
        process_bits = [f"pid {rec.matched_process.pid}"]
        if rec.matched_process.tty:
            process_bits.append(rec.matched_process.tty)
        runtime = format_duration(rec.matched_process.etime_seconds)
        if runtime != "—":
            process_bits.append(runtime)
        items.append(("Process", " | ".join(process_bits)))

    file_bits: list[str] = []
    if rec.last_write is not None:
        file_bits.append(format_ts(rec.last_write))
    if rec.path is not None:
        file_bits.append(str(rec.path))
    if file_bits:
        items.append(("File", " | ".join(file_bits)))

    items.extend(picker_metadata_items(rec))
    return items


def build_picker_details(
    rec: SessionRecord, width: int, pane_preview: list[str] | None = None
) -> list[str]:
    lines: list[str] = []
    for label, value in picker_detail_items(rec):
        append_detail(lines, label, value, width)

    if pane_preview:
        for index, line in enumerate(pane_preview):
            append_detail(lines, "Preview" if index == 0 else "", line, width)

    if not lines:
        lines.append("No additional metadata for this session.")
    return lines


def picker_details_renderable(rec: SessionRecord) -> Table:
    table = Table.grid(padding=(0, 1))
    table.expand = True
    table.add_column(style="bold cyan", no_wrap=True)
    table.add_column(ratio=1, overflow="fold")
    for label, value in picker_detail_items(rec):
        table.add_row(f"{label}:", value)
    return table


class SessionPickerApp(App[int]):
    CSS = """
    Screen {
        layout: vertical;
    }

    #body {
        height: 1fr;
        layout: horizontal;
    }

    #sessions {
        width: 3fr;
        height: 100%;
    }

    #sidebar {
        width: 2fr;
        min-width: 32;
        height: 100%;
        border-left: solid $surface;
    }

    #details {
        height: auto;
        max-height: 40%;
        padding: 0 1;
        border-bottom: solid $surface;
    }

    #preview {
        height: 1fr;
        min-width: 1;
        padding: 0 1;
    }

    #message {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }

    #body.narrow {
        layout: vertical;
    }

    #sessions.narrow {
        width: 100%;
        height: 3fr;
    }

    #sidebar.narrow {
        width: 100%;
        min-width: 1;
        height: 2fr;
        border-left: none;
        border-top: solid $surface;
    }
    """

    BINDINGS = [
        Binding("enter", "focus_selected", "Focus", priority=True),
        Binding("q", "cancel", "Quit", priority=True),
        Binding("escape", "cancel", "Quit", show=False, priority=True),
        Binding("j", "cursor_down", "Down"),
        Binding("k", "cursor_up", "Up"),
    ]

    def __init__(
        self,
        records: list[SessionRecord],
        *,
        focus_callback: Callable[[SessionRecord], bool] = focus_tmux_pane,
        preview_callback: Callable[[SessionRecord, int], list[str]]
        = capture_tmux_pane_preview,
        preview_limit: int = 120,
    ) -> None:
        super().__init__()
        self.records = records
        self.focus_callback = focus_callback
        self.preview_callback = preview_callback
        self.preview_limit = preview_limit
        self.preview_cache: dict[str, list[str]] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Container(id="body"):
            yield DataTable(
                show_row_labels=False,
                zebra_stripes=True,
                cursor_type="row",
                id="sessions",
            )
            with Vertical(id="sidebar"):
                yield Static(id="details")
                yield RichLog(
                    id="preview",
                    wrap=False,
                    highlight=False,
                    markup=False,
                    auto_scroll=False,
                )
        yield Static(id="message")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Session Picker"
        self.sub_title = (
            f"{len(self.records)} shown, {self.focusable_count} focusable"
        )
        self.apply_responsive_layout(self.size.width)
        table = self.query_one("#sessions", DataTable)
        for column in PICKER_COLUMNS:
            table.add_column(column)

        for index, rec in enumerate(self.records):
            table.add_row(*rich_picker_row_cells(rec), key=str(index))

        initial_index = first_focusable_index(self.records)
        if initial_index is None:
            self.update_message("No sessions available. Press q to exit.")
            self.update_preview(None)
            return

        table.focus()
        table.move_cursor(row=initial_index, column=0, animate=False)
        self.update_selected_record()

    def on_resize(self, _event: object) -> None:
        self.apply_responsive_layout(self.size.width)

    def apply_responsive_layout(self, width: int) -> None:
        narrow = width < 88
        for selector in ("#body", "#sessions", "#sidebar"):
            self.query_one(selector).set_class(narrow, "narrow")

    @property
    def focusable_count(self) -> int:
        return sum(1 for rec in self.records if rec.tmux_pane is not None)

    def selected_record(self) -> SessionRecord | None:
        table = self.query_one("#sessions", DataTable)
        if table.row_count == 0:
            return None
        if 0 <= table.cursor_row < len(self.records):
            return self.records[table.cursor_row]
        return None

    def on_data_table_row_highlighted(
        self, _event: DataTable.RowHighlighted
    ) -> None:
        self.update_selected_record()

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        self.action_focus_selected()

    def action_cursor_down(self) -> None:
        self.query_one("#sessions", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#sessions", DataTable).action_cursor_up()

    def action_cancel(self) -> None:
        self.exit(1)

    def action_focus_selected(self) -> None:
        rec = self.selected_record()
        if rec is None:
            self.update_message("No sessions available. Press q to exit.")
            return
        if rec.tmux_pane is None:
            self.update_message("No focusable tmux target for this session.")
            return
        if self.focus_callback(rec):
            self.exit(0)
            return
        self.update_message(f"Failed to focus {tmux_target(rec)}.")

    def update_selected_record(self) -> None:
        rec = self.selected_record()
        self.update_preview(rec)
        if rec is None:
            self.update_message("No sessions available. Press q to exit.")
            return

        self.sub_title = (
            f"{len(self.records)} shown, {self.focusable_count} focusable; "
            f"selected {rec.tool} {rec.status}"
        )
        if rec.tmux_pane is None:
            self.update_message("Selected session has no tmux target.")
        else:
            self.update_message("Enter to focus, j/k or arrows to move, q to quit.")

    def update_preview(self, rec: SessionRecord | None) -> None:
        details = self.query_one("#details", Static)
        preview = self.query_one("#preview", RichLog)
        preview.clear()

        if rec is None:
            details.update("No session selected.")
            preview.write(Text("No preview available.", style="dim"))
            return

        details.update(picker_details_renderable(rec))
        if rec.tmux_pane is None:
            preview.write(Text("No focusable tmux target for this session.", style="dim"))
            return

        pane_id = rec.tmux_pane.pane_id
        lines = self.preview_cache.get(pane_id)
        if lines is None:
            lines = self.preview_callback(rec, self.preview_limit)
            self.preview_cache[pane_id] = lines

        if not lines:
            preview.write(Text("Preview unavailable.", style="dim"))
            return

        for line in lines:
            preview.write(Text.from_ansi(line))

    def update_message(self, message: str) -> None:
        self.query_one("#message", Static).update(message)


def run_picker(records: list[SessionRecord]) -> int:
    result = SessionPickerApp(records).run()
    return result if result is not None else 1
