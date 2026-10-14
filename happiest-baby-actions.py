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
"""

import importlib.util
import json
import os
import re
import ssl
import sys
import threading
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

import click
import requests


def _load_exporter():
    """Import the sibling happiest-baby-exporter.py (hyphenated name, so via importlib)."""
    path = Path(__file__).resolve().with_name("happiest-baby-exporter.py")
    spec = importlib.util.spec_from_file_location("exporter", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


exporter = _load_exporter()
API_BASE_URL = exporter.API_BASE_URL

# API Endpoints
DEVICES_ENDPOINT = "/hds/me/v11/devices"
JOURNAL_CREATE_ENDPOINT = "/cs/me/v11/journals"
JOURNAL_ITEM_ENDPOINT = "/cs/me/v11/journals/{journal_id}"
PUMPING_CREATE_ENDPOINT = "/cs/me/v11/pumping-journals"

# SNOO MQTT details, matching the iOS app as used by python-snoo
MQTT_USERNAME = "?SDK=iOS&Version=2.40.1"
CONTROL_TOPIC = "{thing}/state_machine/control"
ACTIVITY_TOPIC = "{thing}/state_machine/activity_state"

LEVELS = {
    "baseline": "BASELINE",
    "1": "LEVEL1",
    "2": "LEVEL2",
    "3": "LEVEL3",
    "4": "LEVEL4",
}
JOURNAL_TYPES = ["breastfeeding", "bottlefeeding", "diaper", "solidfood", "pumping"]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class Context:
    """Options shared by every subcommand, with auth done lazily and only once."""

    def __init__(self, creds: Dict[str, Any], timezone: str, yes: bool, dry_run: bool):
        self.creds = creds
        self.tz = ZoneInfo(timezone)
        self.yes = yes
        self.dry_run = dry_run
        self._token: Optional[str] = None

    @property
    def token(self) -> str:
        if self._token is None:
            self._token = exporter.get_id_token(**self.creds)
        return self._token

    def headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

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


def api_request(ctx: Context, method: str, endpoint: str, **kwargs: Any) -> Any:
    """Call the REST API, exiting with the server's error body on failure."""
    url = f"{API_BASE_URL}{endpoint}"
    try:
        response = requests.request(method, url, headers=ctx.headers(), timeout=30, **kwargs)
    except requests.exceptions.RequestException as e:
        click.echo(f"Error: Network error - {e}", err=True)
        sys.exit(1)
    if not response.ok:
        click.echo(f"Error: {method} {endpoint} failed with HTTP {response.status_code}", err=True)
        if response.text:
            click.echo(f"  {response.text[:1000]}", err=True)
        sys.exit(1)
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


def parse_time(value: Optional[str], tz: ZoneInfo) -> datetime:
    """Parse 'now', '15m'/'2h' (ago), 'HH:MM' (today, or yesterday if in the future), or ISO 8601."""
    now = datetime.now(tz)
    if not value or value.lower() == "now":
        return now

    relative = re.fullmatch(r"(\d+)\s*(m|min|h|hr)(?:\s*ago)?", value.strip().lower())
    if relative:
        amount, unit = int(relative.group(1)), relative.group(2)
        return now - (timedelta(hours=amount) if unit.startswith("h") else timedelta(minutes=amount))

    clock = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if clock:
        at = now.replace(hour=int(clock.group(1)), minute=int(clock.group(2)), second=0, microsecond=0)
        # "02:30" said at 08:00 means this morning; said at 01:00 it means last night
        return at - timedelta(days=1) if at > now else at

    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        raise click.BadParameter(f"Can't read time '{value}'; use now, 15m, 2h, HH:MM, or ISO 8601")
    return at if at.tzinfo else at.replace(tzinfo=tz)


def api_time(at: datetime) -> str:
    """The API requires an explicit UTC offset and accepts milliseconds."""
    return at.isoformat(timespec="milliseconds")


def show_time(at: datetime, tz: ZoneInfo) -> str:
    return at.astimezone(tz).strftime("%a %b %-d %-I:%M %p %Z")


# ---------------------------------------------------------------------------
# SNOO control over MQTT
# ---------------------------------------------------------------------------


def fetch_devices(ctx: Context) -> List[Dict[str, Any]]:
    data = api_request(ctx, "GET", DEVICES_ENDPOINT)
    return data.get("snoo", []) if isinstance(data, dict) else []


