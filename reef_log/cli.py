"""reef-log CLI — wraps ops.py for terminal use.

This is intentionally flag-driven (not interactive prompts) so it's easy
to script and easy to test. The MCP server is the primary conversational
entry point; the CLI is for verification and backfill workflows.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from reef_log import db as db_module
from reef_log import ops

PARAM_FLAGS = ["alkalinity", "calcium", "magnesium", "phosphate", "nitrate"]


@click.group()
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=f"SQLite path (default: {db_module.DEFAULT_DB_PATH})",
)
@click.pass_context
def main(ctx: click.Context, db_path: Path | None) -> None:
    """Local reef tank log."""
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = db_path


@main.group()
def test() -> None:
    """Log or query test results."""


@test.command("add")
@click.option(
    "--tank",
    type=click.Choice(ops.TANKS),
    required=True,
    help="Which tank these readings belong to.",
)
@click.option("--alkalinity", type=float, help="dKH")
@click.option("--calcium", type=float, help="ppm")
@click.option("--magnesium", type=float, help="ppm")
@click.option("--phosphate", type=float, help="ppm")
@click.option("--nitrate", type=float, help="ppm")
@click.option("--notes", default=None)
@click.pass_context
def test_add(ctx: click.Context, tank: str, notes: str | None, **values: float | None) -> None:
    """Log a test session. Pass one or more parameter flags."""
    measurements = [{"parameter": name, "value": v} for name, v in values.items() if v is not None]
    if not measurements:
        raise click.UsageError("supply at least one parameter flag (e.g. --alkalinity 8.2)")

    conn = db_module.connect(ctx.obj["db_path"])
    try:
        test_id = ops.add_test_session(conn, measurements, tank=tank, source="cli", notes=notes)
    finally:
        conn.close()

    rendered = ", ".join(f"{m['parameter']}={m['value']}" for m in measurements)
    click.echo(f"Logged test #{test_id} ({tank}): {rendered}")


@main.group()
def maintenance() -> None:
    """Log or query maintenance events."""


@maintenance.command("add")
@click.argument("event_type")
@click.option(
    "--tank",
    type=click.Choice(ops.MAINTENANCE_TANKS),
    required=True,
    help="Which tank this event belongs to. Use 'both' for shared events.",
)
@click.option(
    "--detail",
    "details",
    multiple=True,
    metavar="KEY=VALUE",
    help="Event detail; repeat for multiple. Values parsed as JSON when possible.",
)
@click.option("--notes", default=None)
@click.pass_context
def maintenance_add(
    ctx: click.Context,
    event_type: str,
    tank: str,
    details: tuple[str, ...],
    notes: str | None,
) -> None:
    """Log a maintenance event (water_change, equipment_change, etc.)."""
    parsed: dict[str, Any] | None = None
    if details:
        parsed = {}
        for kv in details:
            if "=" not in kv:
                raise click.UsageError(f"--detail must be KEY=VALUE, got {kv!r}")
            key, _, raw = kv.partition("=")
            try:
                parsed[key] = json.loads(raw)
            except json.JSONDecodeError:
                parsed[key] = raw

    conn = db_module.connect(ctx.obj["db_path"])
    try:
        mid = ops.add_maintenance(conn, event_type, tank=tank, details=parsed, notes=notes)
    finally:
        conn.close()

    click.echo(f"Logged maintenance #{mid} ({tank}): {event_type}")


PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png"}


@main.group()
def photos() -> None:
    """Inspect photo backfill state."""


@photos.command("pending")
@click.argument(
    "directory",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--recursive",
    "-r",
    is_flag=True,
    help="Scan subdirectories too (default: top level only).",
)
@click.pass_context
def photos_pending(ctx: click.Context, directory: Path, recursive: bool) -> None:
    """List photos in a directory that haven't been logged yet.

    Hashes each .jpg / .jpeg / .png and checks processed_photos. Useful
    before a Claude Desktop backfill conversation: you'll know how many
    files are new vs. already done. HEIC files are skipped (not supported
    without pillow-heif).
    """
    iterator = directory.rglob("*") if recursive else directory.iterdir()
    candidates = sorted(p for p in iterator if p.is_file() and p.suffix.lower() in PHOTO_EXTENSIONS)

    if not candidates:
        click.echo(f"No photos found in {directory}.")
        return

    conn = db_module.connect(ctx.obj["db_path"])
    try:
        pending, already = ops.partition_photos_by_processed(conn, candidates)
    finally:
        conn.close()

    click.echo(f"Scanned {len(candidates)} file(s) in {directory}")
    if pending:
        click.echo(f"  {len(pending)} new (not yet logged):")
        for p in pending:
            click.echo(f"    {p}")
    else:
        click.echo("  0 new — nothing pending.")
    if already:
        click.echo(f"  {len(already)} already logged")


@main.command("history")
@click.option(
    "--tank",
    "-t",
    default=None,
    type=click.Choice(ops.MAINTENANCE_TANKS),
    help="Filter to one tank ('display'/'frag' includes shared 'both' events).",
)
@click.option("--parameter", "-p", default=None, type=click.Choice(PARAM_FLAGS))
@click.option("--event-type", "-e", default=None)
@click.option("--days", "-d", type=int, default=30)
@click.pass_context
def history(
    ctx: click.Context,
    tank: str | None,
    parameter: str | None,
    event_type: str | None,
    days: int,
) -> None:
    """Show recent activity."""
    conn = db_module.connect(ctx.obj["db_path"])
    try:
        rows = ops.get_recent(
            conn, tank=tank, days=days, parameter=parameter, event_type=event_type
        )
    finally:
        conn.close()

    if not rows:
        click.echo(f"No activity in the last {days} days.")
        return

    for r in rows:
        tank_tag = f"[{r['tank']}]"
        if r["kind"] == "test":
            measurements = ", ".join(
                f"{m['parameter']}={m['value']}{m['unit']}" for m in r["measurements"]
            )
            click.echo(f"{r['at']} {tank_tag:<9} TEST  {measurements}")
        else:
            detail_str = json.dumps(r["details"]) if r["details"] else ""
            click.echo(f"{r['at']} {tank_tag:<9} {r['event_type'].upper():<18} {detail_str}")


if __name__ == "__main__":
    main()
