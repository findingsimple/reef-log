"""MCP stdio server — the conversational entry point.

Tools wrap ops.py one-to-one. Each tool opens a fresh DB connection,
runs the operation, closes the connection. Tests monkeypatch
`_db_path_override` to redirect at a tmp DB.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

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


def main() -> None:
    """Entry point for the MCP stdio server."""
    mcp.run()


if __name__ == "__main__":
    main()
