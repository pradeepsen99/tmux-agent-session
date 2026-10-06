"""Read conversation details on selection, outside the picker UI thread."""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterator

from .harnesses.codex import _iter_jsonl_head
from .models import SessionRecord


MAX_TEXT_CHARS = 12000


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _excerpt(value: str) -> str:
    if len(value) <= MAX_TEXT_CHARS:
        return value
    return value[:MAX_TEXT_CHARS] + "\n… [truncated]"


def _object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _reverse_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Read newest entries first without loading a whole transcript."""
    try:
        with path.open("rb") as stream:
            position = stream.seek(0, 2)
            remainder = b""
            while position:
                size = min(position, 64 * 1024)
                position -= size
                stream.seek(position)
                lines = (stream.read(size) + remainder).split(b"\n")
                remainder = lines[0]
                for line in reversed(lines[1:]):
                    item = _object(line.decode("utf-8", errors="replace"))
                    if item:
                        yield item
            item = _object(remainder.decode("utf-8", errors="replace"))
            if item:
                yield item
    except OSError:
        return


def _forward_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Read oldest entries first, stopping as soon as the caller does."""
    try:
        with path.open("rb") as stream:
            for line in stream:
                item = _object(line.decode("utf-8", errors="replace"))
                if item:
                    yield item
    except OSError:
        return


def _conversation(
    path: Path, message: Callable[[dict[str, Any]], tuple[str, str]]
) -> dict[str, str]:
    """Find the first user prompt and the agent's most recent reply."""
    details: dict[str, str] = {}
    for role, text in map(message, _forward_jsonl(path)):
        if role == "user" and text:
            details["original_prompt"] = _excerpt(text)
            break
    for role, text in map(message, _reverse_jsonl(path)):
        if role == "assistant" and text:
            details["last_agent_message"] = _excerpt(text)
            break
    return details


def _codex_message(entry: dict[str, Any]) -> tuple[str, str]:
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return "", ""
    if entry.get("type") == "event_msg":
        role = {"user_message": "user", "agent_message": "assistant"}.get(payload.get("type"), "")
        return role, _text(payload.get("message")) if role else ""
    if entry.get("type") == "response_item" and payload.get("type") == "message":
        role = payload.get("role")
        if role not in {"user", "assistant"}:
            return "", ""
        content = payload.get("content")
        if not isinstance(content, list):
            return "", ""
        text = "\n".join(
            _text(part.get("text")) for part in content
            if isinstance(part, dict) and part.get("type") in {"input_text", "output_text", "text"}
        ).strip()
        # These are injected context, not prompts from the user.
        if role == "user" and text.startswith(("# AGENTS.md instructions", "<environment_context>", "<permissions instructions>")):
            return "", ""
        return role, text
    return "", ""


def _codex_details(rec: SessionRecord) -> dict[str, str]:
    details: dict[str, str] = {}
    if rec.path is None:
        return details
    for entry in _iter_jsonl_head(rec.path):
        payload = entry.get("payload")
        if entry.get("type") != "session_meta" or not isinstance(payload, dict):
            continue
        details["title"] = _text(payload.get("title") or payload.get("thread_name"))
        git = payload.get("git")
        if isinstance(git, dict):
            details["recorded_branch"] = _text(git.get("branch"))
        break
    # Use the index belonging to this transcript, including custom Codex homes.
    for parent in rec.path.parents:
        if parent.name == "sessions":
            for entry in _reverse_jsonl(parent.parent / "session_index.jsonl"):
                if entry.get("id") == rec.session_id:
                    details["title"] = _text(entry.get("thread_name"))
                    break
            break
    details.update(_conversation(rec.path, _codex_message))
    return {key: value for key, value in details.items() if value}


CLAUDE_INJECTED_PREFIXES = (
    "<command-name>", "<command-message>", "<local-command-", "<bash-input>",
    "<bash-stdout>", "<bash-stderr>", "<task-notification>",
)


def _claude_message(entry: dict[str, Any]) -> tuple[str, str]:
    # Meta, subagent and compaction entries are not part of the visible conversation.
    if entry.get("type") not in {"user", "assistant"} or entry.get("isMeta") \
            or entry.get("isSidechain") or entry.get("isCompactSummary"):
        return "", ""
    message = entry.get("message")
    if not isinstance(message, dict):
        return "", ""
    content = message.get("content")
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        # Tool calls, tool results and thinking blocks carry no prose for the reader.
        text = "\n".join(
            _text(part.get("text")) for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
    else:
        return "", ""
    role = entry["type"]
    if role == "user" and text.startswith(CLAUDE_INJECTED_PREFIXES):
        return "", ""
    return role, text


def _claude_details(rec: SessionRecord) -> dict[str, str]:
    if rec.path is None:
        return {}
    return _conversation(rec.path, _claude_message)


def _opencode_text(conn: sqlite3.Connection, message_id: str) -> str:
    parts = conn.execute(
        "SELECT data FROM part WHERE message_id = ? ORDER BY time_created, id", (message_id,),
    )
    texts = []
    for row in parts:
        part = _object(row["data"])
        if part.get("type") == "text" and not part.get("synthetic") and not part.get("ignored"):
            text = _text(part.get("text"))
            if text:
                texts.append(text)
    return "\n".join(texts)


def _opencode_details(rec: SessionRecord) -> dict[str, str]:
    details: dict[str, str] = {}
    if rec.path is None or rec.path.suffix != ".db":
        return details
    try:
        conn = sqlite3.connect(rec.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)
        conn.row_factory = sqlite3.Row
        try:
            session = conn.execute("SELECT * FROM session WHERE id = ?", (rec.session_id,)).fetchone()
            if session is not None:
                details["title"] = _text(session["title"]) if "title" in session.keys() else ""
                if "project_id" in session.keys():
                    try:
                        project = conn.execute("SELECT * FROM project WHERE id = ?", (session["project_id"],)).fetchone()
                        if project is not None:
                            details["project"] = _text(project["name"]) if "name" in project.keys() else ""
                            if not details["project"] and "worktree" in project.keys():
                                details["project"] = Path(project["worktree"]).name
                    except (sqlite3.Error, TypeError):
                        pass
            for key, role, order in (("original_prompt", "user", "ASC"), ("last_agent_message", "assistant", "DESC")):
                messages = conn.execute(
                    f"SELECT id, data FROM message WHERE session_id = ? ORDER BY time_created {order}, id {order}",
                    (rec.session_id,),
                )
                for message in messages:
                    if _object(message["data"]).get("role") != role:
                        continue
                    text = _opencode_text(conn, message["id"])
                    if text:
                        details[key] = _excerpt(text)
                        break
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        pass
    return {key: value for key, value in details.items() if value}


def _git(cwd: str, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=1,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def load_session_details(rec: SessionRecord) -> dict[str, str]:
    """Load only the selected session; missing storage or Git is harmless."""
    loader = {"codex": _codex_details, "claude": _claude_details, "opencode": _opencode_details}.get(rec.tool)
    details = loader(rec) if loader else {}
    cwd = rec.cwd or (rec.matched_process.cwd if rec.matched_process else None)
    cwd = cwd or (rec.tmux_pane.pane_current_path if rec.tmux_pane else None)
    if cwd:
        root = _git(cwd, "rev-parse", "--show-toplevel")
        details.setdefault("project", Path(root or cwd).name)
        if root:
            branch = _git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD")
            if not branch:
                commit = _git(cwd, "rev-parse", "--short", "HEAD")
                branch = f"Detached HEAD ({commit})" if commit else ""
            if branch:
                details["git_branch"] = branch
    return details
