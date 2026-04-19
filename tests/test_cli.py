"""Smoke tests for the reef-log CLI via click's CliRunner."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from reef_log.cli import main


def _run(args: list[str], db_path: Path) -> tuple[int, str]:
    runner = CliRunner()
    result = runner.invoke(main, ["--db", str(db_path), *args], catch_exceptions=False)
    return result.exit_code, result.output


def test_test_add_logs_session(db_path: Path):
    code, out = _run(
        [
            "test",
            "add",
            "--tank",
            "display",
            "--alkalinity",
            "8.2",
            "--calcium",
            "430",
            "--notes",
            "evening",
        ],
        db_path,
    )
    assert code == 0
    assert "Logged test #1" in out
    assert "(display)" in out
    assert "alkalinity=8.2" in out
    assert "calcium=430" in out


def test_test_add_requires_tank(db_path: Path):
    runner = CliRunner()
    result = runner.invoke(
        main, ["--db", str(db_path), "test", "add", "--alkalinity", "8.2"]
    )
    assert result.exit_code != 0
    assert "--tank" in result.output


def test_test_add_rejects_unknown_tank(db_path: Path):
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--db", str(db_path), "test", "add", "--tank", "reef", "--alkalinity", "8.2"],
    )
    assert result.exit_code != 0
    # Click's Choice error mentions the invalid value.
    assert "reef" in result.output


def test_test_add_rejects_shared_tank(db_path: Path):
    """test_results never use 'both' — readings are always per-tank."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--db", str(db_path), "test", "add", "--tank", "both", "--alkalinity", "8.2"],
    )
    assert result.exit_code != 0


def test_test_add_requires_at_least_one_parameter(db_path: Path):
    runner = CliRunner()
    result = runner.invoke(
        main, ["--db", str(db_path), "test", "add", "--tank", "display"]
    )
    assert result.exit_code != 0
    assert "at least one parameter flag" in result.output


def test_maintenance_add_with_details(db_path: Path):
    code, out = _run(
        [
            "maintenance",
            "add",
            "water_change",
            "--tank",
            "display",
            "--detail",
            "volume_liters=50",
            "--detail",
            "salt_brand=Tropic Marin",
            "--notes",
            "weekly",
        ],
        db_path,
    )
    assert code == 0
    assert "Logged maintenance #1" in out
    assert "(display)" in out
    assert "water_change" in out


def test_maintenance_add_accepts_shared_tank(db_path: Path):
    code, out = _run(
        [
            "maintenance",
            "add",
            "filter_change",
            "--tank",
            "both",
            "--detail",
            "item=RO/DI prefilter",
        ],
        db_path,
    )
    assert code == 0
    assert "(both)" in out


def test_maintenance_add_no_details(db_path: Path):
    code, out = _run(
        [
            "maintenance",
            "add",
            "observation",
            "--tank",
            "frag",
            "--notes",
            "cyano",
        ],
        db_path,
    )
    assert code == 0
    assert "observation" in out
    assert "(frag)" in out


def test_maintenance_add_rejects_malformed_detail(db_path: Path):
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--db",
            str(db_path),
            "maintenance",
            "add",
            "water_change",
            "--tank",
            "display",
            "--detail",
            "broken",
        ],
    )
    assert result.exit_code != 0
    assert "KEY=VALUE" in result.output


def test_history_empty_db(db_path: Path):
    code, out = _run(["history"], db_path)
    assert code == 0
    assert "No activity" in out


def test_history_shows_logged_entries(db_path: Path):
    _run(["test", "add", "--tank", "display", "--alkalinity", "8.2"], db_path)
    _run(
        [
            "maintenance",
            "add",
            "water_change",
            "--tank",
            "display",
            "--detail",
            "volume_liters=50",
        ],
        db_path,
    )

    code, out = _run(["history", "--days", "7"], db_path)
    assert code == 0
    assert "TEST" in out
    assert "alkalinity=8.2dKH" in out
    assert "WATER_CHANGE" in out
    assert "[display]" in out


def test_history_filter_by_parameter(db_path: Path):
    _run(["test", "add", "--tank", "display", "--alkalinity", "8.2"], db_path)
    _run(["test", "add", "--tank", "display", "--calcium", "430"], db_path)
    _run(["maintenance", "add", "water_change", "--tank", "display"], db_path)

    code, out = _run(["history", "--parameter", "alkalinity"], db_path)
    assert code == 0
    assert "alkalinity=8.2dKH" in out
    assert "calcium" not in out
    assert "WATER_CHANGE" not in out


def test_history_filter_by_event_type(db_path: Path):
    _run(["test", "add", "--tank", "display", "--alkalinity", "8.2"], db_path)
    _run(["maintenance", "add", "water_change", "--tank", "display"], db_path)
    _run(["maintenance", "add", "equipment_change", "--tank", "display"], db_path)

    code, out = _run(["history", "--event-type", "water_change"], db_path)
    assert code == 0
    assert "WATER_CHANGE" in out
    assert "EQUIPMENT_CHANGE" not in out
    assert "TEST" not in out


def test_history_filter_by_tank(db_path: Path):
    _run(["test", "add", "--tank", "display", "--alkalinity", "8.2"], db_path)
    _run(["test", "add", "--tank", "frag", "--alkalinity", "8.5"], db_path)

    code, out = _run(["history", "--tank", "display"], db_path)
    assert code == 0
    assert "[display]" in out
    assert "[frag]" not in out


def test_history_real_tank_includes_shared_maintenance(db_path: Path):
    """--tank display must surface 'both' maintenance events too."""
    _run(["maintenance", "add", "filter_change", "--tank", "both"], db_path)
    _run(["maintenance", "add", "water_change", "--tank", "frag"], db_path)

    code, out = _run(["history", "--tank", "display"], db_path)
    assert code == 0
    assert "[both]" in out
    assert "[frag]" not in out


def test_history_rejects_unknown_parameter(db_path: Path):
    runner = CliRunner()
    result = runner.invoke(main, ["--db", str(db_path), "history", "--parameter", "salinity"])
    assert result.exit_code != 0
