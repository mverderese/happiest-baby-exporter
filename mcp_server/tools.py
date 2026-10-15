"""MCP tools. Each call uses the Cognito IdToken carried in the caller's access token."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

import happiest_baby as hb

INSTRUCTIONS = """\
Controls a SNOO smart bassinet and reads/writes the Happiest Baby baby journal for the
signed-in parent. They use this when the Happiest Baby app isn't working, often at night:
be brief and state the outcome plainly ("SNOO is now: LEVEL2, hold on").

- Before a write, if you inferred anything the user didn't say (which baby or SNOO, the
  time, pee vs. poo, the level), confirm it with them first.
- Never repeat a write that failed or timed out without first checking state with
  snoo_status or get_journal_entries, so nothing happens twice.
- Always pass on a "safety clip not attached" warning.
- log_journal_entry is experimental: first call get_journal_entries for that type and copy
  the shape of an existing entry's "data" object exactly. If there are none to copy, say
  you can't log that type reliably yet instead of guessing.
- Times: prefer ISO 8601 with the user's UTC offset. "now", "15m", "2h", and "HH:MM" are
  also accepted and read in the timezone argument (the server default if omitted).
"""

mcp = MCPServer(name="Happiest Baby", instructions=INSTRUCTIONS)
DEFAULT_TIMEZONE = "America/New_York"  # replaced from Settings in app.py

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
SNOO_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)
JOURNAL_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
DELETE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)


def _id_token() -> str:
    token = get_access_token()
    id_token = getattr(token, "id_token", None)
    if not id_token:
        raise ToolError("Not signed in; reconnect the Happiest Baby connector")
    return id_token


def _client() -> hb.HappiestBabyClient:
    return hb.HappiestBabyClient(_id_token())


def _tz(name: Optional[str]) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        raise ToolError(f"Unknown timezone '{name}'; use an IANA name like America/Chicago") from None


def _time(value: Optional[str], tz: ZoneInfo) -> datetime:
    try:
        return hb.parse_time(value, tz)
    except ValueError as e:
        raise ToolError(str(e)) from None


def _call(fn, *args, **kwargs):
    """Run a client call, turning library errors into tool errors the model can relay."""
    try:
        return fn(*args, **kwargs)
    except hb.SnooNotConfirmed as e:
        raise ToolError(str(e)) from None
    except hb.AuthError:
        raise ToolError("Happiest Baby rejected the sign-in; reconnect the connector to sign in again") from None
    except hb.HappiestBabyError as e:
        raise ToolError(str(e)) from None
    except ValueError as e:
        raise ToolError(str(e)) from None


def _snoo_result(device: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    sm = state.get("state_machine", {})
    return {
        "snoo": device.get("name"),
        "summary": hb.describe_state(state),
        "level": sm.get("state"),
        "hold": sm.get("hold"),
        "seconds_left_at_level": sm.get("time_left"),
        "sticky_white_noise": sm.get("sticky_white_noise"),
        "active_session": sm.get("is_active_session"),
        "safety_clips_attached": {"left": state.get("left_safety_clip"), "right": state.get("right_safety_clip")},
    }


def _snoo_action(device: Optional[str], action: str, **options: Any) -> Dict[str, Any]:
    id_token = _id_token()
    dev = _call(hb.HappiestBabyClient(id_token).select_device, device)
    state = _call(hb.snoo_command, id_token, dev, action, **options)
    return _snoo_result(dev, state)


# --- SNOO -----------------------------------------------------------------------


@mcp.tool(annotations=READ)
def list_snoos() -> List[Dict[str, Any]]:
    """List the SNOO bassinets on the account with name, serial number, firmware, and online status."""
    devices = _call(_client().devices)
    return [
        {
            "name": d.get("name"),
            "serial": d.get("serialNumber"),
            "firmware": d.get("firmwareVersion"),
            "online": (d.get("presenceIoT") or d.get("presence") or {}).get("online"),
        }
        for d in devices
    ]


@mcp.tool(annotations=READ)
def snoo_status(device: Optional[str] = None) -> Dict[str, Any]:
    """Get the SNOO's current level, hold, time left at this level, white noise, and safety clip status.

    device: SNOO name or serial; only needed if the account has more than one.
    """
    id_token = _id_token()
    dev = _call(hb.HappiestBabyClient(id_token).select_device, device)
    return _snoo_result(dev, _call(hb.snoo_status, id_token, dev))


@mcp.tool(annotations=SNOO_WRITE)
def snoo_start(device: Optional[str] = None) -> Dict[str, Any]:
    """Start the SNOO at its baseline level. Waits for the SNOO to confirm, then returns its state."""
    return _snoo_action(device, "start")


@mcp.tool(annotations=SNOO_WRITE)
def snoo_stop(device: Optional[str] = None) -> Dict[str, Any]:
    """Stop the SNOO. Waits for the SNOO to confirm, then returns its state."""
    return _snoo_action(device, "stop")


@mcp.tool(annotations=SNOO_WRITE)
def snoo_set_level(
    level: Literal["baseline", "1", "2", "3", "4"],
    hold: bool = False,
    device: Optional[str] = None,
) -> Dict[str, Any]:
    """Set the SNOO's motion and sound level, starting it if it's stopped.

    hold: keep the SNOO at this level instead of letting it step down on its own.
    Waits for the SNOO to confirm, then returns its state.
    """
    return _snoo_action(device, "level", level=level, hold=hold)


@mcp.tool(annotations=SNOO_WRITE)
def snoo_white_noise(on: bool, minutes: Literal[15, 30, 60] = 15, device: Optional[str] = None) -> Dict[str, Any]:
    """Turn sticky white noise (sound without motion) on or off; it turns itself off after `minutes`."""
    return _snoo_action(device, "white_noise", on=on, minutes=minutes)


# --- Journal and sleep -------------------------------------------------------------------


def _baby(client: hb.HappiestBabyClient, baby_name: Optional[str]) -> Dict[str, Any]:
    return _call(client.select_baby, baby_name)


@mcp.tool(annotations=READ)
def get_journal_entries(
    journal_type: Literal["breastfeeding", "bottlefeeding", "diaper", "solidfood", "pumping"],
    hours: int = 24,
    limit: int = 20,
    baby_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Journal entries of one type from the last `hours` hours, newest first, as the API returns them
    (including each entry's id and its `data` object). Durations are in seconds."""
    if not 1 <= hours <= 24 * 31:
        raise ToolError("hours must be between 1 and 744")
    client = _client()
    to_date = datetime.now(timezone.utc)
    from_date = to_date - timedelta(hours=hours)
    if journal_type == "pumping":
        entries = _call(client.pumping_journals, from_date, to_date)
    else:
        entries = _call(client.journals, _baby(client, baby_name)["_id"], journal_type, from_date, to_date)
    entries.sort(key=lambda e: e.get("startTime", ""), reverse=True)
    return entries[:max(1, limit)]


