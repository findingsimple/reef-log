"""Tests for ops.py — round-trip every function through a real tmp DB."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

from reef_log import ops

FIXTURES = Path(__file__).parent / "fixtures" / "photos"

# ---------- helpers ----------


def _dt(*, days_ago: float = 0) -> datetime:
    return datetime.now(UTC) - timedelta(days=days_ago)


# ---------- add_test_session ----------


def test_add_test_session_round_trip(conn: sqlite3.Connection):
    test_id = ops.add_test_session(
        conn,
        [
            {"parameter": "alkalinity", "value": 8.2},
            {"parameter": "calcium", "value": 430, "checker_model": "HI758"},
        ],
        tank="display",
        notes="evening test",
    )
    row = conn.execute("SELECT * FROM test_results WHERE id = ?", (test_id,)).fetchone()
    assert row["notes"] == "evening test"
    assert row["source"] == "mcp"
    assert row["tz_assumed"] == 0
    assert row["tank"] == "display"

    measurements = conn.execute(
        "SELECT * FROM test_measurements WHERE test_result_id = ? ORDER BY id",
        (test_id,),
    ).fetchall()
    assert len(measurements) == 2
    assert measurements[0]["parameter"] == "alkalinity"
    assert measurements[0]["unit"] == "dKH"  # default
    assert measurements[1]["unit"] == "ppm"
    assert measurements[1]["checker_model"] == "HI758"


def test_add_test_session_rejects_empty_measurements(conn: sqlite3.Connection):
    with pytest.raises(ValueError, match="at least one measurement"):
        ops.add_test_session(conn, [], tank="display")


def test_add_test_session_unknown_parameter_requires_explicit_unit(conn: sqlite3.Connection):
    with pytest.raises(ValueError, match="no default unit"):
        ops.add_test_session(conn, [{"parameter": "salinity", "value": 1.025}], tank="display")

    test_id = ops.add_test_session(
        conn, [{"parameter": "salinity", "value": 1.025, "unit": "sg"}], tank="display"
    )
    m = conn.execute(
        "SELECT unit FROM test_measurements WHERE test_result_id = ?", (test_id,)
    ).fetchone()
    assert m["unit"] == "sg"


def test_add_test_session_with_explicit_datetime_and_tz_flag(conn: sqlite3.Connection):
    measured_at = datetime(2026, 4, 1, 10, 30, tzinfo=UTC)
    test_id = ops.add_test_session(
        conn,
        [{"parameter": "magnesium", "value": 1380}],
        tank="frag",
        measured_at=measured_at,
        tz_assumed=True,
        source="photo:deadbeef",
    )
    row = conn.execute("SELECT * FROM test_results WHERE id = ?", (test_id,)).fetchone()
    assert row["measured_at"].startswith("2026-04-01T10:30:00")
    assert row["tz_assumed"] == 1
    assert row["source"] == "photo:deadbeef"
    assert row["tank"] == "frag"


def test_add_test_session_rejects_naive_datetime(conn: sqlite3.Connection):
    with pytest.raises(ValueError, match="timezone-aware"):
        ops.add_test_session(
            conn,
            [{"parameter": "calcium", "value": 430}],
            tank="display",
            measured_at=datetime(2026, 4, 1, 10, 30),
        )


def test_add_test_session_persists_confidence(conn: sqlite3.Connection):
    test_id = ops.add_test_session(
        conn,
        [{"parameter": "phosphate", "value": 0.05, "confidence": 0.92}],
        tank="display",
        source="photo:abc",
    )
    m = conn.execute(
        "SELECT confidence FROM test_measurements WHERE test_result_id = ?", (test_id,)
    ).fetchone()
    assert m["confidence"] == pytest.approx(0.92)


def test_add_test_session_requires_tank_kwarg(conn: sqlite3.Connection):
    with pytest.raises(TypeError, match="tank"):
        ops.add_test_session(conn, [{"parameter": "alkalinity", "value": 8.2}])  # type: ignore[call-arg]


def test_add_test_session_rejects_unknown_tank(conn: sqlite3.Connection):
    with pytest.raises(ValueError, match="unknown tank"):
        ops.add_test_session(conn, [{"parameter": "alkalinity", "value": 8.2}], tank="dispaly")
    # And the bad write must not have created any rows.
    assert conn.execute("SELECT COUNT(*) FROM test_results").fetchone()[0] == 0


# ---------- add_maintenance ----------


def test_add_maintenance_round_trip(conn: sqlite3.Connection):
    mid = ops.add_maintenance(
        conn,
        "water_change",
        tank="display",
        details={"volume_liters": 50, "salt_brand": "Tropic Marin"},
        notes="weekly",
    )
    row = conn.execute("SELECT * FROM maintenance_events WHERE id = ?", (mid,)).fetchone()
    assert row["event_type"] == "water_change"
    assert row["notes"] == "weekly"
    assert row["tank"] == "display"
    import json

    assert json.loads(row["details"])["volume_liters"] == 50


def test_add_maintenance_no_details(conn: sqlite3.Connection):
    mid = ops.add_maintenance(conn, "observation", tank="frag", notes="cyano on the rocks")
    row = conn.execute("SELECT * FROM maintenance_events WHERE id = ?", (mid,)).fetchone()
    assert row["details"] is None
    assert row["tank"] == "frag"


def test_add_maintenance_requires_tank_kwarg(conn: sqlite3.Connection):
    with pytest.raises(TypeError, match="tank"):
        ops.add_maintenance(conn, "water_change")  # type: ignore[call-arg]


def test_add_maintenance_rejects_unknown_tank(conn: sqlite3.Connection):
    with pytest.raises(ValueError, match="unknown tank"):
        ops.add_maintenance(conn, "water_change", tank="reef")
    assert conn.execute("SELECT COUNT(*) FROM maintenance_events").fetchone()[0] == 0


def test_add_maintenance_accepts_shared_tank(conn: sqlite3.Connection):
    """Maintenance allows tank='both' for system-wide events (e.g. RO/DI swap)."""
    mid = ops.add_maintenance(
        conn, "filter_change", tank="both", details={"item": "RO/DI prefilter"}
    )
    row = conn.execute("SELECT tank FROM maintenance_events WHERE id = ?", (mid,)).fetchone()
    assert row["tank"] == "both"


def test_add_test_session_rejects_shared_tank(conn: sqlite3.Connection):
    """test_results never use 'both' — readings are always per-tank."""
    with pytest.raises(ValueError, match="unknown tank"):
        ops.add_test_session(conn, [{"parameter": "alkalinity", "value": 8.2}], tank="both")


# ---------- get_recent ----------


def test_get_recent_returns_unified_sorted_newest_first(conn: sqlite3.Connection):
    ops.add_test_session(
        conn,
        [{"parameter": "alkalinity", "value": 8.0}],
        tank="display",
        measured_at=_dt(days_ago=5),
    )
    ops.add_maintenance(conn, "water_change", tank="display", performed_at=_dt(days_ago=1))
    ops.add_test_session(
        conn,
        [{"parameter": "calcium", "value": 420}],
        tank="display",
        measured_at=_dt(days_ago=10),
    )

    recent = ops.get_recent(conn, days=30)
    assert len(recent) == 3
    assert recent[0]["kind"] == "maintenance"  # 1 day ago
    assert recent[1]["kind"] == "test"  # 5 days ago
    assert recent[2]["kind"] == "test"  # 10 days ago
    assert recent[0]["at"] > recent[1]["at"] > recent[2]["at"]


def test_get_recent_window_excludes_old_entries(conn: sqlite3.Connection):
    ops.add_test_session(
        conn,
        [{"parameter": "alkalinity", "value": 8.0}],
        tank="display",
        measured_at=_dt(days_ago=2),
    )
    ops.add_test_session(
        conn,
        [{"parameter": "alkalinity", "value": 8.5}],
        tank="display",
        measured_at=_dt(days_ago=60),
    )
    recent = ops.get_recent(conn, days=30)
    assert len(recent) == 1
    assert recent[0]["measurements"][0]["value"] == 8.0


def test_get_recent_filter_by_parameter_excludes_maintenance(conn: sqlite3.Connection):
    ops.add_test_session(
        conn,
        [{"parameter": "alkalinity", "value": 8.0}],
        tank="display",
        measured_at=_dt(days_ago=1),
    )
    ops.add_test_session(
        conn,
        [{"parameter": "calcium", "value": 430}],
        tank="display",
        measured_at=_dt(days_ago=2),
    )
    ops.add_maintenance(conn, "water_change", tank="display", performed_at=_dt(days_ago=1))

    recent = ops.get_recent(conn, parameter="alkalinity")
    assert len(recent) == 1
    assert recent[0]["kind"] == "test"
    assert recent[0]["measurements"][0]["parameter"] == "alkalinity"


def test_get_recent_filter_by_event_type_excludes_tests(conn: sqlite3.Connection):
    ops.add_test_session(
        conn,
        [{"parameter": "alkalinity", "value": 8.0}],
        tank="display",
        measured_at=_dt(days_ago=1),
    )
    ops.add_maintenance(conn, "water_change", tank="display", performed_at=_dt(days_ago=1))
    ops.add_maintenance(conn, "equipment_change", tank="display", performed_at=_dt(days_ago=2))

    recent = ops.get_recent(conn, event_type="water_change")
    assert len(recent) == 1
    assert recent[0]["kind"] == "maintenance"
    assert recent[0]["event_type"] == "water_change"


def test_get_recent_filter_by_tank(conn: sqlite3.Connection):
    # Asymmetric counts (2 display, 3 frag) so a missing tank predicate would
    # leak rows in either direction and fail the count assertion.
    ops.add_test_session(
        conn,
        [{"parameter": "alkalinity", "value": 8.0}],
        tank="display",
        measured_at=_dt(days_ago=1),
    )
    ops.add_maintenance(conn, "water_change", tank="display", performed_at=_dt(days_ago=2))
    ops.add_test_session(
        conn,
        [{"parameter": "alkalinity", "value": 8.5}],
        tank="frag",
        measured_at=_dt(days_ago=1),
    )
    ops.add_test_session(
        conn,
        [{"parameter": "alkalinity", "value": 8.6}],
        tank="frag",
        measured_at=_dt(days_ago=2),
    )
    ops.add_maintenance(conn, "water_change", tank="frag", performed_at=_dt(days_ago=3))

    display = ops.get_recent(conn, tank="display")
    assert len(display) == 2
    assert all(r["tank"] == "display" for r in display)
    # Specifically: the frag value (8.5/8.6) must not appear here.
    leaked_values = {m["value"] for r in display if r["kind"] == "test" for m in r["measurements"]}
    assert leaked_values == {8.0}

    frag = ops.get_recent(conn, tank="frag")
    assert len(frag) == 3
    assert all(r["tank"] == "frag" for r in frag)


def test_get_recent_combined_parameter_and_tank_filters(conn: sqlite3.Connection):
    ops.add_test_session(
        conn,
        [{"parameter": "alkalinity", "value": 8.0}],
        tank="display",
        measured_at=_dt(days_ago=1),
    )
    ops.add_test_session(
        conn,
        [{"parameter": "alkalinity", "value": 9.0}],
        tank="frag",
        measured_at=_dt(days_ago=1),
    )
    ops.add_test_session(
        conn,
        [{"parameter": "calcium", "value": 430}],
        tank="display",
        measured_at=_dt(days_ago=2),
    )

    rows = ops.get_recent(conn, parameter="alkalinity", tank="display")
    assert len(rows) == 1
    assert rows[0]["tank"] == "display"
    assert rows[0]["measurements"][0]["value"] == 8.0


def test_get_recent_no_tank_returns_all_tanks(conn: sqlite3.Connection):
    ops.add_test_session(
        conn,
        [{"parameter": "alkalinity", "value": 8.0}],
        tank="display",
        measured_at=_dt(days_ago=1),
    )
    ops.add_test_session(
        conn,
        [{"parameter": "alkalinity", "value": 8.5}],
        tank="frag",
        measured_at=_dt(days_ago=2),
    )

    recent = ops.get_recent(conn)
    assert len(recent) == 2
    assert {r["tank"] for r in recent} == {"display", "frag"}


def test_get_recent_empty(conn: sqlite3.Connection):
    assert ops.get_recent(conn) == []


# ---------- get_parameter_history ----------


def test_get_parameter_history_only_matching_parameter(conn: sqlite3.Connection):
    ops.add_test_session(
        conn,
        [
            {"parameter": "alkalinity", "value": 8.0},
            {"parameter": "calcium", "value": 420},
        ],
        tank="display",
        measured_at=_dt(days_ago=2),
    )
    ops.add_test_session(
        conn,
        [{"parameter": "alkalinity", "value": 8.3}],
        tank="display",
        measured_at=_dt(days_ago=1),
    )
    history = ops.get_parameter_history(conn, "alkalinity", tank="display", days=30)
    assert len(history) == 2
    assert [h["value"] for h in history] == [8.0, 8.3]  # ascending by time


def test_get_parameter_history_window(conn: sqlite3.Connection):
    ops.add_test_session(
        conn,
        [{"parameter": "magnesium", "value": 1300}],
        tank="display",
        measured_at=_dt(days_ago=120),
    )
    ops.add_test_session(
        conn,
        [{"parameter": "magnesium", "value": 1380}],
        tank="display",
        measured_at=_dt(days_ago=10),
    )
    assert len(ops.get_parameter_history(conn, "magnesium", tank="display", days=90)) == 1
    assert len(ops.get_parameter_history(conn, "magnesium", tank="display", days=180)) == 2


def test_get_parameter_history_scoped_per_tank(conn: sqlite3.Connection):
    ops.add_test_session(
        conn,
        [{"parameter": "alkalinity", "value": 8.0}],
        tank="display",
        measured_at=_dt(days_ago=1),
    )
    ops.add_test_session(
        conn,
        [{"parameter": "alkalinity", "value": 9.0}],
        tank="frag",
        measured_at=_dt(days_ago=1),
    )

    d = ops.get_parameter_history(conn, "alkalinity", tank="display", days=30)
    f = ops.get_parameter_history(conn, "alkalinity", tank="frag", days=30)
    assert [h["value"] for h in d] == [8.0]
    assert [h["value"] for h in f] == [9.0]


def test_get_parameter_history_requires_tank_kwarg(conn: sqlite3.Connection):
    with pytest.raises(TypeError, match="tank"):
        ops.get_parameter_history(conn, "alkalinity", days=30)  # type: ignore[call-arg]


def test_get_parameter_history_no_join_leak_at_same_timestamp(conn: sqlite3.Connection):
    """Same parameter + same timestamp in both tanks must not cross-pollinate."""
    same_time = datetime(2026, 4, 19, 10, 0, tzinfo=UTC)
    ops.add_test_session(
        conn,
        [{"parameter": "alkalinity", "value": 8.0}],
        tank="display",
        measured_at=same_time,
    )
    ops.add_test_session(
        conn,
        [{"parameter": "alkalinity", "value": 9.0}],
        tank="frag",
        measured_at=same_time,
    )

    d = ops.get_parameter_history(conn, "alkalinity", tank="display", days=365 * 5)
    f = ops.get_parameter_history(conn, "alkalinity", tank="frag", days=365 * 5)
    assert len(d) == 1 and d[0]["value"] == 8.0
    assert len(f) == 1 and f[0]["value"] == 9.0


# ---------- get_last_event ----------


def test_get_last_event_returns_most_recent(conn: sqlite3.Connection):
    ops.add_maintenance(
        conn,
        "equipment_change",
        tank="display",
        performed_at=_dt(days_ago=180),
        details={"equipment": "uv_bulb", "action": "replaced"},
    )
    ops.add_maintenance(
        conn,
        "equipment_change",
        tank="display",
        performed_at=_dt(days_ago=30),
        details={"equipment": "skimmer", "action": "cleaned"},
    )

    last = ops.get_last_event(conn, "equipment_change", tank="display")
    assert last is not None
    assert last["details"]["equipment"] == "skimmer"


def test_get_last_event_none_when_missing(conn: sqlite3.Connection):
    assert ops.get_last_event(conn, "water_change", tank="display") is None


def test_get_last_event_real_tank_query_includes_shared_events(conn: sqlite3.Connection):
    """Querying tank='display' must also surface tank='both' rows."""
    ops.add_maintenance(
        conn,
        "filter_change",
        tank="both",
        performed_at=_dt(days_ago=1),
        details={"item": "RO/DI prefilter"},
    )
    last = ops.get_last_event(conn, "filter_change", tank="display")
    assert last is not None
    assert last["tank"] == "both"
    assert last["details"]["item"] == "RO/DI prefilter"


def test_get_last_event_shared_tank_query_excludes_per_tank_events(conn: sqlite3.Connection):
    """tank='both' must return only shared events, not display- or frag-scoped ones."""
    ops.add_maintenance(conn, "water_change", tank="display", performed_at=_dt(days_ago=1))
    ops.add_maintenance(conn, "water_change", tank="frag", performed_at=_dt(days_ago=2))
    ops.add_maintenance(
        conn,
        "water_change",
        tank="both",
        performed_at=_dt(days_ago=5),
        details={"note": "shared top-off"},
    )

    shared = ops.get_last_event(conn, "water_change", tank="both")
    assert shared is not None
    assert shared["tank"] == "both"
    assert shared["details"]["note"] == "shared top-off"


def test_get_last_event_real_tank_prefers_most_recent_regardless_of_shared(
    conn: sqlite3.Connection,
):
    """When both a per-tank and a shared event exist, the newer wins."""
    ops.add_maintenance(conn, "water_change", tank="display", performed_at=_dt(days_ago=1))
    ops.add_maintenance(conn, "water_change", tank="both", performed_at=_dt(days_ago=10))

    last = ops.get_last_event(conn, "water_change", tank="display")
    assert last is not None
    assert last["tank"] == "display"  # 1 day ago beats 10 days ago


def test_get_last_event_typo_tank_returns_none_not_shared_leak(conn: sqlite3.Connection):
    """A typo must NOT accidentally surface SHARED_TANK rows."""
    ops.add_maintenance(conn, "water_change", tank="both", performed_at=_dt(days_ago=1))
    assert ops.get_last_event(conn, "water_change", tank="dispaly") is None


def test_get_recent_real_tank_includes_shared_maintenance(conn: sqlite3.Connection):
    ops.add_maintenance(conn, "filter_change", tank="both", performed_at=_dt(days_ago=1))
    ops.add_maintenance(conn, "water_change", tank="display", performed_at=_dt(days_ago=2))
    ops.add_maintenance(conn, "water_change", tank="frag", performed_at=_dt(days_ago=3))

    display = ops.get_recent(conn, tank="display")
    display_tanks = {r["tank"] for r in display}
    assert display_tanks == {"display", "both"}  # frag excluded, both included


def test_get_recent_shared_tank_query_excludes_per_tank(conn: sqlite3.Connection):
    ops.add_maintenance(conn, "water_change", tank="display", performed_at=_dt(days_ago=1))
    ops.add_maintenance(conn, "water_change", tank="both", performed_at=_dt(days_ago=2))

    rows = ops.get_recent(conn, tank="both")
    assert len(rows) == 1
    assert rows[0]["tank"] == "both"


def test_get_last_event_scoped_per_tank(conn: sqlite3.Connection):
    ops.add_maintenance(
        conn,
        "water_change",
        tank="display",
        performed_at=_dt(days_ago=5),
        details={"volume_liters": 50},
    )
    ops.add_maintenance(
        conn,
        "water_change",
        tank="frag",
        performed_at=_dt(days_ago=2),
        details={"volume_liters": 20},
    )

    d = ops.get_last_event(conn, "water_change", tank="display")
    f = ops.get_last_event(conn, "water_change", tank="frag")
    assert d is not None and d["details"]["volume_liters"] == 50
    assert d["tank"] == "display"
    assert f is not None and f["details"]["volume_liters"] == 20
    assert f["tank"] == "frag"


# ---------- analyze_trends ----------


def test_analyze_trends_known_stats(conn: sqlite3.Connection):
    # Hand-picked series so the math is checkable.
    values = [8.0, 8.2, 8.4, 8.6, 8.8]  # mean=8.4, stdev=sqrt(0.1)≈0.3162
    for i, v in enumerate(values):
        ops.add_test_session(
            conn,
            [{"parameter": "alkalinity", "value": v}],
            tank="display",
            measured_at=_dt(days_ago=10 - i),
        )

    result = ops.analyze_trends(conn, "alkalinity", tank="display", days=30)
    assert result["count"] == 5
    assert result["min"] == 8.0
    assert result["max"] == 8.8
    assert result["mean"] == pytest.approx(8.4)
    assert result["stdev"] == pytest.approx(math.sqrt(0.1), rel=1e-6)
    assert result["latest_value"] == 8.8
    assert result["tank"] == "display"
    assert "alkalinity" in result["summary"]
    assert "display" in result["summary"]


def test_analyze_trends_empty_window(conn: sqlite3.Connection):
    result = ops.analyze_trends(conn, "alkalinity", tank="display", days=30)
    assert result["count"] == 0
    assert "no measurements" in result["summary"]
    assert result["tank"] == "display"
    # critically, must not crash on stdev/min/max of empty list
    assert "min" not in result
    assert "stdev" not in result


def test_analyze_trends_single_measurement_zero_stdev(conn: sqlite3.Connection):
    ops.add_test_session(
        conn,
        [{"parameter": "magnesium", "value": 1380}],
        tank="display",
        measured_at=_dt(days_ago=1),
    )
    result = ops.analyze_trends(conn, "magnesium", tank="display", days=90)
    assert result["count"] == 1
    assert result["stdev"] == 0.0
    assert result["min"] == result["max"] == result["mean"] == 1380


def test_analyze_trends_excludes_other_parameters(conn: sqlite3.Connection):
    ops.add_test_session(
        conn,
        [
            {"parameter": "alkalinity", "value": 8.0},
            {"parameter": "calcium", "value": 420},
        ],
        tank="display",
        measured_at=_dt(days_ago=1),
    )
    result = ops.analyze_trends(conn, "alkalinity", tank="display", days=30)
    assert result["count"] == 1
    assert result["latest_value"] == 8.0


def test_analyze_trends_scoped_per_tank(conn: sqlite3.Connection):
    ops.add_test_session(
        conn,
        [{"parameter": "alkalinity", "value": 8.0}],
        tank="display",
        measured_at=_dt(days_ago=1),
    )
    ops.add_test_session(
        conn,
        [{"parameter": "alkalinity", "value": 9.5}],
        tank="frag",
        measured_at=_dt(days_ago=1),
    )

    d = ops.analyze_trends(conn, "alkalinity", tank="display", days=30)
    f = ops.analyze_trends(conn, "alkalinity", tank="frag", days=30)
    assert d["latest_value"] == 8.0
    assert f["latest_value"] == 9.5


# ---------- compare_trends ----------


def test_compare_trends_returns_both_tanks(conn: sqlite3.Connection):
    # Distinguishable values, units, AND timestamps per tank so a swap or
    # shared-state bug between the two analyze_trends calls would surface.
    display_time = datetime(2026, 4, 18, 10, 0, tzinfo=UTC)
    frag_time = datetime(2026, 4, 19, 14, 30, tzinfo=UTC)
    ops.add_test_session(
        conn,
        [{"parameter": "alkalinity", "value": 8.0}],
        tank="display",
        measured_at=display_time - timedelta(days=1),
    )
    ops.add_test_session(
        conn,
        [{"parameter": "alkalinity", "value": 8.2, "unit": "dKH"}],
        tank="display",
        measured_at=display_time,
    )
    ops.add_test_session(
        conn,
        [{"parameter": "alkalinity", "value": 9.0, "unit": "meq/L"}],
        tank="frag",
        measured_at=frag_time,
    )

    result = ops.compare_trends(conn, "alkalinity", days=365 * 5)
    assert result["parameter"] == "alkalinity"
    assert set(result["tanks"].keys()) == {"display", "frag"}

    d = result["tanks"]["display"]
    f = result["tanks"]["frag"]
    assert d["count"] == 2
    assert f["count"] == 1
    assert d["mean"] == pytest.approx(8.1)
    assert f["latest_value"] == 9.0
    # Distinguishable units must not be swapped between tanks.
    assert d["unit"] == "dKH"
    assert f["unit"] == "meq/L"
    # Distinguishable timestamps must not be swapped.
    assert d["latest_at"].startswith("2026-04-18")
    assert f["latest_at"].startswith("2026-04-19")
    assert "display" in result["summary"]
    assert "frag" in result["summary"]


def test_compare_trends_summary_mentions_both_tanks_when_one_empty(
    conn: sqlite3.Connection,
):
    ops.add_test_session(
        conn,
        [{"parameter": "alkalinity", "value": 8.0}],
        tank="display",
        measured_at=_dt(days_ago=1),
    )

    result = ops.compare_trends(conn, "alkalinity", days=30)
    # The non-empty tank must still appear in the summary alongside the empty one.
    assert "display mean" in result["summary"]
    assert "frag no data" in result["summary"]


def test_compare_trends_handles_empty_tank(conn: sqlite3.Connection):
    ops.add_test_session(
        conn,
        [{"parameter": "alkalinity", "value": 8.0}],
        tank="display",
        measured_at=_dt(days_ago=1),
    )

    result = ops.compare_trends(conn, "alkalinity", days=30)
    assert result["tanks"]["display"]["count"] == 1
    assert result["tanks"]["frag"]["count"] == 0
    assert "frag no data" in result["summary"]


def test_compare_trends_both_empty(conn: sqlite3.Connection):
    result = ops.compare_trends(conn, "alkalinity", days=30)
    assert result["tanks"]["display"]["count"] == 0
    assert result["tanks"]["frag"]["count"] == 0
    assert "display no data" in result["summary"]


# ---------- log_test_from_photo / is_photo_processed ----------


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def test_is_photo_processed_false_before_true_after(conn: sqlite3.Connection):
    photo = FIXTURES / "HI758_calcium_display.jpg"
    other = FIXTURES / "HI758_calcium_frag.jpg"
    sha = _sha256_of(photo)
    other_sha = _sha256_of(other)
    assert ops.is_photo_processed(conn, sha) is False

    ops.log_test_from_photo(
        conn,
        path=photo,
        tank="display",
        measurements=[{"parameter": "calcium", "value": 446, "checker_model": "HI758"}],
    )
    assert ops.is_photo_processed(conn, sha) is True
    # Must key off the specific sha, not "any row exists in processed_photos".
    assert ops.is_photo_processed(conn, other_sha) is False


def test_log_test_from_photo_happy_path(conn: sqlite3.Connection):
    photo = FIXTURES / "HI758_calcium_display.jpg"
    known_sha = _sha256_of(photo)
    result = ops.log_test_from_photo(
        conn,
        path=photo,
        tank="display",
        measurements=[{"parameter": "calcium", "value": 446, "checker_model": "HI758"}],
        notes="session 1",
    )

    assert result["test_result_id"] >= 1
    # Pin to the known sha so a bug that hashes the wrong bytes would fail.
    assert result["sha256"] == known_sha
    # iPhone fixture has OffsetTimeOriginal=+09:30 → tz not assumed.
    assert result["tz_assumed"] is False
    # EXIF says 2026:04:06 15:46:45 +09:30 → UTC 2026-04-06 06:16:45.
    assert result["measured_at"].startswith("2026-04-06T06:16:45")

    row = conn.execute(
        "SELECT * FROM test_results WHERE id = ?", (result["test_result_id"],)
    ).fetchone()
    assert row["tank"] == "display"
    # tz_assumed must round-trip through the DB correctly (bool→int→bool).
    assert row["tz_assumed"] == 0
    assert row["source"].startswith("photo:")
    assert known_sha in row["source"]
    assert row["notes"] == "session 1"

    pp = conn.execute(
        "SELECT * FROM processed_photos WHERE sha256 = ?", (result["sha256"],)
    ).fetchone()
    assert pp["test_result_id"] == result["test_result_id"]
    assert pp["status"] == "committed"
    assert pp["tank"] == "display"
    payload = json.loads(pp["extracted_payload"])
    assert payload["measurements"][0]["value"] == 446
    assert payload["notes"] == "session 1"


def test_log_test_from_photo_duplicate_rejected(conn: sqlite3.Connection):
    photo = FIXTURES / "HI758_calcium_display.jpg"
    ops.log_test_from_photo(
        conn,
        path=photo,
        tank="display",
        measurements=[{"parameter": "calcium", "value": 446}],
    )

    # The typed exception lets the MCP wrapper dispatch without a fragile
    # substring match on the message.
    with pytest.raises(ops.AlreadyProcessed) as exc_info:
        ops.log_test_from_photo(
            conn,
            path=photo,
            tank="display",
            measurements=[{"parameter": "calcium", "value": 446}],
        )
    assert exc_info.value.sha256 == _sha256_of(photo)

    # Ensure no double-write happened.
    (count,) = conn.execute("SELECT COUNT(*) FROM test_results").fetchone()
    assert count == 1


def test_log_test_from_photo_invalid_tank_fires_before_file_io(
    conn: sqlite3.Connection, tmp_path: Path
):
    """Tank validation must precede any file I/O.

    Pass a nonexistent path alongside the bad tank: we should see
    InvalidTank (a ValueError subclass), not FileNotFoundError. That pins
    the ordering invariant and prevents a refactor from silently checking
    the file first (which would be a wasted hash on invalid input).
    """
    with pytest.raises(ops.InvalidTank, match="unknown tank"):
        ops.log_test_from_photo(
            conn,
            path=tmp_path / "does-not-exist.jpg",
            tank="dispaly",  # typo
            measurements=[{"parameter": "calcium", "value": 446}],
        )


def test_log_test_from_photo_both_tank_rejected_before_file_io(
    conn: sqlite3.Connection, tmp_path: Path
):
    """'both' is for maintenance only — test sessions are always per-tank.

    Same ordering invariant: tank check fires before file I/O.
    """
    with pytest.raises(ops.InvalidTank, match="unknown tank"):
        ops.log_test_from_photo(
            conn,
            path=tmp_path / "does-not-exist.jpg",
            tank="both",
            measurements=[{"parameter": "calcium", "value": 446}],
        )


def test_log_test_from_photo_missing_file(conn: sqlite3.Connection, tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        ops.log_test_from_photo(
            conn,
            path=tmp_path / "nonexistent.jpg",
            tank="display",
            measurements=[{"parameter": "calcium", "value": 446}],
        )


def test_log_test_from_photo_no_exif_falls_back_to_mtime(conn: sqlite3.Connection, tmp_path: Path):
    # JPG without any EXIF data — measured_at should fall back to mtime,
    # and tz_assumed should be True.
    photo = tmp_path / "no_exif.jpg"
    Image.new("RGB", (10, 10), color="red").save(photo, "JPEG")

    result = ops.log_test_from_photo(
        conn,
        path=photo,
        tank="frag",
        measurements=[{"parameter": "calcium", "value": 430}],
    )
    assert result["tz_assumed"] is True

    mtime = datetime.fromtimestamp(photo.stat().st_mtime, UTC)
    stored = datetime.fromisoformat(result["measured_at"].replace("Z", "+00:00"))
    # Allow sub-second drift from the mtime.
    assert abs((stored - mtime).total_seconds()) < 1


def test_log_test_from_photo_two_different_photos_log_separately(
    conn: sqlite3.Connection,
):
    r1 = ops.log_test_from_photo(
        conn,
        path=FIXTURES / "HI758_calcium_display.jpg",
        tank="display",
        measurements=[{"parameter": "calcium", "value": 446}],
    )
    r2 = ops.log_test_from_photo(
        conn,
        path=FIXTURES / "HI758_calcium_frag.jpg",
        tank="frag",
        measurements=[{"parameter": "calcium", "value": 502}],
    )
    assert r1["sha256"] != r2["sha256"]
    assert r1["test_result_id"] != r2["test_result_id"]
    shas = {row["sha256"] for row in conn.execute("SELECT sha256 FROM processed_photos").fetchall()}
    assert shas == {r1["sha256"], r2["sha256"]}


def _strip_exif_offset(src: Path, dst: Path) -> None:
    """Copy `src` to `dst` with OffsetTimeOriginal removed from EXIF.

    Produces a JPG that has DateTimeOriginal but no tz info — the naive
    EXIF case that plan-decision #2 is specifically about.
    """
    from PIL import ExifTags

    with Image.open(src) as img:
        exif = img.getexif()
        exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
        exif_ifd.pop(ExifTags.Base.OffsetTimeOriginal.value, None)
        exif_ifd.pop(ExifTags.Base.OffsetTime.value, None)
        exif_ifd.pop(ExifTags.Base.OffsetTimeDigitized.value, None)
        img.save(dst, "JPEG", exif=exif)


def test_log_test_from_photo_naive_exif_sets_tz_assumed(conn: sqlite3.Connection, tmp_path: Path):
    """DateTimeOriginal present but no OffsetTimeOriginal → tz_assumed=True.

    Covers plan-decision #2: naive EXIF is interpreted as laptop local tz
    and stored as UTC, with tz_assumed=1 on the row.
    """
    photo = tmp_path / "naive_exif.jpg"
    _strip_exif_offset(FIXTURES / "HI758_calcium_display.jpg", photo)

    result = ops.log_test_from_photo(
        conn,
        path=photo,
        tank="display",
        measurements=[{"parameter": "calcium", "value": 446}],
    )
    assert result["tz_assumed"] is True

    row = conn.execute(
        "SELECT tz_assumed FROM test_results WHERE id = ?", (result["test_result_id"],)
    ).fetchone()
    # Also pin the bool→int DB round-trip specifically for the assumed path.
    assert row["tz_assumed"] == 1


def test_log_test_from_photo_mtime_fallback_round_trips_tz_assumed_to_db(
    conn: sqlite3.Connection, tmp_path: Path
):
    """tz_assumed=True from mtime fallback must persist as 1 in test_results."""
    photo = tmp_path / "no_exif.jpg"
    Image.new("RGB", (10, 10), color="red").save(photo, "JPEG")

    result = ops.log_test_from_photo(
        conn,
        path=photo,
        tank="frag",
        measurements=[{"parameter": "calcium", "value": 430}],
    )
    assert result["tz_assumed"] is True

    row = conn.execute(
        "SELECT tz_assumed FROM test_results WHERE id = ?", (result["test_result_id"],)
    ).fetchone()
    assert row["tz_assumed"] == 1


def test_log_test_from_photo_rejects_zero_byte_file(conn: sqlite3.Connection, tmp_path: Path):
    empty = tmp_path / "empty.jpg"
    empty.touch()
    with pytest.raises(ValueError, match="empty"):
        ops.log_test_from_photo(
            conn,
            path=empty,
            tank="display",
            measurements=[{"parameter": "calcium", "value": 446}],
        )


def test_log_test_from_photo_rejects_oversized_file(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Shrink the cap so we don't have to write 50+ MiB of test data.
    monkeypatch.setattr(ops, "MAX_PHOTO_BYTES", 1024)

    photo = tmp_path / "big.jpg"
    photo.write_bytes(b"x" * 2048)
    with pytest.raises(ValueError, match="too large"):
        ops.log_test_from_photo(
            conn,
            path=photo,
            tank="display",
            measurements=[{"parameter": "calcium", "value": 446}],
        )


def test_max_photo_bytes_is_pinned_to_50_mib():
    """The monkeypatched oversized test doesn't prove the real cap. Pin it."""
    assert ops.MAX_PHOTO_BYTES == 50 * 1024 * 1024


