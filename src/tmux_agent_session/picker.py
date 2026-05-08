from __future__ import annotations

import curses
import sys
import textwrap

from .ansi import parse_ansi_segments
from .formatting import (
    display_cwd,
    display_model,
    format_duration,
    format_ts,
    pad,
    picker_metadata_items,
    truncate,
)
from .models import AnsiStyle, SessionRecord
from .tmux import capture_tmux_pane_preview, focus_tmux_pane, tmux_target


def move_selection(current: int | None, selectable: list[int], step: int) -> int | None:
    if not selectable:
        return None
    if current is None or current not in selectable:
        return selectable[0]

    position = selectable.index(current)
    position = max(0, min(len(selectable) - 1, position + step))
    return selectable[position]


def render_picker_line(rec: SessionRecord, width: int) -> str:
    model_width = 12
    target_width = 10
    tool_width = 8
    status_width = 8
    session_width = 16
    fixed = tool_width + status_width + target_width + model_width + session_width + 10
    cwd_width = max(12, width - fixed)
    return "  ".join(
        [
            pad(rec.status, status_width),
            pad(rec.tool, tool_width),
            pad(tmux_target(rec), target_width),
            pad(display_model(rec), model_width),
            pad(rec.session_id, session_width),
            truncate(display_cwd(rec), cwd_width),
        ]
    )[:width]


def render_picker_header(width: int) -> str:
    return render_picker_line(
        SessionRecord(
            tool="TOOL",
            session_id="SESSION",
            path=None,
            last_write=None,
            cwd="CWD",
            metadata={"model": "MODEL"},
            status="STATUS",
        ),
        width,
    )


def picker_split_widths(width: int) -> tuple[int, int]:
    min_list_width = 44
    min_sidebar_width = 32
    divider_width = 2
    if width < min_list_width + min_sidebar_width + divider_width + 1:
        return width, 0

    available_width = width - divider_width
    list_width = available_width // 2
    sidebar_width = available_width - list_width
    if list_width < min_list_width or sidebar_width < min_sidebar_width:
        return width, 0
    return list_width, sidebar_width


def curses_color_number(color: int | None) -> int:
    if color is None:
        return -1
    return {
        0: curses.COLOR_BLACK,
        1: curses.COLOR_RED,
        2: curses.COLOR_GREEN,
        3: curses.COLOR_YELLOW,
        4: curses.COLOR_BLUE,
        5: curses.COLOR_MAGENTA,
        6: curses.COLOR_CYAN,
        7: curses.COLOR_WHITE,
    }.get(color, -1)


def safe_addnstr(
    stdscr: curses.window, y: int, x: int, text: str, width: int, attr: int = 0
) -> None:
    if width <= 0:
        return
    try:
        stdscr.addnstr(y, x, text, width, attr)
    except curses.error:
        pass


def ansi_style_attr(
    style: AnsiStyle,
    color_pairs: dict[tuple[int | None, int | None], int],
    next_pair: list[int],
) -> int:
    attr = curses.A_NORMAL
    if style.bold:
        attr |= curses.A_BOLD
    if style.dim:
        attr |= curses.A_DIM
    if style.reverse:
        attr |= curses.A_REVERSE

    color_key = (style.fg, style.bg)
    if style.fg is None and style.bg is None:
        return attr

    pair_number = color_pairs.get(color_key)
    if pair_number is None:
        try:
            pair_number = next_pair[0]
            curses.init_pair(
                pair_number,
                curses_color_number(style.fg),
                curses_color_number(style.bg),
            )
            color_pairs[color_key] = pair_number
            next_pair[0] += 1
        except curses.error:
            return attr
    return attr | curses.color_pair(pair_number)


