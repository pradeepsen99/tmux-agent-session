from __future__ import annotations

import asyncio
import textwrap
from functools import partial
from collections.abc import Callable
from pathlib import Path

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
    PALETTE,
    STATUS_STYLES,
    format_age,
    format_duration,
    format_ts,
    group_records_by_directory,
    picker_metadata_items,
    short_model,
    status_text,
)
from .models import SessionRecord
from .session_cache import fingerprint
from .session_details import load_session_details
from .tmux import focus_tmux_pane, tmux_target


PICKER_COLUMNS = ("Directory", "Status", "Tool", "Target", "Model")
AGE_REFRESH_SECONDS = 30
ACCENT = PALETTE["accent"]
MUTED = PALETTE["muted"]


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


def display_path(path: Path) -> str:
    home = Path.home()
    try:
        return str(Path("~") / path.relative_to(home))
    except ValueError:
        return str(path)


def picker_detail_items(rec: SessionRecord) -> list[tuple[str, str]]:
    branch = first_metadata_value(rec, ("git_branch",))
    if branch is None:
        recorded = first_metadata_value(rec, ("recorded_branch",))
        branch = f"{recorded} (recorded)" if recorded else None
    project = first_metadata_value(rec, ("project",))
    cwd = display_cwd(rec)
    candidates = [
        ("Title", first_metadata_value(rec, ("title",))),
        ("Original prompt", first_metadata_value(rec, ("original_prompt",))),
        ("Last agent message", first_metadata_value(rec, ("last_agent_message",))),
        ("Git branch", branch),
        ("Project", project),
        ("CWD", cwd if cwd != project else None),
        ("Session", rec.session_id),
    ]
    items = [(label, value) for label, value in candidates if value]
    if rec.requires_user_feedback:
        items.append(("Feedback", "requires user feedback"))

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
        file_bits.append(display_path(rec.path))
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


def session_option(rec: SessionRecord, selected: bool = False, now: float | None = None) -> Table:
    title = first_metadata_value(rec, ("title", "summary", "original_prompt"))
    title = " ".join((title or f"{rec.tool} session").split())
    row = Table.grid(expand=True, padding=(0, 1))
    row.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
    row.add_column(justify="right", no_wrap=True)
    name = Text("› " if selected else "  ", style=f"bold {ACCENT}" if selected else "")
    marker = "●" if rec.status in {"active", "waiting"} else "○"
    name.append(f"{marker} ", style=status_text(rec.status).style)
    name.append(title, style=f"bold {ACCENT}" if selected else "")
    row.add_row(name, status_text(rec.status))
    metadata = " · ".join(filter(None, (rec.tool, tmux_target(rec), short_model(display_model(rec)))))
    row.add_row(
        Text(f"    {metadata}", style=MUTED, no_wrap=True, overflow="ellipsis"),
        Text(format_age(rec.last_write, now) or "", style=MUTED),
    )
    return row


DETAIL_LABEL_STYLE = ACCENT
CONVERSATION_LABELS = ("Original prompt", "Last agent message")
# Facts render as a compact label/value grid; anything unlisted sits between
# Process and Session, keeping identifiers and file paths at the bottom.
FACT_ORDER = {"Git branch": 0, "Model": 1, "Project": 2, "CWD": 3, "Tmux": 4, "Process": 5, "Session": 8, "File": 9}


def picker_details_renderable(rec: SessionRecord, now: float | None = None) -> Table:
    table = Table.grid(padding=(0, 0))
    table.expand = True
    table.add_column(ratio=1, overflow="fold")
    title = first_metadata_value(rec, ("title", "summary")) or f"{rec.tool} session"
    table.add_row(Text(title, style="bold"))

    status = status_text(rec.status)
    age = format_age(rec.last_write, now)
    if age:
        status.append(f"  ·  {age}", style=MUTED)
    if rec.requires_user_feedback:
        status.append("  ·  needs your input", style=STATUS_STYLES["waiting"])
    table.add_row(status)

    items = picker_detail_items(rec)
    for label, value in items:
        if label.startswith(CONVERSATION_LABELS):
            table.add_row(Text(""))
            table.add_row(Text(label, style=DETAIL_LABEL_STYLE))
            table.add_row(Text(value))

    facts = [
        (label, value)
        for label, value in items
        if not label.startswith(CONVERSATION_LABELS)
        and label not in {"Title", "Feedback"}
        and not (label == "Summary" and value == title)
    ]
    if facts:
        facts.sort(key=lambda item: FACT_ORDER.get(item[0], 6))
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style=DETAIL_LABEL_STYLE, no_wrap=True)
        grid.add_column(ratio=1, overflow="fold")
        for label, value in facts:
            grid.add_row(Text(label), Text(value))
        table.add_row(Text(""))
        table.add_row(grid)
    return table