# ---------- log_test_from_photo — atomicity ----------


def test_log_test_from_photo_rollback_on_photo_insert_failure(
    conn: sqlite3.Connection, tmp_path: Path
):
    """Force a mid-transaction failure by poisoning the processed_photos
    table with a BEFORE-INSERT trigger that always aborts. If the transaction
    wrap works, the test_results + test_measurements rows must also vanish.
    """
    conn.execute(
        "CREATE TRIGGER fail_photo BEFORE INSERT ON processed_photos "
        "BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"
    )
    try:
        with pytest.raises(sqlite3.IntegrityError, match="simulated failure"):
            ops.log_test_from_photo(
                conn,
                path=FIXTURES / "HI758_calcium_display.jpg",
                tank="display",
                measurements=[{"parameter": "calcium", "value": 446}],
            )

        # Every write from the aborted transaction must be gone.
        (tr_count,) = conn.execute("SELECT COUNT(*) FROM test_results").fetchone()
        (tm_count,) = conn.execute("SELECT COUNT(*) FROM test_measurements").fetchone()
        (pp_count,) = conn.execute("SELECT COUNT(*) FROM processed_photos").fetchone()
        assert tr_count == 0
        assert tm_count == 0
        assert pp_count == 0
    finally:
        conn.execute("DROP TRIGGER fail_photo")


