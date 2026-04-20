"""SQLite connection, schema, and migrations.

Plain stdlib sqlite3. WAL mode + foreign keys are enabled at connect time.
Migrations are an ordered list of SQL strings keyed by version, applied
inside a transaction. PRAGMA user_version tracks the applied version.

The schema defined here is canonical — bump the migrations list whenever
it changes. Never edit a past migration.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / ".reef-log" / "reef.db"


MIGRATIONS: list[str] = [
    # v1 — initial schema
    """
    CREATE TABLE test_results (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        measured_at     TEXT NOT NULL,
        tz_assumed      INTEGER NOT NULL DEFAULT 0,
        logged_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        source          TEXT NOT NULL,
        notes           TEXT
    );

    CREATE INDEX idx_test_results_measured_at ON test_results(measured_at);

    CREATE TABLE test_measurements (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        test_result_id  INTEGER NOT NULL REFERENCES test_results(id) ON DELETE CASCADE,
        parameter       TEXT NOT NULL,
        value           REAL NOT NULL,
        unit            TEXT NOT NULL,
        checker_model   TEXT,
        confidence      REAL
    );

    CREATE INDEX idx_test_measurements_test_result_id ON test_measurements(test_result_id);
    CREATE INDEX idx_test_measurements_parameter ON test_measurements(parameter);

    CREATE TABLE maintenance_events (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        performed_at    TEXT NOT NULL,
        tz_assumed      INTEGER NOT NULL DEFAULT 0,
        logged_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        event_type      TEXT NOT NULL,
        details         TEXT,
        notes           TEXT
    );

    CREATE INDEX idx_maintenance_events_performed_at ON maintenance_events(performed_at);
    CREATE INDEX idx_maintenance_events_event_type ON maintenance_events(event_type);

    CREATE TABLE processed_photos (
        sha256              TEXT PRIMARY KEY,
        path                TEXT NOT NULL,
        extracted_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        test_result_id      INTEGER REFERENCES test_results(id) ON DELETE SET NULL,
        status              TEXT NOT NULL,
        extracted_payload   TEXT
    );

    CREATE INDEX idx_processed_photos_status ON processed_photos(status);
    """,
    # v2 — multi-tank support. DEFAULT 'display' satisfies SQLite's
    # ADD COLUMN NOT NULL requirement; never fires for new rows since ops.py
    # requires an explicit tank argument.
    """
    ALTER TABLE test_results ADD COLUMN tank TEXT NOT NULL DEFAULT 'display';
    ALTER TABLE maintenance_events ADD COLUMN tank TEXT NOT NULL DEFAULT 'display';
    ALTER TABLE processed_photos ADD COLUMN tank TEXT NOT NULL DEFAULT 'display';

    CREATE INDEX idx_test_results_tank ON test_results(tank);
    CREATE INDEX idx_maintenance_events_tank ON maintenance_events(tank);
    """,
]


def connect(db_path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    """Open the DB, enable WAL + FKs, run any pending migrations."""
    target = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    if str(target) != ":memory:":
        target.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        str(target),
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,  # we manage transactions explicitly
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")

    _migrate(conn)
    return conn


def _current_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _migrate(conn: sqlite3.Connection) -> None:
    current = _current_version(conn)
    target = len(MIGRATIONS)
    if current >= target:
        return

    with transaction(conn):
        for version in range(current, target):
            for stmt in _split_statements(MIGRATIONS[version]):
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {version + 1}")


def _split_statements(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Wrap a block in BEGIN IMMEDIATE / COMMIT, rolling back on any exception.

    Required because `connect()` sets `isolation_level=None` (autocommit), so
    multi-statement atomicity is the caller's responsibility. Used by
    migrations and by `ops.log_test_from_photo` to keep the test session +
    processed_photos write pair indivisible.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