def session_identity(rec: SessionRecord) -> tuple:
    return rec.tool, rec.session_id, str(rec.path)


def detail_key(rec: SessionRecord) -> tuple:
    return session_identity(rec) + (rec.last_write, fingerprint(rec.path) if rec.path else None)


def summary_text(records: list[SessionRecord]) -> Text:
    summary = Text(f"All {len(records)}", style="bold")
    for label, status in (("Needs you", "waiting"), ("Working", "active"),
                          ("Recent", "recent"), ("Inactive", "stale")):
        count = sum(rec.status == status for rec in records)
        summary.append("   ")
        summary.append(f"{label} {count}", style=STATUS_STYLES[status])
    return summary


def palette_css_variables() -> str:
    """Expose PALETTE to Textual CSS, whose ANSI color names carry an `ansi_` prefix."""
    return "".join(f"${'picker_' + name}: ansi_{color};\n" for name, color in PALETTE.items())


class SessionPickerApp(App[int]):
    CSS = palette_css_variables() + """
    Screen { layout: vertical; background: ansi_default; color: ansi_default; padding: 1 2; }
    #heading { height: 1; text-style: bold; color: $picker_accent; }
    #summary { height: 2; color: $picker_muted; }
    #body { height: 1fr; layout: horizontal; border-top: solid $picker_border; }
    #session-list { width: 3fr; height: 100%; padding: 1 1 0 0; }
    #list-heading { height: 2; color: $picker_muted; padding-left: 4; }
    #sessions { width: 100%; height: 1fr; border: none; padding: 0; background: transparent; color: ansi_default; }
    #sessions:focus { border: none; background-tint: transparent; }
    #sessions > .option-list--option { padding: 0; }
    #sessions > .option-list--option-disabled { color: $picker_accent; text-style: none; }
    #sessions > .option-list--option-highlighted { background: transparent; color: ansi_default; text-style: none; }
    #sessions > .option-list--option-hover { background: transparent; }
    #sidebar { width: 2fr; min-width: 36; height: 100%; border-left: solid $picker_border; padding: 1 2; }
    #detail-heading { height: 2; color: $picker_muted; }
    #details { height: auto; }
    #sessions, #sidebar {
        scrollbar-size: 1 1;
        scrollbar-background: ansi_default;
        scrollbar-background-hover: ansi_default;
        scrollbar-background-active: ansi_default;
        scrollbar-color: $picker_border;
        scrollbar-color-hover: $picker_accent;
        scrollbar-color-active: $picker_accent;
        scrollbar-corner-color: ansi_default;
    }
    #message { height: 1; margin-top: 1; color: $picker_muted; }
    #keys { height: 1; color: $picker_muted; }
    #body.narrow { layout: vertical; }
    #session-list.narrow { width: 100%; height: 3fr; }
    #sidebar.narrow { width: 100%; min-width: 1; height: 2fr; border-left: none; border-top: solid $picker_border; }
    """

    BINDINGS = [
        Binding("enter", "focus_selected", "Focus", priority=True),
        Binding("q", "cancel", "Quit", priority=True),
        Binding("escape", "cancel", "Quit", show=False, priority=True),
        Binding("r", "refresh_records", "Refresh"),
        Binding("j", "cursor_down", "Down"),
        Binding("k", "cursor_up", "Up"),
    ]

    def __init__(
        self,
        records: list[SessionRecord],
        *,
        focus_callback: Callable[[SessionRecord], bool] = focus_tmux_pane,
        records_callback: Callable[[], list[SessionRecord]] | None = None,
        details_callback: Callable[[SessionRecord], dict[str, str]] = load_session_details,
    ) -> None:
        super().__init__(ansi_color=True)
        self.directory_groups = group_records_by_directory(records)
        self.records = [rec for _, group in self.directory_groups for rec in group]
        self.focus_callback = focus_callback
        self.details_callback = details_callback
        self.records_callback = records_callback
        self.load_generation = 0
        self.loading = False
        self.snapshot_valid = True
        self.selected_option_id: str | None = None
        self.details_cache: dict[tuple, dict[str, str]] = {}
        self.details_pending: set[tuple] = set()

    def compose(self) -> ComposeResult:
        yield Static("Agent sessions  ·  Group: Directory", id="heading")
        yield Static(summary_text(self.records), id="summary")
        with Container(id="body"):
            with Container(id="session-list"):
                yield Static("Sessions", id="list-heading")
                yield OptionList(id="sessions")
            with VerticalScroll(id="sidebar"):
                yield Static("Session details", id="detail-heading")
                yield Static(id="details", markup=False)
        yield Static(id="message")
        yield Static(Text.assemble(
            ("↑/↓", ACCENT), " move   ", ("j/k", ACCENT), " move   ",
            ("enter", ACCENT), " focus   ", ("r", ACCENT), " refresh   ", ("q / esc", ACCENT), " quit",
        ), id="keys")

    def on_mount(self) -> None:
        self.title = "Agent sessions"
        self.apply_responsive_layout(self.size.width)
        self.replace_records(self.records)
        self.set_interval(AGE_REFRESH_SECONDS, self.refresh_ages)
        if self.records_callback is not None:
            self.action_refresh_records()

    def refresh_ages(self) -> None:
        """Re-render rows so relative ages keep advancing between data refreshes."""
        sessions = self.query_one("#sessions", OptionList)
        for index, rec in enumerate(self.records):
            option_id = str(index)
            sessions.replace_option_prompt(option_id, session_option(rec, option_id == self.selected_option_id))
        rec = self.selected_record()
        if rec is not None:
            self.query_one("#details", Static).update(picker_details_renderable(rec))

    def replace_records(self, records: list[SessionRecord]) -> None:
        previous = self.selected_record()
        identity = session_identity(previous) if previous else None
        sessions = self.query_one("#sessions", OptionList)
        self.selected_option_id = None
        sessions.clear_options()
        self.directory_groups = group_records_by_directory(records)
        self.records = [rec for _, group in self.directory_groups for rec in group]
        self.query_one("#summary", Static).update(summary_text(self.records))
        index = 0
        for group_index, (directory, group) in enumerate(self.directory_groups):
            # Color comes from CSS: Textual reads a plain Text prompt's "cyan" as RGB #00ffff.
            heading = Text(("\n" if group_index else "") + directory, style="bold")
            heading.append(f"  {len(group)}", style=MUTED)
            sessions.add_option(Option(heading, id=f"directory-{group_index}", disabled=True))
            for rec in group:
                sessions.add_option(Option(session_option(rec), id=str(index)))
                index += 1

        initial_index = next((i for i, rec in enumerate(self.records) if session_identity(rec) == identity), first_focusable_index(self.records))
        if initial_index is None:
            self.update_message("No sessions available. Press q to exit.")
            self.update_details(None)
            return
        sessions.highlighted = sessions.get_option_index(str(initial_index))
        sessions.focus()
        self.update_selected_record()

    def action_refresh_records(self) -> None:
        if self.records_callback is None:
            return
        self.load_generation += 1
        self.loading = True
        self.snapshot_valid = False
        self.update_message("Refreshing sessions…" if self.records else "Loading sessions…")
        self.run_worker(partial(self.fetch_records, self.load_generation), group="session-loading", exclusive=True)

    async def fetch_records(self, generation: int) -> None:
        try:
            records = await asyncio.to_thread(self.records_callback)
        except Exception:
            if generation == self.load_generation:
                self.loading = False
                self.update_message("Could not refresh sessions. Press r to retry.")
            return
        if generation != self.load_generation:
            return
        self.loading = False
        self.snapshot_valid = True
        self.replace_records(records)

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
        index = int(option.id)
        return self.records[index] if index < len(self.records) else None

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
        if not self.snapshot_valid:
            self.update_message("Refreshing sessions…" if self.loading else "Press r to refresh before focusing.")
            return
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
        if self.loading:
            self.update_message("Refreshing sessions…" if self.records else "Loading sessions…")
            return
        if not self.snapshot_valid:
            self.update_message("Could not refresh sessions. Press r to retry.")
            return
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
        key = detail_key(rec)
        rec.metadata.update(self.details_cache.get(key, {}))
        details.update(picker_details_renderable(rec))
        pending = (self.load_generation, key)
        if key not in self.details_cache and pending not in self.details_pending:
            self.details_pending.add(pending)
            self.run_worker(partial(self.fetch_details, rec, key, self.load_generation), group="session-details")

    async def fetch_details(self, rec: SessionRecord, key: tuple, generation: int) -> None:
        try:
            metadata = await asyncio.to_thread(self.details_callback, rec)
        except Exception:
            # Unreadable or changing storage must not close the picker.
            metadata = {}
        finally:
            self.details_pending.discard((generation, key))
        if generation != self.load_generation or detail_key(rec) != key:
            return
        self.details_cache[key] = metadata
        rec.metadata.update(metadata)
        index = next((index for index, record in enumerate(self.records) if record is rec), None)
        if index is None:
            return
        self.query_one("#sessions", OptionList).replace_option_prompt(
            str(index), session_option(rec, self.selected_record() is rec)
        )
        if self.selected_record() is rec:
            self.query_one("#details", Static).update(picker_details_renderable(rec))

    def update_message(self, message: str) -> None:
        self.query_one("#message", Static).update(message)


def run_picker(records: list[SessionRecord], *, records_callback: Callable[[], list[SessionRecord]] | None = None) -> int:
    result = SessionPickerApp(records, records_callback=records_callback).run()
    return result if result is not None else 1
