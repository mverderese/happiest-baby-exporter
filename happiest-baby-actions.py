#!/usr/bin/env -S uv run
# /// script
# dependencies = [
#   "requests>=2.32.0",
#   "click>=8.1.0",
#   "paho-mqtt>=2.0.0",
#   "PySocks>=1.7.1",
# ]
# requires-python = ">=3.12"
# ///
"""
Control a SNOO and write journal entries through the Happiest Baby API.

A backup for when the app misbehaves. SNOO commands go over the same AWS IoT MQTT
channel the app uses; journal entries go to the REST API. Both are undocumented and
reverse-engineered (see python-snoo), so they may break without notice.

Every command that changes something prints what it is about to do and asks for
confirmation, unless --yes is given. --dry-run prints the plan and exits.

Needs the happiest_baby package that sits next to this script.
"""

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import click

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import happiest_baby as hb  # noqa: E402
from happiest_baby.client import JOURNAL_TYPES  # noqa: E402


def _load_exporter():
    """Import the sibling happiest-baby-exporter.py (hyphenated name, so via importlib)."""
    spec = importlib.util.spec_from_file_location("exporter", SCRIPT_DIR / "happiest-baby-exporter.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Context:
    """Options shared by every subcommand, with auth done lazily and only once."""

    def __init__(self, creds: Dict[str, Any], tz: str, yes: bool, dry_run: bool):
        self.creds = creds
        self.tz = ZoneInfo(tz)
        self.yes = yes
        self.dry_run = dry_run
        self._client: Optional[hb.HappiestBabyClient] = None

    @property
    def client(self) -> hb.HappiestBabyClient:
        if self._client is None:
            # The exporter handles credential files, env vars, and prompting
            self._client = hb.HappiestBabyClient(_load_exporter().get_id_token(**self.creds))
        return self._client

    def confirm(self, plan: str) -> None:
        """Show the planned change, then stop (dry run), proceed (--yes), or ask."""
        click.echo(f"Plan: {plan}")
        if self.dry_run:
            click.echo("Dry run; nothing was changed.")
            sys.exit(0)
        if self.yes:
            return
        if not sys.stdin.isatty():
            click.echo("Error: Not confirmed; re-run with --yes to do this", err=True)
            sys.exit(2)
        if not click.confirm("Go ahead?"):
            click.echo("Cancelled; nothing was changed.")
            sys.exit(1)


def fail(e: Exception, code: int = 1) -> None:
    click.echo(f"Error: {e}", err=True)
    sys.exit(code)


def when(value: Optional[str], tz: ZoneInfo) -> datetime:
    try:
        return hb.parse_time(value, tz)
    except ValueError as e:
        raise click.BadParameter(str(e))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.group()
@click.option("--email", envvar="HAPPIESTBABY_EMAIL", help="Email address (or HAPPIESTBABY_EMAIL env var)")
@click.option("--password", envvar="HAPPIESTBABY_PASSWORD", help="Password (or HAPPIESTBABY_PASSWORD env var)")
@click.option(
    "--refresh-token",
    envvar="HAPPIESTBABY_REFRESH_TOKEN",
    help="Refresh token to use instead of email/password (or HAPPIESTBABY_REFRESH_TOKEN env var)",
)
@click.option(
    "--credentials-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="JSON file containing a refresh_token (as created by build_skill.py)",
)
@click.option("--timezone", "tz", default="America/New_York", show_default=True,
              help="Timezone for times you enter and see")
@click.option("--yes", "-y", is_flag=True, help="Don't ask for confirmation before changing anything")
@click.option("--dry-run", is_flag=True, help="Show what would change, then exit without changing it")
@click.pass_context
def cli(click_ctx, email, password, refresh_token, credentials_file, tz, yes, dry_run):
    """Control your SNOO and log journal entries without the Happiest Baby app."""
    creds = {
        "email": email,
        "password": password,
        "refresh_token": refresh_token,
        "credentials_file": credentials_file,
    }
    click_ctx.obj = Context(creds, tz, yes, dry_run)


# --- SNOO ------------------------------------------------------------------


@cli.group()
def snoo():
    """Start, stop, and change the level of a SNOO."""


device_option = click.option("--device", help="SNOO name or serial number (needed only with more than one SNOO)")


def select_device(ctx: Context, device: Optional[str]) -> Dict[str, Any]:
    try:
        return ctx.client.select_device(device)
    except hb.HappiestBabyError as e:
        fail(e)


def run_snoo(ctx: Context, device: Optional[str], plan: str, action: str, **options: Any) -> None:
    dev = select_device(ctx, device)
    ctx.confirm(f"{plan} on {dev.get('name', 'SNOO')}")
    click.echo(f"Connecting to {dev.get('name', 'SNOO')}...", err=True)
    try:
        result = hb.snoo_command(ctx.client.id_token, dev, action, **options)
    except hb.SnooNotConfirmed as e:
        fail(e, 3)
    except hb.HappiestBabyError as e:
        fail(e)
    click.echo(f"✓ Done. SNOO is now: {hb.describe_state(result)}")


@snoo.command("devices")
@click.pass_obj
def snoo_devices(ctx: Context):
    """List the SNOOs on this account."""
    try:
        devices = ctx.client.devices()
    except hb.HappiestBabyError as e:
        fail(e)
    for d in devices:
        iot = d.get("awsIoT") or {}
        online = (d.get("presenceIoT") or d.get("presence") or {}).get("online")
        click.echo(f"{d.get('name', 'Unnamed')}  serial={d.get('serialNumber')}  firmware={d.get('firmwareVersion')}"
                   f"  online={online}  mqtt_host={iot.get('clientEndpoint')}")


@snoo.command("status")
@device_option
@click.option("--json", "as_json", is_flag=True, help="Print the raw state message")
@click.pass_obj
def snoo_status(ctx: Context, device: Optional[str], as_json: bool):
    """Show the SNOO's current level, hold, and timer."""
    dev = select_device(ctx, device)
    click.echo(f"Connecting to {dev.get('name', 'SNOO')}...", err=True)
    try:
        result = hb.snoo_status(ctx.client.id_token, dev)
    except hb.HappiestBabyError as e:
        fail(e, 3)
    click.echo(json.dumps(result, indent=2) if as_json else f"SNOO is: {hb.describe_state(result)}")


@snoo.command("start")
@device_option
@click.pass_obj
def snoo_start(ctx: Context, device: Optional[str]):
    """Start the SNOO at its baseline level."""
    run_snoo(ctx, device, "Start the SNOO", "start")


@snoo.command("stop")
@device_option
@click.pass_obj
def snoo_stop(ctx: Context, device: Optional[str]):
    """Stop the SNOO."""
    run_snoo(ctx, device, "Stop the SNOO", "stop")


@snoo.command("level")
@click.argument("level", type=click.Choice(list(hb.LEVELS), case_sensitive=False))
@click.option("--hold/--no-hold", default=False, show_default=True, help="Keep the SNOO at this level")
@device_option
@click.pass_obj
def snoo_level(ctx: Context, level: str, hold: bool, device: Optional[str]):
    """Set the SNOO to LEVEL (baseline, 1, 2, 3, 4), starting it if stopped."""
    target = hb.LEVELS[level.lower()]
    run_snoo(ctx, device, f"Set the SNOO to {target} with hold {'on' if hold else 'off'}",
             "level", level=level, hold=hold)


@snoo.command("white-noise")
@click.argument("setting", type=click.Choice(["on", "off"], case_sensitive=False))
@click.option("--minutes", type=click.Choice(["15", "30", "60"]), default="15", show_default=True,
              help="Turn sticky white noise off after this long")
@device_option
@click.pass_obj
def snoo_white_noise(ctx: Context, setting: str, minutes: str, device: Optional[str]):
    """Turn sticky white noise (sound without motion) on or off."""
    on = setting.lower() == "on"
    plan = f"Turn sticky white noise {setting.lower()}" + (f" for {minutes} minutes" if on else "")
    run_snoo(ctx, device, plan, "white_noise", on=on, minutes=int(minutes))


# --- Journal ---------------------------------------------------------------


@cli.group()
def journal():
    """Log, review, and delete journal entries (diapers, feeds, ...)."""


baby_option = click.option("--baby-name", help="Baby name (needed only with more than one baby)")
time_option = click.option("--at", "at", help="When it happened: now (default), 15m, 2h, HH:MM, or ISO 8601")
note_option = click.option("--note", help="Optional note")


@journal.command("recent")
@click.option("--type", "journal_type", type=click.Choice(JOURNAL_TYPES), required=True)
@click.option("--days", type=int, default=2, show_default=True, help="How far back to look")
@click.option("--limit", type=int, default=5, show_default=True, help="Show at most this many (newest first)")
@baby_option
@click.pass_obj
def journal_recent(ctx: Context, journal_type: str, days: int, limit: int, baby_name: Optional[str]):
    """Print recent entries as raw JSON, including their ids and data shapes."""
    to_date = datetime.now(timezone.utc)
    from_date = to_date - timedelta(days=days)
    try:
        if journal_type == "pumping":
            entries = ctx.client.pumping_journals(from_date, to_date)
        else:
            baby_id = ctx.client.select_baby(baby_name)["_id"]
            entries = ctx.client.journals(baby_id, journal_type, from_date, to_date)
    except hb.HappiestBabyError as e:
        fail(e)
    entries.sort(key=lambda e: e.get("startTime", ""), reverse=True)
    click.echo(json.dumps(entries[:limit], indent=2))


def create_entry(ctx: Context, baby_name: Optional[str], journal_type: str, data: Dict[str, Any],
                 at: datetime, end_at: Optional[datetime], note: Optional[str], plan: str) -> None:
    payload: Dict[str, Any] = {"type": journal_type, "data": data, "startTime": hb.api_time(at)}
    if end_at:
        payload["endTime"] = hb.api_time(end_at)
    if note:
        payload["note"] = note
    who = ""
    try:
        if journal_type != "pumping":
            baby = ctx.client.select_baby(baby_name)
            payload["babyId"] = baby["_id"]
            who = f" for {baby.get('babyName', 'baby')}"
    except hb.HappiestBabyError as e:
        fail(e)

    ctx.confirm(f"{plan}{who} at {hb.show_time(at, ctx.tz)}" + (f' with note "{note}"' if note else ""))
    try:
        created = ctx.client.create_journal(payload)
    except hb.HappiestBabyError as e:
        fail(e)
    entry_id = (created.get("id") or created.get("_id")) if isinstance(created, dict) else None
    click.echo(f"✓ Logged{f' (id {entry_id})' if entry_id else ''}")


@journal.command("diaper")
@click.option("--pee", is_flag=True, help="Wet diaper")
@click.option("--poo", is_flag=True, help="Dirty diaper")
@time_option
@note_option
@baby_option
@click.pass_obj
def journal_diaper(ctx: Context, pee: bool, poo: bool, at: Optional[str], note: Optional[str], baby_name: Optional[str]):
    """Log a diaper change (--pee, --poo, or both)."""
    types = [t for t, flag in (("pee", pee), ("poo", poo)) if flag]
    if not types:
        raise click.UsageError("Pass --pee, --poo, or both")
    create_entry(ctx, baby_name, "diaper", {"types": types}, when(at, ctx.tz), None, note,
                 f"Log a {' + '.join(types)} diaper")


@journal.command("add")
@click.option("--type", "journal_type", type=click.Choice(JOURNAL_TYPES), required=True)
@click.option("--data", "data_json", required=True, help="The entry's data object as JSON")
@time_option
@click.option("--end-at", help="When it ended, for entries with a duration (same formats as --at)")
@note_option
@baby_option
@click.pass_obj
def journal_add(ctx: Context, journal_type: str, data_json: str, at: Optional[str], end_at: Optional[str],
                note: Optional[str], baby_name: Optional[str]):
    """EXPERIMENTAL: log any entry type with a hand-built data object.

    Only diapers are confirmed to work. For other types, copy the "data" shape from
    `journal recent --type TYPE` so the new entry matches what the app writes.
    """
    try:
        data = json.loads(data_json)
    except ValueError as e:
        raise click.BadParameter(f"--data is not valid JSON - {e}")
    if not isinstance(data, dict):
        raise click.BadParameter("--data must be a JSON object")
    start = when(at, ctx.tz)
    end = when(end_at, ctx.tz) if end_at else None
    if end and end < start:
        raise click.BadParameter("--end-at is before --at")
    create_entry(ctx, baby_name, journal_type, data, start, end, note,
                 f"Log a {journal_type} entry with data {json.dumps(data)}")


@journal.command("delete")
@click.argument("journal_id")
@click.pass_obj
def journal_delete(ctx: Context, journal_id: str):
    """EXPERIMENTAL: delete the entry with JOURNAL_ID (find ids with `journal recent`)."""
    ctx.confirm(f"Delete journal entry {journal_id}")
    try:
        ctx.client.delete_journal(journal_id)
    except hb.HappiestBabyError as e:
        fail(e)
    click.echo("✓ Deleted")


if __name__ == "__main__":
    cli()
