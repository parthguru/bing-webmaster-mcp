#!/usr/bin/env bash
# Canonical test entrypoint.
#
# set -o pipefail is the point of this file. `pytest ... | tail` otherwise
# reports tail's exit code, so a failing suite reads as green -- which is
# exactly how a red suite got committed during the 2026-09-02 build run.
# Pipe this script's output freely; the exit code stays honest.
#
# pytest exits 5 when it collects nothing, so an empty or mis-pathed suite
# fails here rather than silently passing.
set -euo pipefail

cd "$(dirname "$0")/.."
uv run pytest tests/ "$@"
