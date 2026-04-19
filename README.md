# reef-log

A local-only reef tank log. Records water test results and maintenance events, exposed to Claude as an MCP server over stdio. SQLite at `~/.reef-log/reef.db`. Single user, single machine.

See `CLAUDE.md` for working notes and architecture.

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
```

## Run tests

```sh
uv run pytest
uv run pytest --cov=reef_log --cov-report=term-missing
```

## CLI

```sh
uv run reef-log --help
```

## Register the MCP server with Claude Code

```sh
claude mcp add reef-log --scope user -- /bin/bash -c "cd /path/to/reef-log && .venv/bin/python -m reef_log.mcp_server"
```

Replace `/path/to/reef-log` with the actual path to this repository. Verify with `claude mcp list`.
