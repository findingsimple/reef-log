"""MCP stdio server — the conversational entry point.

Tools wrap ops.py one-to-one. Each tool opens a fresh DB connection,
runs the operation, closes the connection. Tests monkeypatch
`_db_path_override` to redirect at a tmp DB.

Photo logging (`log_test_from_photo`) is conversational under option C:
the calling Claude reads the image natively, supplies the measurements,
and the server dedups by SHA-256 + parses EXIF for the timestamp. No
server-side vision call is made.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from PIL import UnidentifiedImageError

from reef_log import db as db_module
from reef_log import ops

# Test hook: monkeypatch this to redirect every tool at a tmp DB.
_db_path_override: Path | None = None


@contextmanager
def _connection() -> Iterator[sqlite3.Connection]:
    conn = db_module.connect(_db_path_override)
    try:
        yield conn
    finally:
        conn.close()


mcp = FastMCP("reef-log")


@mcp.tool()
def log_test(
    tank: str,
    measurements: list[dict[str, Any]],
    measured_at: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Log a water-test session for one tank.

    tank: 'display' or 'frag' — readings are always per-tank.
    measurements: list of {parameter, value, unit?, checker_model?}.
    parameter must be one of: alkalinity, calcium, magnesium, phosphate, nitrate.
    measured_at: ISO 8601 UTC; defaults to now.
    """
    with _connection() as conn:
        test_id = ops.add_test_session(
            conn, measurements, tank=tank, measured_at=measured_at, source="mcp", notes=notes
        )
    rendered = ", ".join(f"{m['parameter']}={m['value']}" for m in measurements)
    return {"id": test_id, "tank": tank, "logged": rendered}


@mcp.tool()
def log_maintenance(
    tank: str,
    event_type: str,
    performed_at: str | None = None,
    details: dict[str, Any] | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Log a maintenance event for one tank, or both via tank='both'.

    tank: 'display', 'frag', or 'both' (use 'both' for system-wide events
    like an RO/DI filter swap — read paths surface 'both' rows in either
    tank's view).
    event_type: water_change, chemical_refill, equipment_change, filter_media,
    livestock, or observation.
    performed_at: ISO 8601 UTC; defaults to now.
    details: free-form JSON-serializable payload appropriate for the event_type.
    """
    with _connection() as conn:
        mid = ops.add_maintenance(
            conn, event_type, tank=tank, performed_at=performed_at, details=details, notes=notes
        )
    return {"id": mid, "tank": tank, "event_type": event_type}


@mcp.tool()
def get_recent(
    days: int = 30,
    tank: str | None = None,
    parameter: str | None = None,
    event_type: str | None = None,
) -> list[dict[str, Any]]:
    """Unified recent activity (tests + maintenance), newest first.

    tank: optional filter — 'display' or 'frag' surfaces that tank plus shared
    maintenance ('both'); 'both' surfaces only shared maintenance; omit for all.
    parameter restricts to test sessions containing it; event_type restricts
    to maintenance of that type.
    """
    with _connection() as conn:
        return ops.get_recent(
            conn, tank=tank, days=days, parameter=parameter, event_type=event_type
        )


@mcp.tool()
def get_parameter_history(parameter: str, tank: str, days: int = 90) -> list[dict[str, Any]]:
    """Per-tank time series of measurements for a single parameter, ascending by time."""
    with _connection() as conn:
        return ops.get_parameter_history(conn, parameter, tank=tank, days=days)


@mcp.tool()
def get_last_event(event_type: str, tank: str) -> dict[str, Any] | None:
    """Most recent maintenance event of the given type for the given tank, or null.

    tank='display' or 'frag' surfaces shared ('both') events too; tank='both'
    returns only shared events.
    """
    with _connection() as conn:
        return ops.get_last_event(conn, event_type, tank=tank)


@mcp.tool()
def analyze_trends(parameter: str, tank: str, days: int = 90) -> dict[str, Any]:
    """Per-tank min/max/mean/stdev for a parameter, plus a one-line summary."""
    with _connection() as conn:
        return ops.analyze_trends(conn, parameter, tank=tank, days=days)


@mcp.tool()
def compare_trends(parameter: str, days: int = 90) -> dict[str, Any]:
    """Side-by-side trend stats for both tanks over the same window."""
    with _connection() as conn:
        return ops.compare_trends(conn, parameter, days=days)


@mcp.tool()
def log_test_from_photo(
    path: str,
    tank: str,
    measurements: list[dict[str, Any]],
    notes: str | None = None,
) -> dict[str, Any]:
    """Log a water-test session sourced from a Hanna LCD photo.

    You (Claude) read the photo in conversation and supply `measurements`.
    The server hashes the file (SHA-256), parses EXIF for measured_at,
    and rejects duplicates — re-dropping the same photo in a new chat
    returns an `already_processed` error rather than double-logging.

    **Convention:** always state the readings you extracted ("I see calcium
    446 ppm and magnesium 1380 ppm") in your reply BEFORE calling this tool,
    so the user can correct a misread digit before anything is written.

    path: absolute path to the photo file (JPG or PNG). HEIC is not supported
        — Pillow can't decode HEIC without the optional pillow-heif plugin,
        which is not a project dependency.
    tank: 'display' or 'frag'.
    measurements: list of {parameter, value, unit?, checker_model?}.
        For HI774 ULR showing ppb, normalize ÷1000 and supply unit='ppm'.
        Alkalinity is Salifert — never use this tool for alk; use log_test.
    notes: optional free-form string.

    Returns either {test_result_id, sha256, measured_at, tz_assumed} on
    success, or {error, message} on a recognized failure. Recognized errors:
    'already_processed', 'invalid_tank', 'not_found', 'not_an_image',
    'invalid_photo'.
    """
    with _connection() as conn:
        try:
            return ops.log_test_from_photo(
                conn,
                path=path,
                tank=tank,
                measurements=measurements,
                notes=notes,
            )
        except ops.AlreadyProcessed as exc:
            return {"error": "already_processed", "message": str(exc)}
        except ops.InvalidTank as exc:
            return {"error": "invalid_tank", "message": str(exc)}
        except sqlite3.IntegrityError as exc:
            # Lost a race with a concurrent call on the same SHA (pre-check
            # passed but the INSERT hit the UNIQUE constraint). Surface as
            # the same structured error as the dedup path.
            return {"error": "already_processed", "message": str(exc)}
        except FileNotFoundError as exc:
            return {"error": "not_found", "message": str(exc)}
        except UnidentifiedImageError as exc:
            return {"error": "not_an_image", "message": str(exc)}
        except ValueError as exc:
            # Size-guard and any other validation ValueError not already
            # handled by AlreadyProcessed / InvalidTank above.
            return {"error": "invalid_photo", "message": str(exc)}


def main() -> None:
    """Entry point for the MCP stdio server."""
    mcp.run()


if __name__ == "__main__":
    main()
