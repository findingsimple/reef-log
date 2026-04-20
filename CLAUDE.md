# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Steps 1–3 of the implementation plan are complete. The MCP server is wired and tested; what remains is conversational photo logging (step 4 — `log_test_from_photo` tool with SHA-256 dedup, **no server-side vision call**), bulk photo sessions (step 5 — also conversational, not a batch CLI), and trend-analysis polish (step 6 — most of the math already lives in `ops.analyze_trends`).

**Authoritative documents:**
- `.scratch/reef-log-plan-seed.md` — original design intent
- `/Users/jason/.claude/plans/read-scratch-reef-log-plan-seed-md-and-u-golden-emerson.md` — executable plan. Six decisions are baked in, with **#1 revised (2026-04-20)**: photo logging is conversational + server-side SHA-256 dedup, not a two-step `extract_from_photo` / `commit_extracted` tool pair. Rationale: halves vision-token cost by avoiding a duplicate vision read; eliminates `extract.py` + vision prompt + mocked-API test scaffolding.

## What this is

A personal, single-user, local-only reef tank log. One Python package, one SQLite file at `~/.reef-log/reef.db`, exposed to Claude as an MCP server over stdio. Used from Claude Desktop / Claude Code on the laptop, and from mobile via `claude remote-control` when the laptop is on.

## Architecture

```
reef_log/
├── db.py          # ✅ sqlite3 stdlib + WAL + FKs + migrations list
├── ops.py         # ✅ the seam: add_test_session, add_maintenance, get_*, analyze_trends, compare_trends; +log_test_from_photo / +is_photo_processed (step 4)
├── mcp_server.py  # ✅ FastMCP stdio entry — 7 tools today, 8 after step 4
└── cli.py         # ✅ click: test add, maintenance add, history
```

**No `extract.py`.** Vision is done by the calling Claude in conversation, not by server code.

**Architectural rule:** MCP tools and CLI both call `ops.py` directly. `ops.py` has no MCP, no CLI, no I/O beyond the supplied connection. Future-expansion paths (FastAPI over Tailscale, alt frontends) all hang off this seam.

## Tech stack (locked)

Python 3.11+, `uv`, `sqlite3` stdlib, `mcp` 1.27+ (FastMCP), `Pillow` (EXIF parsing only), `click`, `pytest`, `pytest-cov`, `ruff`. The server does NOT call Claude's vision API — the calling Claude (Desktop/Code) reads photos natively and supplies values. **Do not add:** `anthropic` SDK (see MCP behavior rules — no server-side vision), SQLAlchemy, Alembic, FastAPI, Docker, Pydantic, HTTP API, auth.

## Commands

`uv` is the installer (builds `.venv` from the lockfile). At runtime, the MCP server launches directly from `.venv/bin/python` so Claude Desktop startup isn't gated on a sync check. Use `uv run` during development for auto-sync.

```sh
uv sync                                              # install deps into .venv (run after pulling changes)
uv run pytest                                        # full suite (51 tests)
uv run pytest tests/test_ops.py -k analyze_trends    # single test pattern
uv run pytest --cov=reef_log --cov-report=term-missing
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

## MCP behavior rules (plan decision #1, revised 2026-04-20)

- **Manual writes don't echo-back.** `log_test` / `log_maintenance` write directly when called via MCP — the user typed the values, that IS the confirmation.
- **Photo logging is conversational, not server-side vision.** The calling Claude (Desktop/Code) reads the photo natively in the user's conversation, extracts readings, and calls `log_test_from_photo(path, tank, measurements, notes?)` with the already-extracted values. The server **does not** call Anthropic vision — it only hashes the file, checks `processed_photos` for dedup, and writes. This avoids a duplicate vision read (halves vision-token cost) and eliminates the `extract.py` / vision-prompt / mocked-API scaffolding.
- **Confirmation stays conversational.** The read-in-chat flow naturally lets the user correct values before Claude calls the tool. The original mechanical two-step (`extract_from_photo` → `commit_extracted`) is no longer needed — confirmation is enforced by the conversation, not by the tool surface.
- **Dedup is authoritative.** `log_test_from_photo` rejects any write whose SHA-256 is already in `processed_photos` — re-dropping the same photo into a new conversation will not double-log.
- **Duplicate parameters in one session** are still a smell. If Claude sees two HI758 calcium photos from the same tank within a short window, it should ask before logging both.

## Hanna Checker mapping

The user's actual setup (saved in project memory `tank_test_setup.md`). This is reference for the Claude in conversation — it does not live in any server-side code under option C.

| model | parameter | canonical unit stored |
|---|---|---|
| HI758 | calcium | ppm |
| HI774 (ULR) | phosphate | ppm — **may display ppb on-screen; Claude normalizes ÷1000 before calling `log_test_from_photo`** |
| HI782 | nitrate | ppm |
| HI783 | magnesium | ppm |

**Alkalinity is Salifert (titration), not Hanna.** No LCD to OCR. Alk readings only ever arrive via `log_test` / `cli test add`. A Salifert syringe photo dropped into chat should be recognized as non-Hanna and either logged via `log_test` (with the user telling Claude the endpoint reading) or declined — never `log_test_from_photo`.

## Test conventions

- File-backed tmp DB fixture (`conftest.py`), not `:memory:` — WAL semantics must match prod.
- **Do not mock SQLite** — stdlib sqlite was chosen specifically to keep tests honest. Mock the clock boundary (datetime/EXIF) only; under option C there's no network boundary to mock because the server doesn't call external APIs.
- `test_mcp_server.py` uses real ops against the tmp DB rather than mocks — the MCP wrappers are thin enough that integration tests cost nothing extra.
- Photo-related tests use real JPG fixtures in `tests/fixtures/photos/` (SHA-256 hashed, EXIF parsed by Pillow — not interpreted for LCD content).

## Wiring the MCP server into Claude Desktop

Register via the Claude CLI:

```sh
claude mcp add reef-log --scope user -- /bin/bash -c "cd /path/to/reef-log && .venv/bin/python -m reef_log.mcp_server"
```

Replace `/path/to/reef-log` with the actual path to this repository. This launches the MCP server from the project's `.venv` directly — no `uv run` in the hot path, so startup is fast. Tradeoff: you must run `uv sync` yourself after pulling changes that touch dependencies.

Verify with `claude mcp list`. Restart Claude Desktop. 7 tools are registered today (`log_test`, `log_maintenance`, `get_recent`, `get_parameter_history`, `get_last_event`, `analyze_trends`, `compare_trends`); step 4 adds an eighth (`log_test_from_photo`).

## What's next

**Read `.scratch/handoff.md` first** — it has the verification checklist and the concrete step-4 file plan (revised for the conversational-extraction decision).

In short: step 4 adds `ops.is_photo_processed` + `ops.log_test_from_photo` + a single MCP tool wrapper. **No API key required at runtime.** Tests use the 8 canonical JPG fixtures already in `tests/fixtures/photos/` (named `HI{model}_{parameter}_{tank}.jpg`, one per checker × tank combo from session 1). Plan-decision #4 (HI774 ULR ppb→ppm) is now Claude's responsibility in conversation, not server code.

## Explicit non-goals

Multi-user, auth, hosted service, web UI, automatic alerting, logging while laptop is off, probe/controller ingestion, copying photos into the project dir or DB. If a request implies any of these, surface the conflict with the plan rather than quietly building it.