def test_log_test_from_photo_integrity_error_on_race(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
):
    """Simulate the SHA race: is_photo_processed returns False, but the
    INSERT hits the UNIQUE constraint because another caller beat us to it.
    ops raises the raw IntegrityError (no typed wrapper); the MCP layer
    translates it to `already_processed`.
    """
    photo = FIXTURES / "HI758_calcium_display.jpg"

    # Seed the dedup row first so the INSERT will collide.
    ops.log_test_from_photo(
        conn,
        path=photo,
        tank="display",
        measurements=[{"parameter": "calcium", "value": 446}],
    )

    # Force is_photo_processed to lie so we bypass the pre-check.
    monkeypatch.setattr(ops, "is_photo_processed", lambda conn, sha: False)

    with pytest.raises(sqlite3.IntegrityError):
        ops.log_test_from_photo(
            conn,
            path=photo,
            tank="display",
            measurements=[{"parameter": "calcium", "value": 446}],
        )

    # Second attempt rolled back — row count unchanged from the first call.
    (tr_count,) = conn.execute("SELECT COUNT(*) FROM test_results").fetchone()
    assert tr_count == 1


# ---------- _parse_exif_offset + more EXIF branches ----------


def test_parse_exif_offset_happy_paths():
    from datetime import timedelta as _td

    assert ops._parse_exif_offset("+09:30").utcoffset(None) == _td(hours=9, minutes=30)
    assert ops._parse_exif_offset("-05:00").utcoffset(None) == _td(hours=-5)
    assert ops._parse_exif_offset("+00:00").utcoffset(None) == _td(0)


