# Every recipe runs under bash with pipefail, so a failing command in a
# pipeline fails the target. Without this, `pytest ... | tail` reports the
# exit code of `tail` -- which is how a red suite gets recorded as green.
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

.PHONY: install lint format test build deploy start mcp_inspector clean

install:
	uv sync

lint:
	uv run mypy --strict mcp_server_bwt/
	uv run ruff check --fix mcp_server_bwt/

format:
	uv run ruff format mcp_server_bwt/

# Was: `pytest mcp_server_bwt --doctest-modules --junitxml=...$(cat .python-version)`,
# which collected ZERO tests (the suite lives in tests/, not in the package) and
# depended on .python-version, which is gitignored and absent. It reported success
# while inspecting nothing. pytest exits 5 on "no tests collected", so this target
# now fails loudly if the suite ever disappears.
test:
	uv run pytest tests/ -v

build: clean
	uv run build

deploy: install build

# `ship_it: build / git push` removed 2026-09-02. This repo's remote was the
# UPSTREAM author's public repository, so one word published Synectus work to a
# stranger's repo. Pushing is now deliberate and manual, never a make target.

start:
	uv run mcp_server_bwt/main.py

mcp_inspector:
	npx @modelcontextprotocol/inspector uv --directory ${PWD} run mcp_server_bwt/main.py

clean:
	rm -rf dist/ build/ reports/ *.egg-info/ *cache
