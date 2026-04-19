# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Steps 1–3 of the implementation plan are complete. The MCP server is wired and tested; what remains is photo extraction (step 4), batch backfill + review (step 5), and the trend-analysis polish (step 6 — most of the math already lives in `ops.analyze_trends`).

**Authoritative documents:**
- `.scratch/reef-log-plan-seed.md` — original design intent
- `/Users/jason/.claude/plans/read-scratch-reef-log-plan-seed-md-and-u-golden-emerson.md` — executable plan with five decisions baked in (confirmation rule scope, EXIF tz policy, photo grouping window, HI774 ULR unit handling, Salifert/alk-is-manual)

## What this is

A personal, single-user, local-only reef tank log. One Python package, one SQLite file at `~/.reef-log/reef.db`, exposed to Claude as an MCP server over stdio. Used from Claude Desktop / Claude Code on the laptop, and from mobile via `claude remote-control` when the laptop is on.

## Architecture

```
reef_log/
├── db.py          # ✅ sqlite3 stdlib + WAL + FKs + migrations list
├── ops.py         # ✅ the seam: add_test_session, add_maintenance, get_*, analyze_trends, compare_trends
├── mcp_server.py  # ✅ FastMCP stdio entry — seven tools registered
├── cli.py         # ✅ click: test add, maintenance add, history
└── extract.py     # ⏳ step 4 — Hanna photo → reading via Claude vision
```

**Architectural rule:** MCP tools and CLI both call `ops.py` directly. `ops.py` has no MCP, no CLI, no I/O beyond the supplied connection. Future-expansion paths (FastAPI over Tailscale, alt frontends) all hang off this seam.

## Tech stack (locked)

Python 3.11+, `uv`, `sqlite3` stdlib, `mcp` 1.27+ (FastMCP), `anthropic` 0.96+, `Pillow`, `click`, `pytest`, `pytest-cov`, `ruff`. **Do not add:** SQLAlchemy, Alembic, FastAPI, Docker, Pydantic (unless it genuinely simplifies extraction payload validation), HTTP API, auth.

## Commands

`uv` is the installer (builds `.venv` from the lockfile). At runtime, the MCP server launches directly from `.venv/bin/python` so Claude Desktop startup isn't gated on a sync check. Use `uv run` during development for auto-sync.

```sh
uv sync                                              # install deps into .venv (run after pulling changes)
uv run pytest                                        # full suite (51 tests)
uv run pytest tests/test_ops.py -k analyze_trends    # single test pattern
uv run pytest --cov=reef_log --cov-report=term-missing
uv run pytest --run-vision                           # NOT YET — opts into live API tests once they exist
uv run ruff check && uv run ruff format --check
uv run reef-log --help                               # CLI
.venv/bin/python -m reef_log.mcp_server              # MCP stdio server (what Claude Desktop launches)
```

## Data model rules

