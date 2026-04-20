"""Tests for the MCP server: tools are registered + dispatch correctly to ops.

Strategy:
- Verify each expected tool is registered with FastMCP (catches name typos
  and missing decorators).
- Call each tool function directly against a real tmp DB (the wrappers are
  thin enough that real-DB integration tests cost nothing extra and catch
  argument-mapping bugs that mocked-ops tests wouldn't).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reef_log import mcp_server

EXPECTED_TOOLS = {
    "log_test",
    "log_maintenance",
    "get_recent",
    "get_parameter_history",
    "get_last_event",
    "analyze_trends",
    "compare_trends",
    "log_test_from_photo",
}

FIXTURES = Path(__file__).parent / "fixtures" / "photos"


@pytest.fixture(autouse=True)
def _redirect_db(db_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mcp_server, "_db_path_override", db_path)


def test_all_expected_tools_registered():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {t.name for t in tools}
    assert EXPECTED_TOOLS.issubset(names), f"missing: {EXPECTED_TOOLS - names}"


def test_log_test_dispatches_and_returns_id():
    result = mcp_server.log_test(
        tank="display",
        measurements=[
            {"parameter": "alkalinity", "value": 8.2},
            {"parameter": "calcium", "value": 430},
        ],
        notes="evening",
    )
    assert result["id"] == 1
    assert result["tank"] == "display"
    assert "alkalinity=8.2" in result["logged"]


def test_log_maintenance_dispatches_and_returns_id():
    result = mcp_server.log_maintenance(
        tank="display",
        event_type="water_change",
        details={"volume_liters": 50},
        notes="weekly",
    )
    assert result["id"] == 1
    assert result["tank"] == "display"
    assert result["event_type"] == "water_change"


def test_log_maintenance_accepts_shared_tank():
    result = mcp_server.log_maintenance(
        tank="both",
        event_type="filter_change",
        details={"item": "RO/DI prefilter"},
    )
    assert result["tank"] == "both"


def test_get_recent_returns_unified_list():
    mcp_server.log_test(tank="display", measurements=[{"parameter": "alkalinity", "value": 8.2}])
    mcp_server.log_maintenance(tank="display", event_type="water_change")
    rows = mcp_server.get_recent(days=7)
    assert len(rows) == 2
    assert {r["kind"] for r in rows} == {"test", "maintenance"}


def test_get_recent_filter_by_parameter():
    mcp_server.log_test(tank="display", measurements=[{"parameter": "alkalinity", "value": 8.2}])
    mcp_server.log_test(tank="display", measurements=[{"parameter": "calcium", "value": 430}])
    mcp_server.log_maintenance(tank="display", event_type="water_change")
    rows = mcp_server.get_recent(parameter="alkalinity")
    assert len(rows) == 1
    assert rows[0]["measurements"][0]["parameter"] == "alkalinity"


def test_get_recent_filter_by_tank():
    mcp_server.log_test(tank="display", measurements=[{"parameter": "alkalinity", "value": 8.0}])
    mcp_server.log_test(tank="frag", measurements=[{"parameter": "alkalinity", "value": 9.0}])

    display = mcp_server.get_recent(tank="display")
    assert len(display) == 1
    assert display[0]["tank"] == "display"


def test_get_recent_real_tank_includes_shared_maintenance():
    """tank='display' surfaces 'both' maintenance rows too — read-path expansion."""
    mcp_server.log_maintenance(tank="both", event_type="filter_change")
    mcp_server.log_maintenance(tank="frag", event_type="water_change")

    display = mcp_server.get_recent(tank="display")
    assert {r["tank"] for r in display} == {"both"}  # frag excluded, both included


def test_get_parameter_history_dispatches():
    mcp_server.log_test(tank="display", measurements=[{"parameter": "magnesium", "value": 1380}])
    history = mcp_server.get_parameter_history("magnesium", tank="display", days=30)
    assert len(history) == 1
    assert history[0]["value"] == 1380


def test_get_last_event_dispatches():
    mcp_server.log_maintenance(
        tank="display",
        event_type="equipment_change",
        details={"equipment": "uv_bulb", "action": "replaced"},
    )
    last = mcp_server.get_last_event("equipment_change", tank="display")
    assert last is not None
    assert last["details"]["equipment"] == "uv_bulb"


def test_get_last_event_returns_none_when_empty():
    assert mcp_server.get_last_event("water_change", tank="display") is None


def test_get_last_event_real_tank_includes_shared():
    mcp_server.log_maintenance(tank="both", event_type="filter_change")
    last = mcp_server.get_last_event("filter_change", tank="frag")
    assert last is not None
    assert last["tank"] == "both"


def test_analyze_trends_dispatches():
    for v in [8.0, 8.2, 8.4]:
        mcp_server.log_test(tank="display", measurements=[{"parameter": "alkalinity", "value": v}])
    result = mcp_server.analyze_trends("alkalinity", tank="display", days=30)
    assert result["count"] == 3
    assert result["min"] == 8.0
    assert result["max"] == 8.4
    assert "alkalinity" in result["summary"]


def test_compare_trends_dispatches():
    mcp_server.log_test(tank="display", measurements=[{"parameter": "alkalinity", "value": 8.0}])
    mcp_server.log_test(tank="display", measurements=[{"parameter": "alkalinity", "value": 8.4}])
    mcp_server.log_test(tank="frag", measurements=[{"parameter": "alkalinity", "value": 9.0}])

    result = mcp_server.compare_trends("alkalinity", days=30)
    assert set(result["tanks"].keys()) == {"display", "frag"}
    assert result["tanks"]["display"]["count"] == 2
    assert result["tanks"]["frag"]["count"] == 1


def test_log_test_passes_measured_at_through():
    mcp_server.log_test(
        tank="display",
        measurements=[{"parameter": "calcium", "value": 430}],
        measured_at="2026-04-01T10:30:00.000Z",
    )
    history = mcp_server.get_parameter_history("calcium", tank="display", days=365)
    assert any(h["at"].startswith("2026-04-01") for h in history)


def test_log_test_from_photo_dispatches_and_returns_sha():
    import hashlib as _h

    photo = FIXTURES / "HI758_calcium_display.jpg"
    known_sha = _h.sha256(photo.read_bytes()).hexdigest()
    result = mcp_server.log_test_from_photo(
        path=str(photo),
        tank="display",
        measurements=[{"parameter": "calcium", "value": 446, "checker_model": "HI758"}],
    )
    assert "error" not in result
    assert result["test_result_id"] >= 1
    assert result["sha256"] == known_sha  # not just "64 chars long"
    # ISO-8601 UTC shape; parsing EXIF correctness is covered in test_ops.py.
    assert result["measured_at"].endswith("Z")
    assert result["tz_assumed"] is False


def test_log_test_from_photo_duplicate_returns_structured_error():
    photo = FIXTURES / "HI758_calcium_display.jpg"
    mcp_server.log_test_from_photo(
        path=str(photo),
        tank="display",
        measurements=[{"parameter": "calcium", "value": 446}],
    )
    result = mcp_server.log_test_from_photo(
        path=str(photo),
        tank="display",
        measurements=[{"parameter": "calcium", "value": 446}],
    )
    assert result["error"] == "already_processed"
    assert "already processed" in result["message"]
    # Also prove the dedup path didn't double-write (would catch an impl
    # that just hardcoded the error string and kept writing).
    from reef_log import db as _db

    conn = _db.connect(mcp_server._db_path_override)
    try:
        (n,) = conn.execute("SELECT COUNT(*) FROM test_results").fetchone()
    finally:
        conn.close()
    assert n == 1


def test_log_test_from_photo_missing_file_returns_structured_error(tmp_path: Path):
    result = mcp_server.log_test_from_photo(
        path=str(tmp_path / "nope.jpg"),
        tank="display",
        measurements=[{"parameter": "calcium", "value": 446}],
    )
    assert result["error"] == "not_found"


def test_log_test_from_photo_non_image_returns_structured_error(tmp_path: Path):
    fake = tmp_path / "not-really-a-photo.jpg"
    fake.write_text("this is plain text, not a JPG")
    result = mcp_server.log_test_from_photo(
        path=str(fake),
        tank="display",
        measurements=[{"parameter": "calcium", "value": 446}],
    )
    assert result["error"] == "not_an_image"


def test_log_test_from_photo_empty_file_returns_structured_error(tmp_path: Path):
    empty = tmp_path / "empty.jpg"
    empty.touch()
    result = mcp_server.log_test_from_photo(
        path=str(empty),
        tank="display",
        measurements=[{"parameter": "calcium", "value": 446}],
    )
    assert result["error"] == "invalid_photo"
    assert "empty" in result["message"]


def test_log_test_from_photo_invalid_tank_returns_structured_error(tmp_path: Path):
    """Tank typo should surface as `invalid_tank`, not lumped into `invalid_photo`."""
    result = mcp_server.log_test_from_photo(
        path=str(FIXTURES / "HI758_calcium_display.jpg"),
        tank="dispaly",  # typo
        measurements=[{"parameter": "calcium", "value": 446}],
    )
    assert result["error"] == "invalid_tank"
    assert "unknown tank" in result["message"]


def test_log_test_from_photo_heic_returns_not_an_image():
    """HEIC is documented as unsupported (no pillow-heif dep). Pin the
    contract so a transitive dep change doesn't silently flip behaviour.
    """
    heic = FIXTURES / "IMG_0752.HEIC"
    result = mcp_server.log_test_from_photo(
        path=str(heic),
        tank="display",
        measurements=[{"parameter": "calcium", "value": 446}],
    )
    assert result["error"] == "not_an_image"
