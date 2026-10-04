from __future__ import annotations

import asyncio
import textwrap
from collections.abc import Callable

from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.events import Resize
from textual.containers import Container, VerticalScroll
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from .formatting import (
    display_cwd,
    display_model,
    first_metadata_value,
    format_duration,
    format_ts,
    group_records_by_directory,
    picker_metadata_items,
    status_text,
)
from .models import SessionRecord
from .session_details import load_session_details
from .tmux import focus_tmux_pane, tmux_target


PICKER_COLUMNS = ("Directory", "Status", "Tool", "Target", "Model")


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


def picker_row_cells(rec: SessionRecord) -> tuple[str, str, str, str, str]:
    return (
        rec.status,
        rec.tool,
        tmux_target(rec),
        display_model(rec) or "—",
        display_cwd(rec) or "—",
    )


def rich_picker_row_cells(rec: SessionRecord) -> list[Text]:
    status, tool, target, model, cwd = picker_row_cells(rec)
    base_style = "dim" if rec.tmux_pane is None else ""
    status_cell = status_text(status)
    if base_style:
        status_cell.stylize(base_style)
    return [
        status_cell,
        Text(tool, style=base_style),
        Text(target, style=base_style),
        Text(model, style=base_style),
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
    role = first_metadata_value(rec, ("last_message_role",))
    last_message_label = f"Last message ({role})" if role else "Last message"
    branch_label = "Git branch (recorded)" if rec.metadata.get("recorded_branch") and not rec.metadata.get("git_branch") else "Git branch"
    items = [
        ("Title", first_metadata_value(rec, ("title",)) or "Unavailable"),
        ("Project", first_metadata_value(rec, ("project",)) or display_cwd(rec) or "Unavailable"),
        (branch_label, first_metadata_value(rec, ("git_branch", "recorded_branch")) or "Unavailable"),
        ("Last user prompt", first_metadata_value(rec, ("last_user_prompt",)) or "Unavailable"),
        (last_message_label, first_metadata_value(rec, ("last_message",)) or "Unavailable"),
        ("Session", rec.session_id),
    ]
    if rec.requires_user_feedback:
        items.append(("Feedback", "requires user feedback"))
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
    rec: SessionRecord, width: int
) -> list[str]:
    lines: list[str] = []
    for label, value in picker_detail_items(rec):
        append_detail(lines, label, value, width)

    if not lines:
        lines.append("No additional metadata for this session.")
    return lines


def session_option(rec: SessionRecord, selected: bool = False) -> Table:
    title = first_metadata_value(rec, ("title", "summary", "last_user_prompt"))
    title = " ".join((title or f"{rec.tool} session").split())
    row = Table.grid(expand=True, padding=(0, 1))
    row.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
    row.add_column(justify="right", no_wrap=True)
    name = Text("› " if selected else "  ", style="bold #65d9ef" if selected else "")
    marker = "●" if rec.status in {"active", "waiting"} else "○"
    name.append(f"{marker} ", style=status_text(rec.status).style)
    name.append(title, style="bold" if selected else "")
    row.add_row(name, status_text(rec.status))
    metadata = " · ".join(filter(None, (rec.tool, tmux_target(rec), display_model(rec))))
    row.add_row(Text(f"    {metadata}", style="#8fb3ca", no_wrap=True, overflow="ellipsis"), Text(""))
    return row


def picker_details_renderable(rec: SessionRecord) -> Table:
    table = Table.grid(padding=(0, 0))
    table.expand = True
    table.add_column(ratio=1, overflow="fold")
    items = picker_detail_items(rec)
    title = first_metadata_value(rec, ("title", "summary")) or f"{rec.tool} session"
    table.add_row(Text(title, style="bold"))
    table.add_row(status_text(rec.status))
    items.sort(key=lambda item: 0 if item[0] == "Model" else 1)
    for label, value in items:
        if label == "Title":
            continue
        table.add_row(Text(""))
        table.add_row(Text(label, style="#65d9ef"))
        table.add_row(Text(value))
    return table