def test_parse_exif_offset_malformed_raises():
    # Short, no sign, no colon, empty — each should raise rather than silently
    # inventing a timezone.
    for bad in ("", "Z", "+5", "+0930", "garbage", "0:00"):
        with pytest.raises(ValueError):
            ops._parse_exif_offset(bad)


def _write_exif_jpg(
    path: Path,
    dt_str: str | None,
    offset_str: str | None = None,
) -> None:
    """Write a tiny JPG with the given EXIF DateTimeOriginal/OffsetTimeOriginal."""
    from PIL import ExifTags

    img = Image.new("RGB", (10, 10), color="red")
    exif = img.getexif()
    exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
    if dt_str is not None:
        exif_ifd[ExifTags.Base.DateTimeOriginal.value] = dt_str
    if offset_str is not None:
        exif_ifd[ExifTags.Base.OffsetTimeOriginal.value] = offset_str
    img.save(path, "JPEG", exif=exif)


def test_log_test_from_photo_malformed_datetime_falls_back_to_mtime(
    conn: sqlite3.Connection, tmp_path: Path
):
    photo = tmp_path / "bad_dt.jpg"
    _write_exif_jpg(photo, dt_str="0000:00:00 00:00:00")

    result = ops.log_test_from_photo(
        conn,
        path=photo,
        tank="display",
        measurements=[{"parameter": "calcium", "value": 446}],
    )
    assert result["tz_assumed"] is True
    mtime = datetime.fromtimestamp(photo.stat().st_mtime, UTC)
    stored = datetime.fromisoformat(result["measured_at"].replace("Z", "+00:00"))
    assert abs((stored - mtime).total_seconds()) < 1