def select_device(ctx: Context, device_name: Optional[str]) -> Dict[str, Any]:
    devices = fetch_devices(ctx)
    if not devices:
        click.echo("Error: No SNOO found on this account", err=True)
        sys.exit(1)
    if device_name:
        matches = [
            d for d in devices
            if device_name.lower() in (str(d.get("name", "")).lower(), str(d.get("serialNumber", "")).lower())
        ]
    else:
        matches = devices if len(devices) == 1 else []
    if len(matches) != 1:
        click.echo("Error: Pass --device with the name or serial of one of:", err=True)
        for d in devices:
            click.echo(f"  - {d.get('name', 'Unnamed')} ({d.get('serialNumber')})", err=True)
        sys.exit(1)
    device = matches[0]
    if not (device.get("awsIoT") or {}).get("clientEndpoint"):
        click.echo(f"Error: {device.get('name')} has no AWS IoT endpoint; is its firmware up to date?", err=True)
        sys.exit(1)
    return device


def _ssl_context() -> ssl.SSLContext:
    cafile = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    return ssl.create_default_context(cafile=cafile if cafile and os.path.isfile(cafile) else None)


def _proxy_args(host: str) -> Optional[Dict[str, Any]]:
    """HTTP(S) proxy from the environment, for sandboxes that only allow outbound traffic through one."""
    if urllib.request.proxy_bypass(host):
        return None
    proxy = urllib.request.getproxies().get("https")
    if not proxy:
        return None
    import socks  # PySocks; paho uses it for proxying

    parts = urllib.parse.urlparse(proxy)
    args: Dict[str, Any] = {
        "proxy_type": socks.HTTP,
        "proxy_addr": parts.hostname,
        "proxy_port": parts.port or 8080,
        "proxy_rdns": True,
    }
    if parts.username:
        args["proxy_username"] = urllib.parse.unquote(parts.username)
        args["proxy_password"] = urllib.parse.unquote(parts.password or "")
    return args


def snoo_exchange(
    ctx: Context,
    device: Dict[str, Any],
    command: Optional[Dict[str, Any]],
    done: Callable[[Dict[str, Any]], bool],
    timeout: float = 20.0,
) -> Optional[Dict[str, Any]]:
    """Connect to the SNOO's MQTT channel, send a command, and wait for a matching state.

    Returns the first activity_state message for which done() is true, or the last one
    seen (possibly None) on timeout. A status request is sent after a few seconds if the
    SNOO hasn't reported a matching state on its own.
    """
    import paho.mqtt.client as mqtt

    iot = device["awsIoT"]
    host = iot["clientEndpoint"]
    thing = iot["thingName"]
    control_topic = CONTROL_TOPIC.format(thing=thing)

    matched = threading.Event()
    connected = threading.Event()
    state: Dict[str, Any] = {"last": None, "error": None, "result": None}

    def publish(payload: Dict[str, Any]) -> None:
        ts = int(datetime.now().timestamp() * 10_000_000)  # the app's timestamp format
        client.publish(control_topic, json.dumps({"ts": ts, **payload}), qos=1)

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            state["error"] = f"MQTT connection refused: {reason_code}"
            connected.set()
            matched.set()
            return
        client.subscribe(ACTIVITY_TOPIC.format(thing=thing), qos=1)

    def on_subscribe(client, userdata, mid, reason_codes, properties):
        connected.set()
        publish(command if command else {"command": "send_status"})

    def on_disconnect(client, userdata, flags, reason_code, properties):
        if not connected.is_set():
            # AWS IoT drops the socket instead of refusing when the token is rejected
            state["error"] = f"Connection closed by {host} before subscribing ({reason_code}); the login may have been rejected"
            connected.set()
            matched.set()

    def on_message(client, userdata, message):
        try:
            data = json.loads(message.payload.decode())
        except ValueError:
            return
        state["last"] = data
        if done(data):
            state["result"] = data
            matched.set()

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"hb_exporter_{uuid.uuid4()}",
        transport="websockets",
        protocol=mqtt.MQTTv31,
    )
    client.username_pw_set(MQTT_USERNAME)
    client.ws_set_options(path="/mqtt", headers={"token": ctx.token})
    client.tls_set_context(_ssl_context())
    proxy = _proxy_args(host)
    if proxy:
        client.proxy_set(**proxy)
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    client.on_disconnect = on_disconnect

    click.echo(f"Connecting to {device.get('name', 'SNOO')}...")
    try:
        client.connect(host, 443, keepalive=30)
    except OSError as e:
        click.echo(f"Error: Could not reach {host} - {e}", err=True)
        sys.exit(1)
    client.loop_start()
    try:
        if not connected.wait(15):
            click.echo(f"Error: Timed out connecting to {host}", err=True)
            sys.exit(1)
        if command and not matched.wait(min(5.0, timeout)):
            publish({"command": "send_status"})
        matched.wait(timeout)
    finally:
        client.disconnect()
        client.loop_stop()

    if state["error"]:
        click.echo(f"Error: {state['error']}", err=True)
        sys.exit(1)
    return state["result"] or state["last"]


