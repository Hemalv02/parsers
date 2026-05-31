#!/usr/bin/env python3
"""Sample a corpus directory and run each file through the parser dispatch.

Usage:
    uv run python scripts/benchmark.py <corpus_dir> [per_ext_sample] [out.json]

Samples up to `per_ext_sample` files per extension (default 12), runs them
through `app.main.dispatch` exactly as the HTTP API would, and
reports per-extension success rate, parser distribution, latency, and a
few sample errors. Results are also written to JSON for later inspection.

This is a smoke/coverage test against real-world files, not a correctness
benchmark — it verifies the pipeline survives messy inputs and routes each
format to the expected converter.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from fastapi import HTTPException

from app.main import ALL_SUPPORTED, dispatch


def collect(corpus: Path, per_ext: int) -> dict[str, list[Path]]:
    buckets: dict[str, list[Path]] = defaultdict(list)
    for p in corpus.rglob("*"):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if len(buckets[ext]) < per_ext:
            buckets[ext].append(p)
    return buckets


def run_one(path: Path, mode: str = "markdown") -> dict:
    start = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        try:
            result = dispatch(path, Path(tmp), mode)
            return {
                "ok": True,
                "parser": result.get("parser"),
                "chars": len(result.get("markdown", "")),
                "ms": round((time.monotonic() - start) * 1000),
            }
        except HTTPException as exc:
            return {
                "ok": False,
                "status": exc.status_code,
                "error": str(exc.detail)[:160],
                "ms": round((time.monotonic() - start) * 1000),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "status": None,
                "error": f"{type(exc).__name__}: {exc}"[:160],
                "ms": round((time.monotonic() - start) * 1000),
            }


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    corpus = Path(sys.argv[1])
    per_ext = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    out_path = Path(sys.argv[3]) if len(sys.argv) > 3 else Path("benchmark_results.json")

    buckets = collect(corpus, per_ext)
    records: list[dict] = []
    print(f"corpus: {corpus}")
    print(f"extensions found: {dict((k, len(v)) for k, v in sorted(buckets.items()))}\n")

    for ext in sorted(buckets):
        files = buckets[ext]
        supported = ext in ALL_SUPPORTED
        oks = 0
        parsers: dict[str, int] = defaultdict(int)
        errors: list[str] = []
        total_ms = 0
        for f in files:
            r = run_one(f)
            r["ext"] = ext
            r["file"] = f.name
            records.append(r)
            total_ms += r["ms"]
            if r["ok"]:
                oks += 1
                parsers[r["parser"]] += 1
            elif len(errors) < 3:
                errors.append(f"[{r.get('status')}] {r['error']}")
        n = len(files)
        avg = round(total_ms / n) if n else 0
        flag = "" if supported else "  (UNSUPPORTED ext — 415 expected)"
        print(f"{ext:8} n={n:<3} ok={oks}/{n}  avg={avg}ms  parsers={dict(parsers)}{flag}")
        for e in errors:
            print(f"         err: {e}")

    out_path.write_text(json.dumps(records, indent=2))
    total = len(records)
    ok_total = sum(1 for r in records if r["ok"])
    print(f"\nTOTAL: {ok_total}/{total} ok  ->  {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