def test_log_test_from_photo_out_of_window_datetime_falls_back_to_mtime(
    conn: sqlite3.Connection, tmp_path: Path
):
    """EXIF saying 1970 (unset camera clock) should be discarded, not stored."""
    photo = tmp_path / "epoch.jpg"
    _write_exif_jpg(photo, dt_str="1970:01:01 00:00:00", offset_str="+00:00")

    result = ops.log_test_from_photo(
        conn,
        path=photo,
        tank="display",
        measurements=[{"parameter": "calcium", "value": 446}],
    )
    assert result["tz_assumed"] is True
    # Should be mtime, which is "now-ish" — definitely not 1970.
    stored = datetime.fromisoformat(result["measured_at"].replace("Z", "+00:00"))
    assert stored.year >= ops.MIN_EXIF_YEAR


def test_log_test_from_photo_malformed_offset_raises(conn: sqlite3.Connection, tmp_path: Path):
    """A present-but-malformed OffsetTimeOriginal must raise rather than fall
    back silently — EXIF claiming an offset with garbage is a real data problem.
    """
    photo = tmp_path / "bad_offset.jpg"
    _write_exif_jpg(photo, dt_str="2026:04:06 15:46:45", offset_str="garbage")

    with pytest.raises(ValueError, match="malformed EXIF offset"):
        ops.log_test_from_photo(
            conn,
            path=photo,
            tank="display",
            measurements=[{"parameter": "calcium", "value": 446}],
        )


def test_log_test_from_photo_naive_exif_stores_expected_utc_value(
    conn: sqlite3.Connection, tmp_path: Path
):
    """Beyond 'tz_assumed=True', verify the naive EXIF was actually treated
    as laptop local and converted to UTC (not stored as if already UTC).
    """
    photo = tmp_path / "naive.jpg"
    _strip_exif_offset(FIXTURES / "HI758_calcium_display.jpg", photo)

    result = ops.log_test_from_photo(
        conn,
        path=photo,
        tank="display",
        measurements=[{"parameter": "calcium", "value": 446}],
    )

    # Reconstruct the expected UTC: treat "2026-04-06 15:46:45" as laptop
    # local, astimezone(UTC).
    expected = datetime(2026, 4, 6, 15, 46, 45).astimezone().astimezone(UTC)
    stored = datetime.fromisoformat(result["measured_at"].replace("Z", "+00:00"))
    assert stored == expected
