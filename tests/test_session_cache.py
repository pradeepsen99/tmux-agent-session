import json
from pathlib import Path

from tmux_agent_session.harnesses import codex
from tmux_agent_session.models import SessionCandidates
from tmux_agent_session.session_cache import CACHE_ENABLED, SessionCache, fingerprint


def transcript(path, model="one"):
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "session-one", "cwd": str(path.parent)}}) + "\n" + json.dumps({"type": "turn_context", "payload": {"model": model}}) + "\n")


def test_cache_reuses_and_invalidates(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    path = tmp_path / "log.jsonl"
    transcript(path)
    original = codex.extract_codex_session
    calls = []
    def extract(path, candidates=None):
        calls.append(path)
        return original(path, candidates)
    monkeypatch.setattr(codex, "extract_codex_session", extract)
    baseline = codex.load_sessions([path])
    assert codex.load_sessions([path]) == baseline
    assert len(calls) == 1
    transcript(path, "second")
    assert codex.load_sessions([path])[0].metadata["model"] == "second"
    assert len(calls) == 2
    path.unlink()
    assert codex.load_sessions([path]) == []


def test_nonmatching_and_invalid_results_are_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    path = tmp_path / "log.jsonl"
    transcript(path)
    assert codex.load_sessions([path], SessionCandidates(cwds=frozenset({"/absent"}))) == []
    monkeypatch.setattr(codex, "extract_codex_session", lambda *_: (_ for _ in ()).throw(AssertionError("parsed")))
    assert len(codex.load_sessions([path])) == 1


def test_cache_changed_during_parse_and_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    path = tmp_path / "log.jsonl"
    transcript(path)
    cache = SessionCache()
    stamp = fingerprint(path)
    transcript(path, "changed")
    cache.write(path, stamp, None)
    assert cache.read(path, stamp) == (False, None)
    cache.close()
    token = CACHE_ENABLED.set(False)
    try:
        cache = SessionCache()
        assert cache.conn is None
    finally:
        CACHE_ENABLED.reset(token)


def test_corrupt_cache_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    directory = tmp_path / "tmux-agent-session"
    directory.mkdir()
    (directory / "metadata.sqlite3").write_text("not sqlite")
    path = tmp_path / "log.jsonl"
    transcript(path)
    assert len(codex.load_sessions([path])) == 1


def test_fast_parser_matches_fallback(tmp_path):
    path = tmp_path / "log.jsonl"
    for prefix in ("", "invalid\n", "{}\n"):
        transcript(path)
        path.write_text(prefix + path.read_text())
        assert codex._extract_codex_payloads(path) == codex._extract_codex_payloads_fallback(path)
    path.write_text('{"type":"session_meta","payload":{"id":"session-one"}}\n{"broken":')
    assert codex._extract_codex_payloads(path) == codex._extract_codex_payloads_fallback(path)


def test_replacement_version_and_invalid_result(tmp_path, monkeypatch):
    import os
    from tmux_agent_session import session_cache
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    path = tmp_path / "log.jsonl"
    transcript(path)
    codex.load_sessions([path])
    before = path.stat()
    replacement = tmp_path / "new.jsonl"
    transcript(replacement, "two")
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    replacement.replace(path)
    assert codex.load_sessions([path])[0].metadata["model"] == "two"
    monkeypatch.setattr(session_cache, "PARSER_VERSION", 99)
    cache = SessionCache()
    assert cache.read(path, fingerprint(path)) == (False, None)
    cache.close()
    path.write_text("not json")
    assert codex.load_sessions([path]) == []
    monkeypatch.setattr(codex, "extract_codex_session", lambda *_: (_ for _ in ()).throw(AssertionError("parsed")))
    assert codex.load_sessions([path]) == []


def test_locked_and_unwritable_cache(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cache = SessionCache()
    cache.close()
    conn = sqlite3.connect(tmp_path / "cache/tmux-agent-session/metadata.sqlite3")
    conn.execute("BEGIN EXCLUSIVE")
    path = tmp_path / "log.jsonl"
    transcript(path)
    try:
        assert len(codex.load_sessions([path])) == 1
    finally:
        conn.rollback()
        conn.close()
    monkeypatch.setenv("XDG_CACHE_HOME", str(path))
    assert len(codex.load_sessions([path])) == 1


def test_concurrent_loaders_and_uncached_equivalence(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    path = tmp_path / "log.jsonl"
    transcript(path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: codex.load_sessions([path]), range(2)))
    token = CACHE_ENABLED.set(False)
    try:
        uncached = codex.load_sessions([path])
    finally:
        CACHE_ENABLED.reset(token)
    assert results[0] == results[1] == uncached
