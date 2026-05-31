#!/usr/bin/env bash
# PostToolUse hook (Write|Edit|MultiEdit): ruff-format + lint-fix the written
# Python file using the project's uv-pinned ruff.
#
# Reads the tool-call JSON on stdin (`.tool_input.file_path`). No-ops for
# non-.py files and vendored code. Always exits 0 — it tidies the file without
# blocking the write; remaining unfixable lint still prints for visibility.

f=$(jq -r '.tool_input.file_path // empty')
[ -z "$f" ] && exit 0

case "$f" in
  *.py) ;;
  *) exit 0 ;;
esac

# Vendored third-party code is kept verbatim — never reformat it.
case "$f" in
  */app/vendor/*) exit 0 ;;
esac

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$f")}" || exit 0

uv run ruff format "$f"
uv run ruff check --fix "$f"
exit 0
