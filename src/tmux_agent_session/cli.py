#!/usr/bin/env python3
"""CLI entry point for inspecting Codex and OpenCode tmux sessions."""

from __future__ import annotations

import argparse
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__
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
from .models import ProcessInfo, SessionCandidates, SessionRecord, TmuxPane
from .processes import (
    SESSION_ID_PATTERNS,
    detect_processes,
    extract_session_ids,
    get_cwd,
    normalize_tty,
    parse_etime_to_seconds,
    resolve_process_cwds,
)
from .scoring import (
    add_process_only_records,
    age_minutes,
    mark_feedback_required,
    pane_requires_user_feedback,
    score_session,
    sort_records,
    strip_ansi,
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
    deduplicate_tmux_pane_records,
    detect_tmux_panes,
    focus_tmux_pane,
    tmux_target,
)


_PICKER_EXPORTS = {
    "SessionPickerApp",
    "append_detail",
    "build_picker_details",
    "first_focusable_index",
    "move_selection",
    "picker_detail_items",
    "picker_details_renderable",
    "picker_row_cells",
    "rich_picker_row_cells",
    "run_picker",
}


def _picker_attr(name: str):
    from . import picker

    return getattr(picker, name)


def __getattr__(name: str):
    if name in _PICKER_EXPORTS:
        return _picker_attr(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def append_detail(*args, **kwargs):
    return _picker_attr("append_detail")(*args, **kwargs)


def build_picker_details(*args, **kwargs):
    return _picker_attr("build_picker_details")(*args, **kwargs)


def first_focusable_index(*args, **kwargs):
    return _picker_attr("first_focusable_index")(*args, **kwargs)


def move_selection(*args, **kwargs):
    return _picker_attr("move_selection")(*args, **kwargs)


def picker_detail_items(*args, **kwargs):
    return _picker_attr("picker_detail_items")(*args, **kwargs)


def picker_details_renderable(*args, **kwargs):
    return _picker_attr("picker_details_renderable")(*args, **kwargs)


def picker_row_cells(*args, **kwargs):
    return _picker_attr("picker_row_cells")(*args, **kwargs)


def rich_picker_row_cells(*args, **kwargs):
    return _picker_attr("rich_picker_row_cells")(*args, **kwargs)


def run_picker(records: list[SessionRecord]) -> int:
    return _picker_attr("run_picker")(records)


def load_sessions(
    tool: str,
    base_paths: list[Path],
    candidates: SessionCandidates | None = None,
) -> list[SessionRecord]:
    return _load_harness_sessions(tool, base_paths, candidates)


def tmux_attached_processes(
    processes: list[ProcessInfo], panes: list[TmuxPane]
) -> list[ProcessInfo]:
    pane_ttys = {pane.pane_tty for pane in panes if pane.pane_tty is not None}
    return [
        proc
        for proc in processes
        if proc.tty is not None and normalize_tty(proc.tty) in pane_ttys
    ]


def apply_tmux_pane_cwds(processes: list[ProcessInfo], panes: list[TmuxPane]) -> None:
    cwd_by_tty = {
        pane.pane_tty: pane.pane_current_path
        for pane in panes
        if pane.pane_tty is not None and pane.pane_current_path is not None
    }
    for proc in processes:
        if proc.cwd is not None or proc.tty is None:
            continue
        proc.cwd = cwd_by_tty.get(normalize_tty(proc.tty))


def build_session_candidates(
    processes: list[ProcessInfo],
) -> dict[str, SessionCandidates]:
    ids_by_tool: dict[str, set[str]] = {}
    cwds_by_tool: dict[str, set[str]] = {}

    for proc in processes:
        ids = ids_by_tool.setdefault(proc.tool, set())
        ids.update(session_id for session_id in proc.session_ids if session_id)

        cwd = normalize_cwd(proc.cwd)
        if cwd is not None:
            cwds_by_tool.setdefault(proc.tool, set()).add(cwd)

    tools = set(ids_by_tool) | set(cwds_by_tool)
    return {
        tool: SessionCandidates(
            session_ids=frozenset(ids_by_tool.get(tool, set())),
            cwds=frozenset(cwds_by_tool.get(tool, set())),
        )
        for tool in tools
    }


def session_candidates_for_tool(
    candidates_by_tool: dict[str, SessionCandidates], tool: str
) -> SessionCandidates:
    return candidates_by_tool.get(tool, SessionCandidates())


def build_records(args: argparse.Namespace) -> list[SessionRecord]:
    opencode_dirs = args.opencode_dir or DEFAULT_OPENCODE_DIRS

    with ThreadPoolExecutor(max_workers=2) as executor:
        processes_future = executor.submit(detect_processes)
        panes_future = executor.submit(detect_tmux_panes)
        processes = processes_future.result()
        panes = panes_future.result()

    if args.tool != "all":
        processes = [proc for proc in processes if proc.tool == args.tool]

    attached_processes = tmux_attached_processes(processes, panes)
    apply_tmux_pane_cwds(attached_processes, panes)
    processes_missing_cwd = [proc for proc in attached_processes if proc.cwd is None]
    if processes_missing_cwd:
        resolve_process_cwds(processes_missing_cwd)
    candidates_by_tool = build_session_candidates(attached_processes)
    records: list[SessionRecord] = []

    load_tasks: list[tuple[str, list[Path], SessionCandidates]] = []
    if args.tool in ("all", "codex"):
        load_tasks.append(
            ("codex", [args.codex_dir], session_candidates_for_tool(candidates_by_tool, "codex"))
        )
    if args.tool in ("all", "opencode"):
        load_tasks.append(
            (
                "opencode",
                opencode_dirs,
                session_candidates_for_tool(candidates_by_tool, "opencode"),
            )
        )

    if len(load_tasks) == 1:
        tool, paths, candidates = load_tasks[0]
        records.extend(load_sessions(tool, paths, candidates))
    elif load_tasks:
        with ThreadPoolExecutor(max_workers=len(load_tasks)) as executor:
            futures = [
                executor.submit(load_sessions, tool, paths, candidates)
                for tool, paths, candidates in load_tasks
            ]
            for future in futures:
                records.extend(future.result())

    for rec in records:
        score_session(rec, processes, args.active_minutes, args.recent_hours)

    records = add_process_only_records(records, processes)
    attach_tmux_panes(records, panes)
    records = [rec for rec in records if rec.tmux_pane is not None]
    records = deduplicate_tmux_pane_records(records, "opencode")
    mark_feedback_required(records, capture_tmux_pane_preview)
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
        help="open the Textual picker and focus the selected tmux pane",
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
