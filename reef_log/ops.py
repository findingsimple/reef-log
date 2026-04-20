"""The single seam between storage and everything else.

MCP tools and the CLI both call into this module directly. Keep it pure:
no MCP imports, no CLI imports, no I/O beyond the supplied connection.
Functions take a sqlite3.Connection so callers can wire up tmp DBs in tests.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import statistics
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image

from reef_log import db as db_module


class AlreadyProcessed(ValueError):
    """Raised when a photo's SHA-256 is already in `processed_photos`.

    Typed so the MCP wrapper can translate to a structured error without a
    fragile substring match on the message.
    """

    def __init__(self, sha256: str):
        super().__init__(f"photo already processed (sha256={sha256})")
        self.sha256 = sha256


class InvalidTank(ValueError):
    """Raised when a caller supplies an unknown tank name.

    Typed so the MCP wrapper can surface a distinct `invalid_tank` error
    (rather than lumping it under the generic `invalid_photo` bucket).
    Still subclasses `ValueError` so existing `except ValueError` paths
    keep working.
    """


# Default canonical unit per parameter. Callers may override per-measurement
# (e.g. Claude normalizes HI774 ULR ppb → ppm in conversation before calling
# log_test_from_photo with unit='ppm').
DEFAULT_UNITS: dict[str, str] = {
    "alkalinity": "dKH",
    "calcium": "ppm",
    "magnesium": "ppm",
    "phosphate": "ppm",
    "nitrate": "ppm",
}

# Canonical tank names. Stored as plain TEXT (no CHECK constraint at the DB
# level) so adding a third tank is a one-line change here. Writes are validated
# against this tuple via _check_tank — a typo like "dispaly" would otherwise
# silently disappear from compare_trends and tank-filtered reads.
TANKS: tuple[str, ...] = ("display", "frag")

# Synthetic tank value for maintenance events that affect both tanks (e.g. an
# RO/DI filter swap). Writing tank='both' once preserves the truth that one
# human did one act once — preferable to logging the same event twice and
# inflating future "average days between water changes" analyses.
# Read paths expand a query for a real tank to also include 'both' rows.
SHARED_TANK = "both"
MAINTENANCE_TANKS: tuple[str, ...] = TANKS + (SHARED_TANK,)


def _check_tank(tank: str) -> None:
    if tank not in TANKS:
        raise InvalidTank(f"unknown tank {tank!r}; expected one of {TANKS}")


def _check_maintenance_tank(tank: str) -> None:
    if tank not in MAINTENANCE_TANKS:
        raise InvalidTank(f"unknown tank {tank!r}; expected one of {MAINTENANCE_TANKS}")


def _maintenance_tank_filter(tank: str | None) -> tuple[str, ...] | None:
    """Expand a real-tank query to also match SHARED_TANK rows.

    None → no filter; tank in TANKS → that tank plus SHARED_TANK; anything
    else (SHARED_TANK itself, or a typo) → exact literal match so typos
    return empty rather than silently pulling in shared events.
    """
    if tank is None:
        return None
    if tank in TANKS:
        return (tank, SHARED_TANK)
    return (tank,)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware (UTC) before storage")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _coerce_at(at: datetime | str | None) -> str:
    if at is None:
        return _now_iso()
    if isinstance(at, datetime):
        return _to_iso(at)
    return at


def _cutoff_iso(days: int) -> str:
    return _to_iso(datetime.now(UTC) - timedelta(days=days))


def add_test_session(
    conn: sqlite3.Connection,
    measurements: list[dict[str, Any]],
    *,
    tank: str,
    measured_at: datetime | str | None = None,
    source: str = "mcp",
    notes: str | None = None,
    tz_assumed: bool = False,
) -> int:
    """Insert one test_results row plus N test_measurements. Returns the test_result_id.

    Each measurement: {parameter, value, unit?, checker_model?, confidence?}
    """
    if not measurements:
        raise ValueError("at least one measurement is required")
    _check_tank(tank)

    measured_at_iso = _coerce_at(measured_at)

    cur = conn.execute(
        "INSERT INTO test_results (measured_at, tz_assumed, source, notes, tank) "
        "VALUES (?, ?, ?, ?, ?)",
        (measured_at_iso, 1 if tz_assumed else 0, source, notes, tank),
    )
    test_result_id = cur.lastrowid
    assert test_result_id is not None

    for m in measurements:
        parameter = m["parameter"]
        unit = m.get("unit") or DEFAULT_UNITS.get(parameter)
        if unit is None:
            raise ValueError(
                f"no default unit for parameter {parameter!r}; supply 'unit' explicitly"
            )
        conn.execute(
            "INSERT INTO test_measurements "
            "(test_result_id, parameter, value, unit, checker_model, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                test_result_id,
                parameter,
                float(m["value"]),
                unit,
                m.get("checker_model"),
                m.get("confidence"),
            ),
        )

    return test_result_id


def add_maintenance(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    tank: str,
    performed_at: datetime | str | None = None,
    details: dict[str, Any] | None = None,
    notes: str | None = None,
    tz_assumed: bool = False,
) -> int:
    """Insert one maintenance_events row. Returns the new row id.

    tank accepts the canonical TANKS values plus SHARED_TANK ('both') for events
    that affect every tank (e.g. shared RO/DI filter swap). Read paths expand
    a real-tank query to also include 'both' rows.
    """
    _check_maintenance_tank(tank)
    cur = conn.execute(
        "INSERT INTO maintenance_events "
        "(performed_at, tz_assumed, event_type, details, notes, tank) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            _coerce_at(performed_at),
            1 if tz_assumed else 0,
            event_type,
            json.dumps(details) if details is not None else None,
            notes,
            tank,
        ),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def _row_to_test_session(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    measurements = conn.execute(
        "SELECT parameter, value, unit, checker_model, confidence "
        "FROM test_measurements WHERE test_result_id = ?",
        (row["id"],),
    ).fetchall()
    return {
        "kind": "test",
        "id": row["id"],
        "at": row["measured_at"],
        "tank": row["tank"],
        "tz_assumed": bool(row["tz_assumed"]),
        "source": row["source"],
        "notes": row["notes"],
        "measurements": [dict(m) for m in measurements],
    }


def _row_to_maintenance(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "kind": "maintenance",
        "id": row["id"],
        "at": row["performed_at"],
        "tank": row["tank"],
        "tz_assumed": bool(row["tz_assumed"]),
        "event_type": row["event_type"],
        "details": json.loads(row["details"]) if row["details"] else None,
        "notes": row["notes"],
    }


def get_recent(
    conn: sqlite3.Connection,
    *,
    tank: str | None = None,
    days: int = 30,
    parameter: str | None = None,
    event_type: str | None = None,
) -> list[dict[str, Any]]:
    """Unified recent view, sorted newest-first.

    tank filter restricts both tests and maintenance to one tank; None returns all.
    parameter filter restricts to test sessions containing that parameter.
    event_type filter restricts to maintenance events of that type.
    """
    cutoff = _cutoff_iso(days)
    out: list[dict[str, Any]] = []

    include_tests = event_type is None
    include_maintenance = parameter is None

    if include_tests:
        sql = "SELECT * FROM test_results WHERE measured_at >= ?"
        params: list[Any] = [cutoff]
        if tank is not None:
            sql += " AND tank = ?"
            params.append(tank)
        if parameter is not None:
            sql += (
                " AND EXISTS (SELECT 1 FROM test_measurements tm "
                "WHERE tm.test_result_id = test_results.id AND tm.parameter = ?)"
            )
            params.append(parameter)
        sql += " ORDER BY measured_at DESC"
        test_rows = conn.execute(sql, params).fetchall()
        out.extend(_row_to_test_session(conn, r) for r in test_rows)

    if include_maintenance:
        sql = "SELECT * FROM maintenance_events WHERE performed_at >= ?"
        params = [cutoff]
        tanks = _maintenance_tank_filter(tank)
        if tanks is not None:
            placeholders = ",".join("?" * len(tanks))
            sql += f" AND tank IN ({placeholders})"
            params.extend(tanks)
        if event_type is not None:
            sql += " AND event_type = ?"
            params.append(event_type)
        sql += " ORDER BY performed_at DESC"
        mx_rows = conn.execute(sql, params).fetchall()
        out.extend(_row_to_maintenance(r) for r in mx_rows)

    out.sort(key=lambda x: x["at"], reverse=True)
    return out


def get_parameter_history(
    conn: sqlite3.Connection,
    parameter: str,
    *,
    tank: str,
    days: int = 90,
) -> list[dict[str, Any]]:
    cutoff = _cutoff_iso(days)
    rows = conn.execute(
        "SELECT tr.measured_at AS at, tm.value, tm.unit, tm.checker_model, tm.confidence "
        "FROM test_measurements tm "
        "JOIN test_results tr ON tr.id = tm.test_result_id "
        "WHERE tm.parameter = ? AND tr.tank = ? AND tr.measured_at >= ? "
        "ORDER BY tr.measured_at ASC",
        (parameter, tank, cutoff),
    ).fetchall()
    return [dict(r) for r in rows]


def get_last_event(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    tank: str,
) -> dict[str, Any] | None:
    """Most recent maintenance event of the given type for the given tank.

    A real-tank query (e.g. tank='display') also matches SHARED_TANK rows so
    shared events like a system-wide filter swap show up in either tank's view.
    """
    tanks = _maintenance_tank_filter(tank)
    assert tanks is not None  # tank is required, can't be None here
    placeholders = ",".join("?" * len(tanks))
    row = conn.execute(
        f"SELECT * FROM maintenance_events "
        f"WHERE event_type = ? AND tank IN ({placeholders}) "
        f"ORDER BY performed_at DESC LIMIT 1",
        (event_type, *tanks),
    ).fetchone()
    return _row_to_maintenance(row) if row is not None else None


def analyze_trends(
    conn: sqlite3.Connection,
    parameter: str,
    *,
    tank: str,
    days: int = 90,
) -> dict[str, Any]:
    history = get_parameter_history(conn, parameter, tank=tank, days=days)
    count = len(history)

    if count == 0:
        return {
            "parameter": parameter,
            "tank": tank,
            "days": days,
            "count": 0,
            "summary": f"{parameter} ({tank}): no measurements in the last {days} days.",
        }

    values = [h["value"] for h in history]
    unit = history[-1]["unit"]
    latest_value = values[-1]
    latest_at = history[-1]["at"]

    vmin = min(values)
    vmax = max(values)
    vmean = statistics.fmean(values)
    vstdev = statistics.stdev(values) if count > 1 else 0.0

    summary = (
        f"{parameter} ({tank}): {count} measurement{'s' if count > 1 else ''} "
        f"over {days} days, range {vmin:g}–{vmax:g} {unit}, "
        f"mean {vmean:.2f} (stdev {vstdev:.2f}). "
        f"Latest {latest_value:g} {unit} at {latest_at}."
    )

    return {
        "parameter": parameter,
        "tank": tank,
        "unit": unit,
        "days": days,
        "count": count,
        "min": vmin,
        "max": vmax,
        "mean": vmean,
        "stdev": vstdev,
        "latest_value": latest_value,
        "latest_at": latest_at,
        "summary": summary,
    }


# Guards against accidentally hashing /dev/urandom (hangs) or a zero-byte
# file (deterministic empty-file SHA collides across every future empty file).
MIN_PHOTO_BYTES = 1
MAX_PHOTO_BYTES = 50 * 1024 * 1024  # 50 MiB

# Reject wildly out-of-range EXIF timestamps that usually mean the camera's
# clock was unset. Outside this window we fall through to file mtime.
MIN_EXIF_YEAR = 2020
MAX_EXIF_FUTURE = timedelta(days=1)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_exif_offset(offset_str: str) -> timezone:
    """Parse EXIF OffsetTimeOriginal ('+HH:MM' or '-HH:MM') to a tzinfo.

    Malformed values raise — EXIF that claims an offset but provides garbage
    is a real data problem, not something to silently paper over.
    """
    if len(offset_str) < 6 or offset_str[0] not in "+-":
        raise ValueError(f"malformed EXIF offset: {offset_str!r}")
    sign = 1 if offset_str[0] == "+" else -1
    h, m = offset_str[1:].split(":")
    return timezone(sign * timedelta(hours=int(h), minutes=int(m)))


def _photo_measured_at(path: Path) -> tuple[datetime, bool]:
    """Return (UTC-aware datetime, tz_assumed).

    Uses EXIF DateTimeOriginal (+ OffsetTimeOriginal when present). Falls back
    to file mtime when: EXIF is absent, DateTimeOriginal is missing or
    unparseable, or the parsed timestamp is outside the sanity window
    (pre-2020 or more than a day in the future — usually an unset camera
    clock). Fallbacks always set tz_assumed=True.

    A malformed OffsetTimeOriginal raises (via _parse_exif_offset) rather than
    silently degrading. Non-image files raise UnidentifiedImageError (bubbled
    from Image.open) — callers translate at the tool boundary.
    """
    mtime_fallback = (datetime.fromtimestamp(path.stat().st_mtime, UTC), True)

    with Image.open(path) as img:
        exif = img.getexif()

    if not exif:
        return mtime_fallback

    exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
    dt_str = exif_ifd.get(ExifTags.Base.DateTimeOriginal.value)
    offset_str = exif_ifd.get(ExifTags.Base.OffsetTimeOriginal.value)

    if not dt_str:
        return mtime_fallback

    try:
        naive = datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return mtime_fallback

    if offset_str:
        aware = naive.replace(tzinfo=_parse_exif_offset(offset_str))
        tz_assumed = False
    else:
        # Naive EXIF — interpret as laptop local tz, flag the assumption.
        aware = naive.astimezone()
        tz_assumed = True

    stored = aware.astimezone(UTC)
    if stored.year < MIN_EXIF_YEAR or stored > datetime.now(UTC) + MAX_EXIF_FUTURE:
        return mtime_fallback

    return stored, tz_assumed


def is_photo_processed(conn: sqlite3.Connection, sha256: str) -> bool:
    row = conn.execute("SELECT 1 FROM processed_photos WHERE sha256 = ?", (sha256,)).fetchone()
    return row is not None


def log_test_from_photo(
    conn: sqlite3.Connection,
    *,
    path: str | os.PathLike[str],
    tank: str,
    measurements: list[dict[str, Any]],
    notes: str | None = None,
) -> dict[str, Any]:
    """Log a test session sourced from a photo.

    Claude (in conversation) reads the photo and supplies `measurements`.
    Server is authoritative for timestamp (parsed from EXIF here) and
    dedup (SHA-256 of the file contents — rejected if already in
    processed_photos). No vision API is called.

    All three writes (test_results + test_measurements + processed_photos)
    are wrapped in a single transaction so a failure between them can't
    leave an orphan test session that would silently re-log on retry.
    """
    _check_tank(tank)

    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"photo not found: {p}")

    size = p.stat().st_size
    if size < MIN_PHOTO_BYTES:
        raise ValueError(f"photo is empty: {p}")
    if size > MAX_PHOTO_BYTES:
        raise ValueError(f"photo is too large ({size} bytes > {MAX_PHOTO_BYTES}): {p}")

    sha256 = _sha256_file(p)
    if is_photo_processed(conn, sha256):
        raise AlreadyProcessed(sha256)

    measured_at_dt, tz_assumed = _photo_measured_at(p)

    with db_module.transaction(conn):
        test_result_id = add_test_session(
            conn,
            measurements,
            tank=tank,
            measured_at=measured_at_dt,
            source=f"photo:{sha256}",
            notes=notes,
            tz_assumed=tz_assumed,
        )
        conn.execute(
            "INSERT INTO processed_photos "
            "(sha256, path, test_result_id, status, extracted_payload, tank) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                sha256,
                str(p.resolve()),
                test_result_id,
                "committed",
                json.dumps({"measurements": measurements, "notes": notes}),
                tank,
            ),
        )

    return {
        "test_result_id": test_result_id,
        "sha256": sha256,
        "measured_at": _to_iso(measured_at_dt),
        "tz_assumed": tz_assumed,
    }


def compare_trends(
    conn: sqlite3.Connection,
    parameter: str,
    *,
    days: int = 90,
) -> dict[str, Any]:
    """Side-by-side trend stats for every tank in TANKS over the same window."""
    per_tank = {t: analyze_trends(conn, parameter, tank=t, days=days) for t in TANKS}

    parts = []
    for t, stats in per_tank.items():
        if stats["count"] == 0:
            parts.append(f"{t} no data")
        else:
            parts.append(
                f"{t} mean {stats['mean']:.2f} {stats['unit']} (latest {stats['latest_value']:g})"
            )
    summary = f"{parameter} over {days} days — " + "; ".join(parts) + "."

    return {
        "parameter": parameter,
        "days": days,
        "tanks": per_tank,
        "summary": summary,
    }
