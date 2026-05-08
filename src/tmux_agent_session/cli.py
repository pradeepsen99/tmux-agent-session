#!/usr/bin/env python3
"""CLI entry point for inspecting Codex and OpenCode tmux sessions."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from . import __version__
from .ansi import (
    ANSI_SGR_RE,
    ansi_color_from_256,
    ansi_color_from_rgb,
    apply_ansi_sgr,
    parse_ansi_segments,
)
from .commands import run_command
from .formatting import (
    PICKER_METADATA_PRIMARY,
    PICKER_METADATA_SECONDARY,
    display_cwd,
    display_model,
    first_metadata_value,
    format_duration,
    format_iso_ts,
    format_ts,
    joined_metadata_value,
    metadata_text,
    pad,
    picker_metadata_items,
    print_json,
    print_table,
    truncate,
)
from .harnesses import load_sessions as _load_harness_sessions
from .harnesses.codex import DEFAULT_CODEX_DIR, extract_codex_session
from .harnesses.opencode import DEFAULT_OPENCODE_DIRS, extract_opencode_sessions
from .models import AnsiStyle, ProcessInfo, SessionRecord, TmuxPane
from .picker import (
    ansi_style_attr,
    append_detail,
    build_picker_details,
    curses_color_number,
    move_selection,
    picker_split_widths,
    render_ansi_line,
    render_picker_header,
    render_picker_line,
    run_picker,
    safe_addnstr,
)
from .processes import (
    SESSION_ID_PATTERNS,
    detect_processes,
    extract_session_ids,
    get_cwd,
    normalize_tty,
    parse_etime_to_seconds,
)
from .scoring import (
    add_process_only_records,
    age_minutes,
    score_session,
    sort_records,
)
from .session_files import (
    extract_session_from_json,
    extract_session_records,
    fallback_file_record,
    find_session_files,
    normalize_cwd,
    read_json_file,
    read_jsonl_file,
    safe_mtime,
)
from .tmux import (
    attach_tmux_panes,
    capture_tmux_pane_preview,
    detect_tmux_panes,
    focus_tmux_pane,
    tmux_target,
)


def load_sessions(tool: str, base_paths: list[Path]) -> list[SessionRecord]:
    return _load_harness_sessions(tool, base_paths)


def build_records(args: argparse.Namespace) -> list[SessionRecord]:
    opencode_dirs = args.opencode_dir or DEFAULT_OPENCODE_DIRS

    processes = detect_processes()
    if args.tool != "all":
        processes = [proc for proc in processes if proc.tool == args.tool]
    panes = detect_tmux_panes()
    records: list[SessionRecord] = []

    if args.tool in ("all", "codex"):
        records.extend(load_sessions("codex", [args.codex_dir]))
    if args.tool in ("all", "opencode"):
        records.extend(load_sessions("opencode", opencode_dirs))

    for rec in records:
        score_session(rec, processes, args.active_minutes, args.recent_hours)

    records = add_process_only_records(records, processes)
    attach_tmux_panes(records, panes)
    records = sort_records(records)

    if not args.include_stale:
        records = [r for r in records if r.status != "stale"]

    return records


def build_arg_parser() -> argparse.ArgumentParser:
    kwargs = {"description": "List likely active Codex and OpenCode sessions"}
    if Path(sys.argv[0]).name == "__main__.py" and __package__ == "tmux_agent_session":
        kwargs["prog"] = "python -m tmux_agent_session"
    p = argparse.ArgumentParser(**kwargs)
    p.add_argument("--tool", choices=["all", "codex", "opencode"], default="all")
    p.add_argument(
        "--active-minutes",
        type=int,
        default=10,
        help="freshness window for active session writes",
    )
    p.add_argument(
        "--recent-hours",
        type=int,
        default=12,
        help="freshness window for recent session writes",
    )
    p.add_argument("--json", action="store_true", help="emit JSON")
    p.add_argument(
        "--pick",
        action="store_true",
        help="open an interactive picker and focus the selected tmux pane",
    )
    p.add_argument(
        "--show-reasons", action="store_true", help="show why each row was classified"
    )
    p.add_argument("--codex-dir", type=Path, default=DEFAULT_CODEX_DIR)
    p.add_argument("--opencode-dir", type=Path, action="append", default=[])
    p.add_argument(
        "--include-stale", action="store_true", help="include stale sessions"
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.pick and args.json:
        parser.error("--pick cannot be combined with --json")

    records = build_records(args)

    if args.pick:
        return run_picker(records)
    if args.json:
        print_json(records)
    else:
        print_table(records)
        if args.show_reasons:
            print()
            for rec in records:
                print(f"[{rec.tool}] {rec.session_id} -> {rec.status} ({rec.score})")
                for reason in rec.reasons:
                    print(f"  - {reason}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
