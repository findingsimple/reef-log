# reef-log

A local-only reef tank log: water tests, maintenance events, and
conversational photo logging via Claude. Two tanks (`display` / `frag`)
with shared-event support. SQLite at `~/.reef-log/reef.db`. Single user,
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
- `reef-log photos pending <dir> [--recursive]` — list photos in a directory that haven't been logged yet. Useful before a Claude Desktop backfill conversation.

## MCP server

Register with Claude Desktop / Claude Code:

```sh
claude mcp add reef-log --scope user -- /bin/bash -c "cd /path/to/reef-log && .venv/bin/python -m reef_log.mcp_server"
```

Replace `/path/to/reef-log` with the actual repo path. Verify with `claude mcp list`, then restart Claude Desktop.

Eight tools are exposed:

| tool | purpose |
|---|---|
| `log_test` | Log a per-tank water-test session directly (typed values). |
| `log_maintenance` | Log a maintenance event (per-tank or shared). |
| `log_test_from_photo` | Log a test session from a Hanna LCD photo — Claude reads the photo natively, supplies values; server hashes, dedups via SHA-256, parses EXIF, writes. No server-side vision API call. |
| `get_recent` | Unified recent view (tests + maintenance), newest first. |
| `get_parameter_history` | Per-tank time series of measurements. |
| `get_last_event` | "When did I last…" maintenance event. |
| `analyze_trends` | Per-tank min/max/mean/stdev + slope and direction (`rising`/`falling`/`stable`). |
| `compare_trends` | Side-by-side trend stats for both tanks. |

### Photo logging convention

Photo logging is **conversational** — drop a Hanna LCD photo into a Claude Desktop chat and tell Claude which tank, and Claude reads the values natively, states them back to you for confirmation, then calls `log_test_from_photo`. The server never makes its own vision API call; it only hashes, parses EXIF, and writes. Re-dropping the same photo in a new chat is idempotent (SHA-256 dedup).

Supported checkers: HI758 (calcium), HI774 ULR (phosphate — Claude normalizes ppb→ppm in chat), HI782 (nitrate), HI783 (magnesium). Alkalinity is Salifert titration — use `log_test` / `reef-log test add --alkalinity`, never the photo tool.

Supported file formats: `.jpg`, `.png`. HEIC is **not** supported (no `pillow-heif` dep). Convert with `sips -s format jpeg *.HEIC` first.