def render_ansi_line(
    stdscr: curses.window,
    y: int,
    x: int,
    text: str,
    width: int,
    color_pairs: dict[tuple[int | None, int | None], int],
    next_pair: list[int],
) -> None:
    if width <= 0:
        return

    cursor = x
    remaining = width
    for chunk, style in parse_ansi_segments(text):
        if remaining <= 0:
            break
        if not chunk:
            continue
        rendered = chunk[:remaining]
        safe_addnstr(
            stdscr,
            y,
            cursor,
            rendered,
            len(rendered),
            ansi_style_attr(style, color_pairs, next_pair),
        )
        cursor += len(rendered)
        remaining -= len(rendered)
    if remaining > 0:
        safe_addnstr(stdscr, y, cursor, " " * remaining, remaining)


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


def build_picker_details(
    rec: SessionRecord, width: int, pane_preview: list[str] | None = None
) -> list[str]:
    lines: list[str] = []
    append_detail(lines, "Session", rec.session_id, width)

    if pane_preview:
        for index, line in enumerate(pane_preview):
            append_detail(lines, "Preview" if index == 0 else "", line, width)

    append_detail(lines, "CWD", display_cwd(rec), width)

    if rec.tmux_pane is not None:
        tmux_bits = [tmux_target(rec)]
        if rec.tmux_pane.window_name:
            tmux_bits.append(rec.tmux_pane.window_name)
        if rec.tmux_pane.pane_tty:
            tmux_bits.append(rec.tmux_pane.pane_tty)
        append_detail(lines, "Tmux", " | ".join(tmux_bits), width)

    if rec.matched_process is not None:
        process_bits = [f"pid {rec.matched_process.pid}"]
        if rec.matched_process.tty:
            process_bits.append(rec.matched_process.tty)
        runtime = format_duration(rec.matched_process.etime_seconds)
        if runtime != "—":
            process_bits.append(runtime)
        append_detail(lines, "Process", " | ".join(process_bits), width)

    file_bits: list[str] = []
    if rec.last_write is not None:
        file_bits.append(format_ts(rec.last_write))
    if rec.path is not None:
        file_bits.append(str(rec.path))
    append_detail(lines, "File", " | ".join(file_bits), width)

    for label, value in picker_metadata_items(rec):
        append_detail(lines, label, value, width)

    if not lines:
        lines.append("No additional metadata for this session.")
    return lines


