# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

The MCP server is wired, tested, and in daily use. A one-time photo-backfill feature (`log_test_from_photo`, `reef-log photos pending`, SHA-256 dedup, EXIF parsing) existed briefly and was removed once the historical Hanna LCD photos were imported. The `processed_photos` SQLite table remains in the schema as an inert audit trail of that backfill — it is no longer written to or read from.

Historical design context, if ever useful, lives in `.scratch/reef-log-plan-seed.md` and `.scratch/handoff.md`.

## What this is

A personal, single-user, local-only reef tank log. One Python package, one SQLite file at `~/.reef-log/reef.db`, exposed to Claude as an MCP server over stdio. Used from Claude Desktop / Claude Code on the laptop, and from mobile via `claude remote-control` when the laptop is on.

## Architecture

```
reef_log/
├── db.py          # ✅ sqlite3 stdlib + WAL + FKs + migrations list
├── ops.py         # ✅ the seam: add_test_session, add_maintenance, get_*, analyze_trends, compare_trends
├── mcp_server.py  # ✅ FastMCP stdio entry — 7 tools
└── cli.py         # ✅ click: test add, maintenance add, history
```

**Architectural rule:** MCP tools and CLI both call `ops.py` directly. `ops.py` has no MCP, no CLI, no I/O beyond the supplied connection. Future-expansion paths (FastAPI over Tailscale, alt frontends) all hang off this seam.

## Tech stack (locked)

Python 3.11+, `uv`, `sqlite3` stdlib, `mcp` 1.27+ (FastMCP), `click`, `pytest`, `pytest-cov`, `ruff`. **Do not add:** `anthropic` SDK, SQLAlchemy, Alembic, FastAPI, Docker, Pydantic, HTTP API, auth.

## Commands

`uv` is the installer (builds `.venv` from the lockfile). At runtime, the MCP server launches directly from `.venv/bin/python` so Claude Desktop startup isn't gated on a sync check. Use `uv run` during development for auto-sync.

```sh
uv sync                                              # install deps into .venv (run after pulling changes)
uv run pytest                                        # full suite
uv run pytest tests/test_ops.py -k analyze_trends    # single test pattern
uv run pytest --cov=reef_log --cov-report=term-missing
uv run ruff check && uv run ruff format --check
uv run reef-log --help                               # CLI
.venv/bin/python -m reef_log.mcp_server              # MCP stdio server (what Claude Desktop launches)
```

## Data model rules

- User-facing tables: `test_results`, `test_measurements`, `maintenance_events`. The `processed_photos` table also exists from an earlier feature but is no longer written or read — leave it alone unless you're intentionally dropping it in a new migration. Schema is canonical in `reef_log/db.py`. Bump the migrations list whenever it changes; never edit a past migration.
- Timestamps stored as ISO-8601 UTC text (`YYYY-MM-DDTHH:MM:SS.ffffffZ`) — lexicographically sortable, comparable as strings for `>=` cutoffs.
- `_to_iso` rejects naive datetimes — callers must pass timezone-aware datetimes.
- `tz_assumed=1` flags rows whose timestamp came from an assumed timezone rather than an explicit one.
- `parameter` is `TEXT`, not an enum — adding salinity/pH later is a no-op.
- `tank` is `TEXT` and required on every write. Canonical values are `"display"` and `"frag"` for real tanks; `"both"` is allowed only on `maintenance_events` for system-wide events (e.g. RO/DI filter swap). Validation lives in `ops._check_tank` / `_check_maintenance_tank` — a typo would otherwise silently disappear from `compare_trends` and tank-filtered reads.
- A real-tank query (`tank="display"` or `"frag"`) on maintenance read paths automatically expands to also match `"both"` rows so shared events show up in either tank's view. Querying `tank="both"` returns only the literal shared events.
- Test sessions are per-tank (one `test_results` row = one tank's reading session). `tank="both"` is rejected on `test_results` since per-parameter values are always tank-specific.
- Append-mostly. Use `UPDATE` for corrections, not deletes.

## MCP behavior rules

- **Manual writes don't echo-back.** `log_test` / `log_maintenance` write directly when called via MCP — the user typed the values, that IS the confirmation.

## Hanna Checker reference

The user's actual test-kit setup (also captured in project memory `tank_test_setup.md`). Useful when the user reports readings verbally — normalize the units here before calling `log_test`.

| model | parameter | canonical unit stored |
|---|---|---|
| HI758 | calcium | ppm |
| HI774 (ULR) | phosphate | ppm — may display ppb on-screen; divide by 1000 before logging |
| HI782 | nitrate | ppm |
| HI783 | magnesium | ppm |

**Alkalinity is Salifert (titration), not Hanna.** No LCD. Alk readings always arrive via `log_test` / `cli test add --alkalinity`.

## Test conventions

- File-backed tmp DB fixture (`conftest.py`), not `:memory:` — WAL semantics must match prod.
- **Do not mock SQLite** — stdlib sqlite was chosen specifically to keep tests honest. Mock the clock boundary only; the server has no network boundary to mock.
- `test_mcp_server.py` uses real ops against the tmp DB rather than mocks — the MCP wrappers are thin enough that integration tests cost nothing extra.

## Wiring the MCP server into Claude Desktop

Register via the Claude CLI:

```sh
claude mcp add reef-log --scope user -- /bin/bash -c "cd /path/to/reef-log && .venv/bin/python -m reef_log.mcp_server"
```

Replace `/path/to/reef-log` with the actual path to this repository. This launches the MCP server from the project's `.venv` directly — no `uv run` in the hot path, so startup is fast. Tradeoff: you must run `uv sync` yourself after pulling changes that touch dependencies.

Verify with `claude mcp list`. Restart Claude Desktop. 7 tools are registered: `log_test`, `log_maintenance`, `get_recent`, `get_parameter_history`, `get_last_event`, `analyze_trends`, `compare_trends`.

## Explicit non-goals

Multi-user, auth, hosted service, web UI, automatic alerting, logging while laptop is off, probe/controller ingestion, re-adding photo ingestion. If a request implies any of these, surface the conflict rather than quietly building it.