def describe_state(data: Optional[Dict[str, Any]]) -> str:
    if not data:
        return "no state received"
    sm = data.get("state_machine", {})
    level = sm.get("state", "?")
    parts = [{"ONLINE": "stopped"}.get(level, level)]
    if sm.get("hold") == "on":
        parts.append("hold on")
    if sm.get("is_active_session") and sm.get("time_left", -1) >= 0:
        parts.append(f"{sm['time_left'] // 60} min left at this level")
    if sm.get("sticky_white_noise") == "on":
        parts.append("sticky white noise on")
    if sm.get("weaning") == "on":
        parts.append("weaning on")
    clips = (data.get("left_safety_clip"), data.get("right_safety_clip"))
    if clips != (None, None) and not all(clips):
        parts.append("⚠ safety clip not attached")
    return ", ".join(parts)


def run_snoo_command(
    ctx: Context,
    device_name: Optional[str],
    plan: str,
    command: Dict[str, Any],
    done: Callable[[Dict[str, Any]], bool],
) -> None:
    device = select_device(ctx, device_name)
    ctx.confirm(f"{plan} on {device.get('name', 'SNOO')}")
    result = snoo_exchange(ctx, device, command, done)
    if result and done(result):
        click.echo(f"✓ Done. SNOO is now: {describe_state(result)}")
    else:
        click.echo("Command sent, but the SNOO did not confirm the change in time.", err=True)
        click.echo(f"  Last reported state: {describe_state(result)}", err=True)
        sys.exit(3)


def _state(data: Dict[str, Any]) -> Dict[str, Any]:
    return data.get("state_machine", {})


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
@click.option("--timezone", default="America/New_York", show_default=True, help="Timezone for times you enter and see")
@click.option("--yes", "-y", is_flag=True, help="Don't ask for confirmation before changing anything")
@click.option("--dry-run", is_flag=True, help="Show what would change, then exit without changing it")
@click.pass_context
def cli(click_ctx, email, password, refresh_token, credentials_file, timezone, yes, dry_run):
    """Control your SNOO and log journal entries without the Happiest Baby app."""
    creds = {
        "email": email,
        "password": password,
        "refresh_token": refresh_token,
        "credentials_file": credentials_file,
    }
    click_ctx.obj = Context(creds, timezone, yes, dry_run)


# --- SNOO ------------------------------------------------------------------


@cli.group()
def snoo():
    """Start, stop, and change the level of a SNOO."""


device_option = click.option("--device", help="SNOO name or serial number (needed only with more than one SNOO)")


@snoo.command("devices")
@click.pass_obj
def snoo_devices(ctx: Context):
    """List the SNOOs on this account."""
    for d in fetch_devices(ctx):
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
    result = snoo_exchange(ctx, dev, None, lambda data: True, timeout=15)
    if result is None:
        click.echo("Error: The SNOO did not report its state; it may be offline", err=True)
        sys.exit(3)
    click.echo(json.dumps(result, indent=2) if as_json else f"SNOO is: {describe_state(result)}")


@snoo.command("start")
@device_option
@click.pass_obj
def snoo_start(ctx: Context, device: Optional[str]):
    """Start the SNOO at its baseline level."""
    run_snoo_command(ctx, device, "Start the SNOO", {"command": "start_snoo"},
                     lambda d: _state(d).get("state") not in (None, "ONLINE"))


@snoo.command("stop")
@device_option
@click.pass_obj
def snoo_stop(ctx: Context, device: Optional[str]):
    """Stop the SNOO."""
    run_snoo_command(ctx, device, "Stop the SNOO", {"command": "go_to_state", "state": "ONLINE", "hold": "off"},
                     lambda d: _state(d).get("state") == "ONLINE")