@mcp.tool(annotations=READ)
def get_sleep_sessions(hours: int = 24, baby_name: Optional[str] = None, timezone_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """Sleep sessions overlapping the last `hours` hours: start time, total seconds, and seconds per
    state (e.g. asleep, soothing). source is "snoo", or "manual" for sleep logged in the app outside
    the SNOO, which may carry a label (the app's tag, e.g. in_arms) and a free-text note."""
    if not 1 <= hours <= 24 * 31:
        raise ToolError("hours must be between 1 and 744")
    tz = _tz(timezone_name)
    client = _client()
    baby = _baby(client, baby_name)
    to_date = datetime.now(tz)
    from_date = to_date - timedelta(hours=hours)
    return _call(client.sleep_sessions, baby["_id"], from_date, to_date, str(tz))


def _create(client: hb.HappiestBabyClient, journal_type: str, data: Dict[str, Any], baby_name: Optional[str],
            start: datetime, end: Optional[datetime], note: Optional[str], tz: ZoneInfo) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"type": journal_type, "data": data, "startTime": hb.api_time(start)}
    if end:
        payload["endTime"] = hb.api_time(end)
    if note:
        payload["note"] = note
    who = None
    if journal_type != "pumping":
        baby = _baby(client, baby_name)
        payload["babyId"] = baby["_id"]
        who = baby.get("babyName")
    created = _call(client.create_journal, payload)
    entry_id = (created.get("id") or created.get("_id")) if isinstance(created, dict) else None
    return {"logged": journal_type, "baby": who, "at": hb.show_time(start, tz), "id": entry_id, "data": data}


@mcp.tool(annotations=JOURNAL_WRITE)
def log_diaper(
    pee: bool,
    poo: bool,
    time: str = "now",
    note: Optional[str] = None,
    baby_name: Optional[str] = None,
    timezone_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Log a diaper change. Set pee and/or poo (at least one). time: ISO 8601 with offset,
    or now, 15m, 2h, HH:MM in timezone_name."""
    types = [t for t, flag in (("pee", pee), ("poo", poo)) if flag]
    if not types:
        raise ToolError("Set pee, poo, or both")
    tz = _tz(timezone_name)
    return _create(_client(), "diaper", {"types": types}, baby_name, _time(time, tz), None, note, tz)


@mcp.tool(annotations=JOURNAL_WRITE)
def log_journal_entry(
    journal_type: Literal["breastfeeding", "bottlefeeding", "solidfood", "pumping"],
    data: Dict[str, Any],
    time: str = "now",
    end_time: Optional[str] = None,
    note: Optional[str] = None,
    baby_name: Optional[str] = None,
    timezone_name: Optional[str] = None,
) -> Dict[str, Any]:
    """EXPERIMENTAL: log a feed, pumping session, or solid food. `data` must copy the shape of an
    existing entry's data object from get_journal_entries (same fields, nesting, and units).
    end_time is for entries with a duration."""
    tz = _tz(timezone_name)
    start = _time(time, tz)
    end = _time(end_time, tz) if end_time else None
    if end and end < start:
        raise ToolError("end_time is before time")
    return _create(_client(), journal_type, data, baby_name, start, end, note, tz)


@mcp.tool(annotations=DELETE)
def delete_journal_entry(journal_id: str) -> Dict[str, Any]:
    """EXPERIMENTAL: delete a journal entry by its id (from get_journal_entries)."""
    _call(_client().delete_journal, journal_id)
    return {"deleted": journal_id}