class SessionPickerApp(App[int]):
    CSS = """
    Screen { layout: vertical; background: ansi_default; color: ansi_default; padding: 1 2; }
    #heading { height: 1; text-style: bold; color: #65d9ef; }
    #summary { height: 2; color: #8fb3ca; }
    #body { height: 1fr; layout: horizontal; border-top: solid #24475c; }
    #session-list { width: 2fr; height: 100%; padding: 1 1 0 0; }
    #list-heading { height: 2; color: #8fb3ca; padding-left: 4; }
    #sessions { width: 100%; height: 1fr; border: none; padding: 0; background: transparent; }
    #sessions:focus { border: none; background-tint: transparent; }
    #sessions > .option-list--option { padding: 0; }
    #sessions > .option-list--option-disabled { color: #65d9ef; text-style: none; }
    #sessions > .option-list--option-highlighted { background: #102d42; color: #eef8ff; text-style: none; }
    #sessions > .option-list--option-hover { background: #0b2233; }
    #sidebar { width: 3fr; min-width: 32; height: 100%; border-left: solid #24475c; padding: 1 2; }
    #detail-heading { height: 2; color: #8fb3ca; }
    #details { height: auto; }
    #sessions, #sidebar {
        scrollbar-size: 1 1;
        scrollbar-background: ansi_default;
        scrollbar-background-hover: ansi_default;
        scrollbar-background-active: ansi_default;
        scrollbar-color: #24475c;
        scrollbar-color-hover: #65d9ef;
        scrollbar-color-active: #65d9ef;
        scrollbar-corner-color: ansi_default;
    }
    #message { height: 1; margin-top: 1; color: #8fb3ca; }
    #keys { height: 1; color: #8fb3ca; }
    #body.narrow { layout: vertical; }
    #session-list.narrow { width: 100%; height: 3fr; }
    #sidebar.narrow { width: 100%; min-width: 1; height: 2fr; border-left: none; border-top: solid #24475c; }
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
        details_callback: Callable[[SessionRecord], dict[str, str]] = load_session_details,
    ) -> None:
        super().__init__(ansi_color=True)
        self.directory_groups = group_records_by_directory(records)
        self.records = [rec for _, group in self.directory_groups for rec in group]
        self.focus_callback = focus_callback
        self.details_callback = details_callback
        self.selected_option_id: str | None = None
        self.details_cache: dict[int, dict[str, str]] = {}
        self.details_pending: set[int] = set()

    def compose(self) -> ComposeResult:
        yield Static("Agent sessions  ·  Group: Directory", id="heading")
        counts = {status: sum(rec.status == status for rec in self.records)
                  for status in ("waiting", "active", "recent", "stale")}
        summary = Text(f"All {len(self.records)}", style="bold")
        for label, status in (("Needs you", "waiting"), ("Working", "active"),
                              ("Recent", "recent"), ("Inactive", "stale")):
            summary.append("   ")
            summary.append(f"{label} {counts[status]}", style=status_text(status).style)
        yield Static(summary, id="summary")
        with Container(id="body"):
            with Container(id="session-list"):
                yield Static("Sessions", id="list-heading")
                yield OptionList(id="sessions")
            with VerticalScroll(id="sidebar"):
                yield Static("Session details", id="detail-heading")
                yield Static(id="details", markup=False)
        yield Static(id="message")
        yield Static(Text.assemble(
            ("↑/↓", "#65d9ef"), " move   ", ("j/k", "#65d9ef"), " move   ",
            ("enter", "#65d9ef"), " focus   ", ("q / esc", "#65d9ef"), " quit",
        ), id="keys")

    def on_mount(self) -> None:
        self.title = "Agent sessions"
        self.apply_responsive_layout(self.size.width)
        sessions = self.query_one("#sessions", OptionList)
        index = 0
        for group_index, (directory, group) in enumerate(self.directory_groups):
            heading = Text(("\n" if group_index else "") + directory, style="bold #65d9ef")
            heading.append(f"  {len(group)}", style="#8fb3ca")
            sessions.add_option(Option(heading, id=f"directory-{group_index}", disabled=True))
            for rec in group:
                sessions.add_option(Option(session_option(rec), id=str(index)))
                index += 1

        initial_index = first_focusable_index(self.records)
        if initial_index is None:
            self.update_message("No sessions available. Press q to exit.")
            self.update_details(None)
            return
        sessions.highlighted = sessions.get_option_index(str(initial_index))
        sessions.focus()
        self.update_selected_record()

    def on_resize(self, event: Resize) -> None:
        self.apply_responsive_layout(event.size.width)

    def apply_responsive_layout(self, width: int) -> None:
        narrow = width < 88
        for selector in ("#body", "#session-list", "#sidebar"):
            self.query_one(selector).set_class(narrow, "narrow")

    @property
    def focusable_count(self) -> int:
        return sum(1 for rec in self.records if rec.tmux_pane is not None)

    def selected_record(self) -> SessionRecord | None:
        option = self.query_one("#sessions", OptionList).highlighted_option
        if option is None or option.disabled or option.id is None:
            return None
        return self.records[int(option.id)]

    def on_option_list_option_highlighted(self, _event: OptionList.OptionHighlighted) -> None:
        self.update_selected_record()

    def on_option_list_option_selected(self, _event: OptionList.OptionSelected) -> None:
        self.action_focus_selected()

    def action_cursor_down(self) -> None:
        self.query_one("#sessions", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#sessions", OptionList).action_cursor_up()

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
        sessions = self.query_one("#sessions", OptionList)
        if self.selected_option_id is not None:
            index = int(self.selected_option_id)
            sessions.replace_option_prompt(self.selected_option_id, session_option(self.records[index]))
        option = sessions.highlighted_option
        self.selected_option_id = option.id if option is not None and not option.disabled else None
        if self.selected_option_id is not None:
            index = int(self.selected_option_id)
            sessions.replace_option_prompt(self.selected_option_id, session_option(self.records[index], True))
        rec = self.selected_record()
        self.update_details(rec)
        if rec is None:
            self.update_message("No sessions available. Press q to exit.")
            return

        self.sub_title = (
            f"{len(self.records)} shown, {self.focusable_count} focusable; "
            f"selected {rec.tool} {rec.status}"
        )
        if rec.tmux_pane is None:
            self.update_message("Selected session has no tmux target.")
        elif rec.requires_user_feedback:
            self.update_message(
                "Feedback required. Enter to focus."
            )
        else:
            self.update_message(f"{rec.tool} · {tmux_target(rec)}")

    def update_details(self, rec: SessionRecord | None) -> None:
        details = self.query_one("#details", Static)
        if rec is None:
            details.update("No session selected.")
            return
        details.update(picker_details_renderable(rec))
        key = id(rec)
        if key not in self.details_cache and key not in self.details_pending:
            self.details_pending.add(key)
            self.run_worker(self.fetch_details(rec), group="session-details")

    async def fetch_details(self, rec: SessionRecord) -> None:
        key = id(rec)
        try:
            metadata = await asyncio.to_thread(self.details_callback, rec)
        except Exception:
            # Unreadable or changing storage must not close the picker.
            metadata = {}
        finally:
            self.details_pending.discard(key)
        self.details_cache[key] = metadata
        rec.metadata.update(metadata)
        index = next(index for index, record in enumerate(self.records) if record is rec)
        self.query_one("#sessions", OptionList).replace_option_prompt(
            str(index), session_option(rec, self.selected_record() is rec)
        )
        if self.selected_record() is rec:
            self.query_one("#details", Static).update(picker_details_renderable(rec))

    def update_message(self, message: str) -> None:
        self.query_one("#message", Static).update(message)


def run_picker(records: list[SessionRecord]) -> int:
    result = SessionPickerApp(records).run()
    return result if result is not None else 1