- Tables: `test_results`, `test_measurements`, `maintenance_events`, `processed_photos` — schema is canonical in `reef_log/db.py`. Bump the migrations list whenever it changes; never edit a past migration.
- Timestamps stored as ISO-8601 UTC text (`YYYY-MM-DDTHH:MM:SS.ffffffZ`) — lexicographically sortable, comparable as strings for `>=` cutoffs.
- `_to_iso` rejects naive datetimes — callers must pass timezone-aware datetimes.
- `tz_assumed=1` flags rows whose timestamp came from naive EXIF interpreted as laptop local time.
- `parameter` is `TEXT`, not an enum — adding salinity/pH later is a no-op.
- `tank` is `TEXT` and required on every write. Canonical values are `"display"` and `"frag"` for real tanks; `"both"` is allowed only on `maintenance_events` for system-wide events (e.g. RO/DI filter swap). Validation lives in `ops._check_tank` / `_check_maintenance_tank` — a typo would otherwise silently disappear from `compare_trends` and tank-filtered reads.
- A real-tank query (`tank="display"` or `"frag"`) on maintenance read paths automatically expands to also match `"both"` rows so shared events show up in either tank's view. Querying `tank="both"` returns only the literal shared events.
- Test sessions are per-tank (one `test_results` row = one tank's reading session). `tank="both"` is rejected on `test_results` since per-parameter values are always tank-specific.
- Append-mostly. Use `UPDATE` for corrections, not deletes.
- Photos stay where they live on disk. Store path + SHA-256 in `processed_photos`, never copy into the project dir or DB.

## MCP behavior rules (from plan decision #1)

- **Manual writes don't echo-back.** `log_test` / `log_maintenance` write directly when called via MCP — the user typed the values, that IS the confirmation. The seed's "always confirm" rule was scoped down because the user enters readings manually rather than via auto-extraction.
- **Photo extraction must not auto-log.** When `extract_from_photo` is built (step 4), it returns a draft only. Writing requires a separate `commit_extracted(payload)` call — this is the mechanical implementation of the confirmation rule.
- **Confidence < 0.85 → review queue**, never the main tables. Same for any group containing duplicate parameters within the 2-hour window.

## Hanna Checker mapping

The user's actual setup (saved in project memory `tank_test_setup.md`):

```python
HANNA_MODELS = {
    "HI758": ("calcium", "ppm"),
    "HI774": ("phosphate", "ppm"),  # HI774 ULR — may display ppb; vision reads on-screen unit; extract.py normalizes
    "HI782": ("nitrate", "ppm"),
    "HI783": ("magnesium", "ppm"),
}
```

**Alkalinity is Salifert (titration), not Hanna.** No LCD to OCR. Alk readings only ever arrive via `log_test` / `cli test add`. The vision prompt covers 4 LCDs, not 5. A Salifert syringe photo fed to the extractor must return confidence 0 with a "not a Hanna LCD" rationale.

## Test conventions

- File-backed tmp DB fixture (`conftest.py`), not `:memory:` — WAL semantics must match prod.
- **Do not mock SQLite** — stdlib sqlite was chosen specifically to keep tests honest. Mock at the network boundary (`anthropic`) and clock boundary (datetime/EXIF) only.
- `test_mcp_server.py` uses real ops against the tmp DB rather than mocks — the MCP wrappers are thin enough that integration tests cost nothing extra.
- A `run_vision` pytest marker (declared in `pyproject.toml`) gates tests that hit the real Anthropic API. Default test runs skip them; use `--run-vision` to opt in.

## Wiring the MCP server into Claude Desktop

Register via the Claude CLI:

```sh
claude mcp add reef-log --scope user -- /bin/bash -c "cd /path/to/reef-log && .venv/bin/python -m reef_log.mcp_server"
```

Replace `/path/to/reef-log` with the actual path to this repository. This launches the MCP server from the project's `.venv` directly — no `uv run` in the hot path, so startup is fast. Tradeoff: you must run `uv sync` yourself after pulling changes that touch dependencies.

Verify with `claude mcp list`. Restart Claude Desktop. The 7 tools (`log_test`, `log_maintenance`, `get_recent`, `get_parameter_history`, `get_last_event`, `analyze_trends`, `compare_trends`) should appear.

## What's next

**Read `.scratch/handoff.md` first** — it has the verification checklist to walk through before step 4, the step-4-onwards file plan, and the open questions to resolve when resuming.

In short: step 4 needs an Anthropic API key (`ANTHROPIC_API_KEY` env var) and 4 real Hanna LCD photos (HI758, HI774 ULR, HI782, HI783) in `tests/fixtures/photos/`. Plan-decision #4 (read on-screen unit, normalize ppb→ppm) drives the vision prompt design.

## Explicit non-goals

Multi-user, auth, hosted service, web UI, automatic alerting, logging while laptop is off, probe/controller ingestion, copying photos into the project dir or DB. If a request implies any of these, surface the conflict with the plan rather than quietly building it.