def run_picker(records: list[SessionRecord]) -> int:
    selectable = [i for i, rec in enumerate(records) if rec.tmux_pane is not None]

    def inner(stdscr: curses.window) -> int:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.keypad(True)
        try:
            curses.use_default_colors()
        except curses.error:
            pass

        status_attrs = {
            "active": curses.A_NORMAL,
            "recent": curses.A_NORMAL,
            "stale": curses.A_DIM,
        }
        if curses.has_colors():
            try:
                curses.start_color()
                curses.init_pair(1, curses.COLOR_GREEN, -1)
                curses.init_pair(2, curses.COLOR_YELLOW, -1)
                curses.init_pair(3, curses.COLOR_CYAN, -1)
                status_attrs = {
                    "active": curses.color_pair(1),
                    "recent": curses.color_pair(2),
                    "stale": curses.color_pair(3) | curses.A_DIM,
                }
            except curses.error:
                pass

        highlight_attr = curses.A_REVERSE
        dim_attr = curses.A_DIM
        title_attr = curses.A_BOLD
        header_attr = curses.A_BOLD
        color_pairs: dict[tuple[int | None, int | None], int] = {}
        next_color_pair = [10]
        selected = selectable[0] if selectable else None
        top = 0
        message = "Enter focus  j/k move  q/Esc cancel"
        preview_cache: dict[str, list[str]] = {}
        selected_preview: list[str] = []
        selected_preview_id: str | None = None

        while True:
            height, width = stdscr.getmaxyx()
            footer_y = max(0, height - 1)
            row_start = 2
            list_width, sidebar_width = picker_split_widths(width)
            divider_x = list_width if sidebar_width else None
            sidebar_x = list_width + 2 if sidebar_width else 0
            list_height = max(1, footer_y - row_start)
            if selected is not None:
                if selected < top:
                    top = selected
                elif selected >= top + list_height:
                    top = selected - list_height + 1
            else:
                top = 0

            stdscr.erase()
            selected_rec = records[selected] if selected is not None else None
            current_preview_id = (
                selected_rec.tmux_pane.pane_id
                if selected_rec is not None and selected_rec.tmux_pane is not None
                else None
            )
            if current_preview_id != selected_preview_id:
                selected_preview_id = current_preview_id
                if current_preview_id is None:
                    selected_preview = []
                else:
                    selected_preview = preview_cache.get(current_preview_id)
                    if selected_preview is None:
                        preview_limit = max(6, footer_y - row_start - 1)
                        selected_preview = capture_tmux_pane_preview(
                            selected_rec, limit=preview_limit
                        )
                        preview_cache[current_preview_id] = selected_preview
            focusable_count = len(selectable)
            title = f"Session Picker  {len(records)} shown  {focusable_count} focusable"
            if selected_rec is not None:
                title = f"{title}  Selected: {selected_rec.tool} {selected_rec.status}"
            safe_addnstr(
                stdscr, 0, 0, truncate(title, width).ljust(width), width, title_attr
            )
            safe_addnstr(
                stdscr,
                1,
                0,
                render_picker_header(list_width).ljust(list_width),
                list_width,
                header_attr,
            )
            if sidebar_width:
                safe_addnstr(
                    stdscr,
                    1,
                    sidebar_x,
                    truncate("Selected Pane", sidebar_width).ljust(sidebar_width),
                    sidebar_width,
                    header_attr,
                )
            for row, record_index in enumerate(
                range(top, min(len(records), top + list_height)), start=1
            ):
                rec = records[record_index]
                row_y = row_start + row - 1
                attr = status_attrs.get(rec.status, curses.A_NORMAL)
                if rec.tmux_pane is None:
                    attr |= dim_attr
                elif record_index == selected:
                    attr |= highlight_attr
                safe_addnstr(
                    stdscr,
                    row_y,
                    0,
                    render_picker_line(rec, list_width),
                    list_width,
                    attr,
                )

            if divider_x is not None:
                for y in range(1, footer_y):
                    safe_addnstr(stdscr, y, divider_x, "|", 1, dim_attr)
                if selected_rec is None:
                    safe_addnstr(
                        stdscr,
                        row_start,
                        sidebar_x,
                        "No focusable tmux target for the visible sessions.".ljust(
                            sidebar_width
                        ),
                        sidebar_width,
                    )
                elif not selected_preview:
                    safe_addnstr(
                        stdscr,
                        row_start,
                        sidebar_x,
                        "Preview unavailable.".ljust(sidebar_width),
                        sidebar_width,
                        dim_attr,
                    )
                else:
                    for index, line in enumerate(
                        selected_preview[: footer_y - row_start]
                    ):
                        render_ansi_line(
                            stdscr,
                            row_start + index,
                            sidebar_x,
                            line,
                            sidebar_width,
                            color_pairs,
                            next_color_pair,
                        )

            if not selectable:
                message = "No tmux-mapped sessions available. Press q to exit."
            footer = message
            safe_addnstr(
                stdscr,
                max(0, height - 1),
                0,
                truncate(footer, width).ljust(width),
                width,
                dim_attr,
            )
            stdscr.refresh()

            key = stdscr.getch()
            if key in (ord("q"), 27):
                return 1
            if key in (curses.KEY_UP, ord("k")):
                selected = move_selection(selected, selectable, -1)
                continue
            if key in (curses.KEY_DOWN, ord("j")):
                selected = move_selection(selected, selectable, 1)
                continue
            if key in (10, 13, curses.KEY_ENTER):
                if selected is None:
                    message = "No focusable tmux target for the visible sessions."
                    continue
                if focus_tmux_pane(records[selected]):
                    return 0
                message = f"Failed to focus {tmux_target(records[selected])}."

    try:
        return curses.wrapper(inner)
    except curses.error as exc:
        print(f"picker failed: {exc}", file=sys.stderr)
        return 1
