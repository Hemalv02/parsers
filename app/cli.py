"""Command-line entrypoint for the parser service.

    parser-service <file> [--mode markdown|structured|both]
                               Parse one file, print the JSON result to stdout.
    parser-service serve       Run the FastAPI app with uvicorn (dev server).

The single-file path drives the same `dispatch()` the HTTP API uses, so it
doubles as a smoke test for the conversion pipeline without booting a server.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from .config import settings


def _parse_one(file_arg: str, mode: str) -> int:
    from .main import dispatch

    src = Path(file_arg)
    if not src.exists():
        print(f"error: no such file: {src}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        staged = tmpdir / src.name
        staged.write_bytes(src.read_bytes())
        try:
            result = dispatch(staged, tmpdir, mode)
        except Exception as exc:  # noqa: BLE001 - CLI surfaces everything
            print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

    from .metadata import augment as augment_metadata

    out: dict = {
        "parser": result["parser"],
        "mode": mode,
        "filename": src.name,
        "bytes": src.stat().st_size,
        "stats": result.get("stats", {}),
        "metadata": augment_metadata(
            result.get("metadata", {}), result.get("markdown", ""), src.name
        ),
    }
    if mode in ("markdown", "both"):
        out["markdown"] = result["markdown"]
    if mode in ("structured", "both"):
        out["structured"] = result.get("structured")
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def _serve() -> int:
    import uvicorn

    host = "0.0.0.0"
    port = 8000
    uvicorn.run("app.main:app", host=host, port=port, reload=False)
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__, file=sys.stderr)
        return 2
    if args[0] == "serve":
        return _serve()

    mode = settings.default_output_mode
    if "--mode" in args:
        i = args.index("--mode")
        try:
            mode = args[i + 1]
        except IndexError:
            print("error: --mode requires a value", file=sys.stderr)
            return 2
        args = args[:i] + args[i + 2 :]
    if mode not in ("markdown", "structured", "both"):
        print(f"error: invalid mode {mode!r}", file=sys.stderr)
        return 2
    return _parse_one(args[0], mode)


if __name__ == "__main__":
    sys.exit(main())
