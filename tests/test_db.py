"""Tests for db.connect, migrations, WAL, and FK behavior."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from reef_log import db as db_module


def test_connect_creates_parent_dir(tmp_path: Path):
    nested = tmp_path / "a" / "b" / "reef.db"
    conn = db_module.connect(nested)
    try:
        assert nested.parent.is_dir()
        assert nested.exists()
    finally:
        conn.close()


def test_connect_enables_wal(conn: sqlite3.Connection):
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_connect_enables_foreign_keys(conn: sqlite3.Connection):
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1


def test_migrations_advance_user_version(conn: sqlite3.Connection):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == len(db_module.MIGRATIONS)
    assert version >= 1


def test_migrations_idempotent(db_path: Path):
    c1 = db_module.connect(db_path)
    v1 = c1.execute("PRAGMA user_version").fetchone()[0]
    c1.close()

    c2 = db_module.connect(db_path)
    v2 = c2.execute("PRAGMA user_version").fetchone()[0]
    c2.close()

    assert v1 == v2 == len(db_module.MIGRATIONS)


def test_v1_schema_creates_all_tables(conn: sqlite3.Connection):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = {r[0] for r in rows}
    expected = {"test_results", "test_measurements", "maintenance_events", "processed_photos"}
    assert expected.issubset(names)


def test_fk_cascade_deletes_measurements(conn: sqlite3.Connection):
    conn.execute("BEGIN")
    cur = conn.execute(
        "INSERT INTO test_results (measured_at, source) VALUES (?, ?)",
        ("2026-04-19T00:00:00.000Z", "test"),
    )
    test_id = cur.lastrowid
    conn.execute(
        "INSERT INTO test_measurements (test_result_id, parameter, value, unit) "
        "VALUES (?, ?, ?, ?)",
        (test_id, "alkalinity", 8.2, "dKH"),
    )
    conn.execute("COMMIT")

    assert conn.execute("SELECT COUNT(*) FROM test_measurements").fetchone()[0] == 1
    conn.execute("DELETE FROM test_results WHERE id = ?", (test_id,))
    assert conn.execute("SELECT COUNT(*) FROM test_measurements").fetchone()[0] == 0


def test_processed_photos_set_null_on_test_result_delete(conn: sqlite3.Connection):
    conn.execute("BEGIN")
    cur = conn.execute(
        "INSERT INTO test_results (measured_at, source) VALUES (?, ?)",
        ("2026-04-19T00:00:00.000Z", "photo:abc"),
    )
    test_id = cur.lastrowid
    conn.execute(
        "INSERT INTO processed_photos (sha256, path, status, test_result_id) VALUES (?, ?, ?, ?)",
        ("abc", "/tmp/foo.jpg", "auto_logged", test_id),
    )
    conn.execute("COMMIT")

    conn.execute("DELETE FROM test_results WHERE id = ?", (test_id,))
    row = conn.execute(
        "SELECT test_result_id FROM processed_photos WHERE sha256 = ?", ("abc",)
    ).fetchone()
    assert row["test_result_id"] is None


def test_fk_enforcement_rejects_orphan_measurement(conn: sqlite3.Connection):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO test_measurements (test_result_id, parameter, value, unit) "
            "VALUES (?, ?, ?, ?)",
            (99999, "alkalinity", 8.2, "dKH"),
        )


def test_logged_at_default_populated(conn: sqlite3.Connection):
    cur = conn.execute(
        "INSERT INTO test_results (measured_at, source) VALUES (?, ?)",
        ("2026-04-19T00:00:00.000Z", "test"),
    )
    row = conn.execute(
        "SELECT logged_at FROM test_results WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    assert row["logged_at"] is not None
    assert row["logged_at"].endswith("Z")


def test_v2_tank_columns_exist(conn: sqlite3.Connection):
    """Migration 2 adds a tank column to the three user-facing tables."""
    for table in ("test_results", "maintenance_events", "processed_photos"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        assert "tank" in cols, f"{table} missing tank column"


def test_v2_tank_default_applies_when_column_omitted(conn: sqlite3.Connection):
    """The DEFAULT 'display' satisfies NOT NULL when legacy SQL omits the column."""
    cur = conn.execute(
        "INSERT INTO test_results (measured_at, source) VALUES (?, ?)",
        ("2026-04-19T00:00:00.000Z", "test"),
    )
    row = conn.execute("SELECT tank FROM test_results WHERE id = ?", (cur.lastrowid,)).fetchone()
    assert row["tank"] == "display"


def test_v2_tank_indexes_exist(conn: sqlite3.Connection):
    """If the tank indexes are dropped from the migration, this test catches it."""
    indexes = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    }
    assert {"idx_test_results_tank", "idx_maintenance_events_tank"}.issubset(indexes)
