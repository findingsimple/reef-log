# reef-log

A local-only reef tank log: water tests and maintenance events, exposed
to Claude via an MCP server. Two tanks (`display` / `frag`) with
shared-event support. SQLite at `~/.reef-log/reef.db`. Single user,
single machine.

See `CLAUDE.md` for architecture and working notes.

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
```

## Run tests

```sh
uv run pytest                                   # full suite
uv run pytest --cov=reef_log --cov-report=term-missing
```

## CLI

```sh
uv run reef-log --help
```

Commands:

- `reef-log test add --tank {display,frag} [--alkalinity N] [--calcium N] ...` — log a water-test session.
- `reef-log maintenance add <event_type> --tank {display,frag,both} [--detail key=value]` — log a maintenance event. Use `--tank both` for system-wide events like an RO/DI filter swap.
- `reef-log history [--tank T] [--parameter P] [--event-type E] [--days N]` — recent activity, newest first.

## MCP server

Register with Claude Desktop / Claude Code:

```sh
claude mcp add reef-log --scope user -- /bin/bash -c "cd /path/to/reef-log && .venv/bin/python -m reef_log.mcp_server"
```

Replace `/path/to/reef-log` with the actual repo path. Verify with `claude mcp list`, then restart Claude Desktop.

Seven tools are exposed:

| tool | purpose |
|---|---|
| `log_test` | Log a per-tank water-test session directly (typed values). |
| `log_maintenance` | Log a maintenance event (per-tank or shared). |
| `get_recent` | Unified recent view (tests + maintenance), newest first. |
| `get_parameter_history` | Per-tank time series of measurements. |
| `get_last_event` | "When did I last…" maintenance event. |
| `analyze_trends` | Per-tank min/max/mean/stdev + slope and direction (`rising`/`falling`/`stable`). |
| `compare_trends` | Side-by-side trend stats for both tanks. |
