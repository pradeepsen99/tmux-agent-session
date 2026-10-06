"""Repeatable metadata scan benchmark; pass --live for real CLI timings."""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from .harnesses import codex
from .session_cache import CACHE_ENABLED


def summarize(values: list[float]) -> dict[str, float]:
    return {"median_ms": round(statistics.median(values), 2),
            "min_ms": round(min(values), 2), "max_ms": round(max(values), 2)}


def fixture_scan(base: Path, runs: int, enabled: bool) -> dict:
    elapsed, discovery, parsing, parsed = [], [], [], []
    for _ in range(runs):
        metrics = {"discovery_ms": 0.0, "parsed_files": 0, "parsing_ms": 0.0}
        find = codex.find_codex_session_files
        extract = codex.extract_codex_session
        def timed_find(path):
            start = time.perf_counter()
            result = find(path)
            metrics["discovery_ms"] += (time.perf_counter() - start) * 1000
            return result
        def counted_extract(*args, **kwargs):
            metrics["parsed_files"] += 1
            start = time.perf_counter()
            result = extract(*args, **kwargs)
            metrics["parsing_ms"] += (time.perf_counter() - start) * 1000
            return result
        token = CACHE_ENABLED.set(enabled)
        try:
            with patch.object(codex, "find_codex_session_files", timed_find), patch.object(codex, "extract_codex_session", counted_extract):
                start = time.perf_counter()
                codex.load_sessions([base])
                elapsed.append((time.perf_counter() - start) * 1000)
        finally:
            CACHE_ENABLED.reset(token)
        discovery.append(metrics["discovery_ms"])
        parsed.append(metrics["parsed_files"])
        parsing.append(metrics["parsing_ms"])
    return {"total": summarize(elapsed), "discovery": summarize(discovery), "parsing": summarize(parsing), "parsed_files": parsed}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--files", type=int, default=1000)
    args = parser.parse_args()
    if args.runs < 1 or args.files < 1:
        parser.error("--runs and --files must be positive")
    if args.live:
        report = {}
        # Populate the cache before measuring warm process launches.
        subprocess.run([sys.executable, "-m", "tmux_agent_session", "--json"], check=True, stdout=subprocess.DEVNULL)
        for label, flags in (("uncached", ["--no-cache"]), ("warm", [])):
            samples = []
            for _ in range(args.runs):
                start = time.perf_counter()
                subprocess.run([sys.executable, "-m", "tmux_agent_session", "--json"] + flags, check=True, stdout=subprocess.DEVNULL)
                samples.append((time.perf_counter() - start) * 1000)
            report[label] = summarize(samples)
    else:
        with tempfile.TemporaryDirectory(prefix="tas-benchmark-") as temporary:
            base = Path(temporary) / "sessions"
            base.mkdir()
            for index in range(args.files):
                entries = [{"type": "session_meta", "payload": {"id": f"session-{index}", "cwd": str(base)}}]
                entries += [{"type": "response_item", "payload": {"text": "x" * 2000}}] * 80
                entries += [{"type": "turn_context", "payload": {"model": "fixture-model"}}]
                (base / f"{index}.jsonl").write_text("\n".join(json.dumps(entry) for entry in entries))
            with patch.dict(os.environ, {"XDG_CACHE_HOME": str(Path(temporary) / "cache")}):
                report = {"files": args.files, "uncached": fixture_scan(base, args.runs, False),
                          "cold": fixture_scan(base, 1, True), "warm": fixture_scan(base, args.runs, True)}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
