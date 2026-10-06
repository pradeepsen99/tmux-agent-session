"""Shared synchronous discovery pipeline used by CLI and picker workers."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
from types import ModuleType
from .models import SessionRecord, SessionCandidates

def build_records(args: argparse.Namespace, backend: ModuleType) -> list[SessionRecord]:
    opencode_dirs = args.opencode_dir or backend.DEFAULT_OPENCODE_DIRS

    with ThreadPoolExecutor(max_workers=2) as executor:
        processes_future = executor.submit(backend.detect_processes)
        panes_future = executor.submit(backend.detect_tmux_panes)
        processes = processes_future.result()
        panes = panes_future.result()

    if args.tool != "all":
        processes = [proc for proc in processes if proc.tool == args.tool]

    attached_processes = backend.tmux_attached_processes(processes, panes)
    backend.apply_tmux_pane_cwds(attached_processes, panes)
    processes_missing_cwd = [proc for proc in attached_processes if proc.cwd is None]
    if processes_missing_cwd:
        backend.resolve_process_cwds(processes_missing_cwd)
    candidates_by_tool = backend.build_session_candidates(attached_processes)
    records: list[SessionRecord] = []

    load_tasks: list[tuple[str, list[Path], SessionCandidates]] = []
    if args.tool in ("all", "codex"):
        load_tasks.append(
            ("codex", [args.codex_dir], backend.session_candidates_for_tool(candidates_by_tool, "codex"))
        )
    if args.tool in ("all", "opencode"):
        load_tasks.append(
            (
                "opencode",
                opencode_dirs,
                backend.session_candidates_for_tool(candidates_by_tool, "opencode"),
            )
        )
    if args.tool in ("all", "cursor-agent"):
        load_tasks.append(
            (
                "cursor-agent",
                [args.cursor_dir],
                backend.session_candidates_for_tool(candidates_by_tool, "cursor-agent"),
            )
        )
    if args.tool in ("all", "claude"):
        load_tasks.append(
            (
                "claude",
                [args.claude_dir],
                backend.session_candidates_for_tool(candidates_by_tool, "claude"),
            )
        )

    if len(load_tasks) == 1:
        tool, paths, candidates = load_tasks[0]
        records.extend(backend.load_sessions(tool, paths, candidates))
    elif load_tasks:
        with ThreadPoolExecutor(max_workers=len(load_tasks)) as executor:
            futures = [
                executor.submit(copy_context().run, backend.load_sessions, tool, paths, candidates)
                for tool, paths, candidates in load_tasks
            ]
            for future in futures:
                records.extend(future.result())

    for rec in records:
        backend.score_session(rec, processes, args.active_minutes, args.recent_hours)

    records = backend.add_process_only_records(records, processes)
    backend.attach_tmux_panes(records, panes)
    records = [rec for rec in records if rec.tmux_pane is not None]
    for tool in ("codex", "opencode", "cursor-agent", "claude"):
        records = backend.deduplicate_tmux_pane_records(records, tool)
    backend.mark_feedback_required(records, backend.capture_tmux_pane_preview)
    records = backend.sort_records(records)

    if not args.include_stale:
        records = [r for r in records if r.status != "stale"]

    return records