@snoo.command("level")
@click.argument("level", type=click.Choice(list(LEVELS), case_sensitive=False))
@click.option("--hold/--no-hold", default=False, show_default=True, help="Keep the SNOO at this level")
@device_option
@click.pass_obj
def snoo_level(ctx: Context, level: str, hold: bool, device: Optional[str]):
    """Set the SNOO to LEVEL (baseline, 1, 2, 3, 4), starting it if stopped."""
    target = LEVELS[level.lower()]
    hold_value = "on" if hold else "off"
    run_snoo_command(
        ctx, device, f"Set the SNOO to {target} with hold {hold_value}",
        {"command": "go_to_state", "state": target, "hold": hold_value},
        lambda d: _state(d).get("state") == target and _state(d).get("hold", hold_value) == hold_value,
    )


@snoo.command("white-noise")
@click.argument("setting", type=click.Choice(["on", "off"], case_sensitive=False))
@click.option("--minutes", type=click.Choice(["15", "30", "60"]), default="15", show_default=True,
              help="Turn sticky white noise off after this long")
@device_option
@click.pass_obj
def snoo_white_noise(ctx: Context, setting: str, minutes: str, device: Optional[str]):
    """Turn sticky white noise (sound without motion) on or off."""
    setting = setting.lower()
    plan = f"Turn sticky white noise {setting}" + (f" for {minutes} minutes" if setting == "on" else "")
    run_snoo_command(
        ctx, device, plan,
        {"command": "set_sticky_white_noise", "state": setting, "timeout_min": int(minutes)},
        lambda d: _state(d).get("sticky_white_noise") == setting,
    )


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
    to_date = datetime.now(ZoneInfo("UTC"))
    from_date = to_date - timedelta(days=days)
    if journal_type == "pumping":
        entries = exporter.fetch_pumping_journals(from_date, to_date, ctx.token)
    else:
        baby_id = exporter.select_baby(ctx.token, baby_name)["_id"]
        entries = exporter.fetch_journals(baby_id, journal_type, from_date, to_date, ctx.token)
    entries.sort(key=lambda e: e.get("startTime", ""), reverse=True)
    click.echo(json.dumps(entries[:limit], indent=2))


def create_entry(ctx: Context, baby_name: Optional[str], journal_type: str, data: Dict[str, Any],
                 at: datetime, end_at: Optional[datetime], note: Optional[str], plan: str) -> None:
    payload: Dict[str, Any] = {"type": journal_type, "data": data, "startTime": api_time(at)}
    if end_at:
        payload["endTime"] = api_time(end_at)
    if note:
        payload["note"] = note

    if journal_type == "pumping":
        endpoint = PUMPING_CREATE_ENDPOINT
        who = ""
    else:
        baby = exporter.select_baby(ctx.token, baby_name)
        payload["babyId"] = baby["_id"]
        endpoint = JOURNAL_CREATE_ENDPOINT
        who = f" for {baby.get('babyName', 'baby')}"

    ctx.confirm(f"{plan}{who} at {show_time(at, ctx.tz)}" + (f' with note "{note}"' if note else ""))
    created = api_request(ctx, "POST", endpoint, json=payload)
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
    create_entry(ctx, baby_name, "diaper", {"types": types}, parse_time(at, ctx.tz), None, note,
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
    start = parse_time(at, ctx.tz)
    end = parse_time(end_at, ctx.tz) if end_at else None
    if end and end < start:
        raise click.BadParameter("--end-at is before --at")
    create_entry(ctx, baby_name, journal_type, data, start, end, note,
                 f"Log a {journal_type} entry with data {json.dumps(data)}")


@journal.command("delete")
@click.argument("journal_id")
@click.pass_obj
def journal_delete(ctx: Context, journal_id: str):
    """EXPERIMENTAL: delete the entry with JOURNAL_ID (find ids with `journal recent`)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", journal_id):
        raise click.BadParameter("That doesn't look like a journal id")
    ctx.confirm(f"Delete journal entry {journal_id}")
    api_request(ctx, "DELETE", JOURNAL_ITEM_ENDPOINT.format(journal_id=journal_id))
    click.echo("✓ Deleted")


if __name__ == "__main__":
    cli()
