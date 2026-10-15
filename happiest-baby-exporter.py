#!/usr/bin/env -S uv run
# /// script
# dependencies = [
#   "requests>=2.32.0",
#   "click>=8.1.0",
# ]
# requires-python = ">=3.12"
# ///
"""
Export journal data from Happiest Baby API to CSV files.

Needs the happiest_baby package that sits next to this script.
"""

import csv
import json
import os
import select
import sys
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import happiest_baby as hb  # noqa: E402


def _exit_on_error(e: hb.HappiestBabyError, action: str) -> None:
    click.echo(f"Error: {action} failed - {e}", err=True)
    sys.exit(1)


def authenticate(email: str, password: str) -> Dict[str, Any]:
    """Authenticate with email/password; returns IdToken, RefreshToken, etc."""
    try:
        return hb.authenticate_password(email, password)
    except hb.HappiestBabyError as e:
        _exit_on_error(e, "Sign-in")


def authenticate_with_refresh_token(refresh_token: str) -> str:
    """Exchange a refresh token for a fresh IdToken."""
    try:
        result = hb.refresh_session(refresh_token)
    except hb.AuthError as e:
        click.echo(f"Error: Sign-in failed - {e}", err=True)
        click.echo("  The refresh token is expired or revoked; generate a new one with build_skill.py", err=True)
        sys.exit(1)
    except hb.HappiestBabyError as e:
        _exit_on_error(e, "Sign-in")
    if "RefreshToken" in result:
        # The stored token can't be updated (e.g. it's read-only inside a skill), so
        # once the old token's grace period ends, later runs will fail.
        click.echo(
            "Warning: Cognito rotated the refresh token; the stored token will stop working soon. "
            "Rebuild credentials with build_skill.py if authentication starts failing.",
            err=True,
        )
    return result["IdToken"]


def revoke_refresh_token(refresh_token: str) -> None:
    """Revoke a refresh token so it can no longer be used."""
    try:
        hb.revoke_refresh_token(refresh_token)
    except hb.HappiestBabyError as e:
        _exit_on_error(e, "Revoking the refresh token")


def refresh_token_from_json(raw: str | bytes, source: str) -> str:
    """Extract refresh_token from credentials JSON, exiting with a clear error if malformed."""
    try:
        token = json.loads(raw)["refresh_token"]
        if not isinstance(token, str) or not token:
            raise ValueError("refresh_token must be a non-empty string")
        return token
    except (ValueError, KeyError, TypeError) as e:
        click.echo(f"Error: Could not read refresh token from {source} - {e}", err=True)
        sys.exit(1)


def load_refresh_token(credentials_file: Path) -> str:
    """Read the refresh token from a credentials JSON file."""
    try:
        raw = credentials_file.read_text()
    except OSError as e:
        click.echo(f"Error: Could not read refresh token from {credentials_file} - {e}", err=True)
        sys.exit(1)
    return refresh_token_from_json(raw, str(credentials_file))


def fetch_journals(
    client: hb.HappiestBabyClient, baby_id: str, journal_type: str, from_date: datetime, to_date: datetime
) -> List[Dict[str, Any]]:
    """Fetch journals of a specific type for a date range."""
    click.echo(f"Fetching {journal_type} journals from {from_date.date()} to {to_date.date()}...")
    data = client.journals(baby_id, journal_type, from_date, to_date)
    click.echo(f"  Retrieved {len(data)} entries")
    return data


def fetch_pumping_journals(client: hb.HappiestBabyClient, from_date: datetime, to_date: datetime) -> List[Dict[str, Any]]:
    """Fetch pumping journals for a date range."""
    click.echo(f"Fetching pumping journals from {from_date.date()} to {to_date.date()}...")
    data = client.pumping_journals(from_date, to_date)
    click.echo(f"  Retrieved {len(data)} entries")
    return data


def fetch_sleep_sessions(
    client: hb.HappiestBabyClient, baby_id: str, from_date: datetime, to_date: datetime, timezone: str
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Fetch sleep for a date range: the raw per-level rows, and one consolidated row per session."""
    click.echo(f"Fetching sleep sessions from {from_date.date()} to {to_date.date()}...")
    levels, detailed = client.sleep_levels(baby_id, from_date, to_date, timezone)
    click.echo(f"  Retrieved {len(levels)} entries")
    return levels, consolidate_sleep_sessions(hb.consolidate_sleep_levels(levels, detailed))


def consolidate_sleep_sessions(sessions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One CSV row per session: start, total seconds, the states it went through, and its source."""
    rows = []
    for session in sessions:
        start = session["startTime"]  # e.g. 2026-10-12T14:50:00-0400
        rows.append({
            "sessionId": session["sessionId"],
            "startTime": f"{start[:10]} {start[11:19]}",
            "startTimeFormatted": start,
            "stateDuration": session["totalSeconds"],
            "type": ",".join(session["secondsByType"]),
            "source": session["source"],
            "label": session.get("label", ""),
            "note": session.get("note", ""),
        })
    return rows


def get_id_token(
    email: Optional[str],
    password: Optional[str],
    refresh_token: Optional[str],
    credentials_file: Optional[Path],
) -> str:
    """Authenticate from whichever credentials were given and return an IdToken."""
    if refresh_token and credentials_file:
        click.echo(
            "Error: Both a refresh token (--refresh-token / HAPPIESTBABY_REFRESH_TOKEN) and "
            "--credentials-file were given; use only one",
            err=True,
        )
        sys.exit(1)

    # Progress goes to stderr so callers printing JSON (happiest-baby-actions.py) keep stdout clean
    click.echo("Authenticating with Happiest Baby...", err=True)
    if credentials_file:
        click.echo(f"  Using refresh token from {credentials_file}", err=True)
        token = authenticate_with_refresh_token(load_refresh_token(credentials_file))
    elif refresh_token:
        click.echo("  Using refresh token from --refresh-token / HAPPIESTBABY_REFRESH_TOKEN", err=True)
        token = authenticate_with_refresh_token(refresh_token)
    else:
        no_credentials = (
            "Error: No credentials provided; pass --credentials-file, --refresh-token, "
            "or --email and --password"
        )
        # Prompt on a terminal, or read answers piped to stdin; fail fast if nothing can answer
        if not (email and password) and not sys.stdin.isatty() and not _stdin_has_data():
            click.echo(no_credentials, err=True)
            sys.exit(1)
        try:
            email = email or click.prompt("Email", err=True)
            password = password or click.prompt("Password", hide_input=True, err=True)
        except click.Abort:  # stdin ended before both answers
            click.echo(f"\n{no_credentials}", err=True)
            sys.exit(1)
        token = authenticate(email, password)["IdToken"]
    click.echo("✓ Authentication successful", err=True)
    return token


def _stdin_has_data(wait: float = 1.0) -> bool:
    """Whether non-terminal stdin has input (or EOF) to read, waiting briefly for a pipe to fill."""
    try:
        return bool(select.select([sys.stdin], [], [], wait)[0])
    except (OSError, ValueError):  # e.g. Windows, where select() doesn't take pipes
        return False


def select_baby(client: hb.HappiestBabyClient, baby_name: Optional[str]) -> Dict[str, Any]:
    """Pick a baby by name, the only baby, or an interactive choice; exits if ambiguous."""
    click.echo("Fetching babies from API...")
    try:
        babies = client.babies()
    except hb.HappiestBabyError as e:
        _exit_on_error(e, "Fetching babies")

    if not babies:
        click.echo("Error: No babies found in your account", err=True)
        sys.exit(1)

    if baby_name is not None:
        # Find baby by name
        matching_babies = [b for b in babies if b.get("babyName", "").lower() == baby_name.lower()]

        if not matching_babies:
            click.echo(f"Error: No baby found with name '{baby_name}'", err=True)
            click.echo("Available babies:", err=True)
            for baby in babies:
                click.echo(f"  - {baby.get('babyName', 'Unknown')}", err=True)
            sys.exit(1)

        baby = matching_babies[0]
        click.echo(f"✓ Using baby: {baby.get('babyName', 'Unknown')}")
    elif len(babies) == 1:
        baby = babies[0]
        click.echo(f"✓ Using baby: {baby.get('babyName', 'Unknown')}")
    elif not sys.stdin.isatty():
        click.echo("Error: Multiple babies found; pass --baby-name with one of:", err=True)
        for baby in babies:
            click.echo(f"  - {baby.get('babyName', 'Unknown')}", err=True)
        sys.exit(1)
    else:
        click.echo(f"Found {len(babies)} babies:")
        for i, baby in enumerate(babies, 1):
            name = baby.get("babyName", "Unknown")
            click.echo(f"  {i}. {name}")

        choice = click.prompt("Select baby number", type=int)
        if choice < 1 or choice > len(babies):
            click.echo("Error: Invalid selection", err=True)
            sys.exit(1)

        baby = babies[choice - 1]
        click.echo(f"✓ Selected baby: {baby.get('babyName', 'Unknown')}")

    return baby


def flatten_dict(d: Dict[str, Any], parent_key: str = "", sep: str = "_", preserve_lists: bool = False) -> Dict[str, Any]:
    """Flatten a nested dictionary."""
    items = []
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_dict(v, new_key, sep=sep, preserve_lists=preserve_lists).items())
        elif isinstance(v, list):
            if preserve_lists:
                # Keep lists as-is for later processing
                items.append((new_key, v))
            else:
                # Convert lists to JSON strings for CSV compatibility
                items.append((new_key, json.dumps(v)))
        else:
            items.append((new_key, v))
    return dict(items)


def clean_journal_entry(
    entry: Dict[str, Any],
    journal_type: str,
    units: str,
    baby_id: Optional[str] = None,
    baby_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Clean and transform a journal entry according to export rules."""
    cleaned = {}

    # Add babyName for all journal types except pumping (if we have it)
    if journal_type != "pumping" and baby_name:
        cleaned["babyName"] = baby_name

    for key, value in entry.items():
        # Skip id, userId, and isActive fields
        if key in ("id", "userId", "isActive"):
            continue

        # Replace babyId with babyName (skip the babyId field entirely since we added babyName above)
        if key == "babyId":
            continue

        # Remove 'data_' prefix
        if key.startswith("data_"):
            key = key[5:]  # Remove 'data_' prefix

        # Handle units - keep only imperial or metric
        if units == "imperial":
            # Skip metric fields
            if key.endswith("Metric") or key.endswith("_ml") or "metric" in key.lower():
                continue
        else:  # metric
            # Skip imperial fields
            if key.endswith("Imperial") or key.endswith("_oz") or "imperial" in key.lower():
                continue

        # Special handling for diaper types field
        if journal_type == "diaper" and key == "types" and isinstance(value, list):
            # Convert list to comma-separated string
            value = ",".join(value)
        elif isinstance(value, list):
            # For any other lists, convert to JSON string
            value = json.dumps(value)

        cleaned[key] = value

    return cleaned


def order_fields(fieldnames: List[str]) -> List[str]:
    """Order fields with babyName first, time fields next, and audit fields last."""
    baby_name_field = []
    time_fields = []
    audit_fields = []
    other_fields = []

    for field in fieldnames:
        if field == "babyName":
            baby_name_field.append(field)
        elif field in ("startTime", "endTime"):
            time_fields.append(field)
        elif field in ("createdAt", "updatedAt"):
            audit_fields.append(field)
        else:
            other_fields.append(field)

    # Order time fields: startTime first, then endTime
    if "startTime" in time_fields and "endTime" in time_fields:
        time_fields = ["startTime", "endTime"]
    elif "startTime" in time_fields:
        time_fields = ["startTime"]
    elif "endTime" in time_fields:
        time_fields = ["endTime"]

    other_fields.sort()
    audit_fields.sort()  # createdAt, updatedAt alphabetically

    return baby_name_field + time_fields + other_fields + audit_fields


def export_to_csv(
    journals: List[Dict[str, Any]],
    output_file: str,
    journal_type: str,
    units: str,
    baby_id: Optional[str] = None,
    baby_name: Optional[str] = None,
) -> None:
    """Export journal data to CSV file."""
    if not journals:
        click.echo(f"  No data to export to {output_file}")
        return

    # Flatten all dictionaries, preserving lists for diaper type
    preserve_lists = (journal_type == "diaper")
    flattened = [flatten_dict(journal, preserve_lists=preserve_lists) for journal in journals]

    # Clean entries
    cleaned = [clean_journal_entry(entry, journal_type, units, baby_id, baby_name) for entry in flattened]

    # Get all unique keys across all entries
    all_keys = set()
    for entry in cleaned:
        all_keys.update(entry.keys())

    # Order fieldnames
    fieldnames = order_fields(list(all_keys))

    with open(output_file, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(cleaned)

    click.echo(f"  Exported to {output_file}")


def create_consolidated_entry(
    entry: Dict[str, Any],
    journal_type: str,
    baby_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Convert a journal entry to consolidated format."""
    # For sleep, use startTimeFormatted if available for consistent formatting
    start_time = entry.get("startTimeFormatted") if journal_type == "sleep" else entry.get("startTime", "")
    if not start_time:
        start_time = entry.get("startTime", "")

    consolidated = {
        "babyName": baby_name or entry.get("babyName", ""),
        "startTime": start_time,
        "journalType": journal_type,
        "type": entry.get("type", ""),
        "amount_left": None,
        "amount_right": None,
        "amount_total": None,
        "duration_left": None,
        "duration_right": None,
        "duration_total": None,
    }

    if journal_type == "breastfeeding":
        # Duration in seconds
        consolidated["duration_left"] = entry.get("left_duration")
        consolidated["duration_right"] = entry.get("right_duration")
        consolidated["duration_total"] = entry.get("totalDuration")

    elif journal_type == "bottlefeeding":
        # Amount in oz (imperial)
        consolidated["amount_total"] = entry.get("amountImperial")

    elif journal_type == "pumping":
        # Amount in oz (imperial), calculate total
        left_amt = entry.get("left_amountImperial")
        right_amt = entry.get("right_amountImperial")
        consolidated["amount_left"] = left_amt
        consolidated["amount_right"] = right_amt
        if left_amt is not None and right_amt is not None:
            # Round to 2 decimal places to avoid floating point precision issues
            consolidated["amount_total"] = round(left_amt + right_amt, 2)
        elif left_amt is not None:
            consolidated["amount_total"] = left_amt
        elif right_amt is not None:
            consolidated["amount_total"] = right_amt

        # Duration in seconds
        consolidated["duration_total"] = entry.get("duration")

    elif journal_type == "sleep":
        # Duration in seconds
        consolidated["duration_total"] = entry.get("stateDuration")

    elif journal_type == "diaper":
        # Diaper has type in "types" field (comma-separated)
        consolidated["type"] = entry.get("types", "")

    return consolidated


def consolidate_all_journals(
    breastfeeding: List[Dict[str, Any]],
    bottlefeeding: List[Dict[str, Any]],
    diaper: List[Dict[str, Any]],
    pumping: List[Dict[str, Any]],
    sleep: List[Dict[str, Any]],
    baby_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Consolidate all journal types into a single unified format."""
    consolidated = []

    for entry in breastfeeding:
        consolidated.append(create_consolidated_entry(entry, "breastfeeding", baby_name))

    for entry in bottlefeeding:
        consolidated.append(create_consolidated_entry(entry, "bottlefeeding", baby_name))

    for entry in diaper:
        consolidated.append(create_consolidated_entry(entry, "diaper", baby_name))

    for entry in pumping:
        consolidated.append(create_consolidated_entry(entry, "pumping", baby_name))

    for entry in sleep:
        consolidated.append(create_consolidated_entry(entry, "sleep", baby_name))

    # Sort by start time
    consolidated.sort(key=lambda x: x.get("startTime", ""))

    return consolidated


def extract_baby_id_from_logs(log_file: str) -> str:
    """Extract baby_id from request logs."""
    log_path = Path(log_file)
    if not log_path.exists():
        click.echo(f"Error: Request log file '{log_file}' not found", err=True)
        sys.exit(1)

    with open(log_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            url = row.get("URL", "")
            if "/babies/" in url and "/journals/" in url:
                # Extract baby_id from URL
                parts = url.split("/babies/")[1].split("/")
                return parts[0]

    click.echo("Error: Could not find baby_id in request logs", err=True)
    sys.exit(1)


@click.command()
@click.option(
    "--email",
    envvar="HAPPIESTBABY_EMAIL",
    help="Email address (can use HAPPIESTBABY_EMAIL env var; interactive prompt if no other credentials are provided)",
)
@click.option(
    "--password",
    envvar="HAPPIESTBABY_PASSWORD",
    help="Password (can use HAPPIESTBABY_PASSWORD env var; interactive prompt if no other credentials are provided)",
)
@click.option(
    "--refresh-token",
    envvar="HAPPIESTBABY_REFRESH_TOKEN",
    help="Refresh token to use instead of email/password (can use HAPPIESTBABY_REFRESH_TOKEN env var)",
)
@click.option(
    "--credentials-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="JSON file containing a refresh_token (as created by build_skill.py)",
)
@click.option(
    "--baby-name",
    help="Baby name to fetch data for (if not provided, interactive selection will be provided, unless there is only a single baby)",
)
@click.option(
    "--lookback-days",
    type=int,
    default=14,
    help="Number of days to look back from today",
    show_default=True,
)
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=".",
    help="Directory to save CSV files",
    show_default=True,
)
@click.option(
    "--journal-type",
    help="Comma-separated list of journal types to export (breastfeeding,bottlefeeding,diaper,solidfood,pumping,sleep; if not specified, exports all types)",
)
@click.option(
    "--units",
    type=click.Choice(["imperial", "metric"], case_sensitive=False),
    default="imperial",
    help="Unit system to use for measurements",
    show_default=True,
)
@click.option(
    "--timezone",
    default="America/New_York",
    help="Timezone for sleep session data",
    show_default=True,
)
def main(
    email: Optional[str],
    password: Optional[str],
    refresh_token: Optional[str],
    credentials_file: Optional[Path],
    baby_name: Optional[str],
    lookback_days: int,
    output_dir: Path,
    journal_type: Optional[str],
    units: str,
    timezone: str,
) -> None:
    """Export journal data from Happiest Baby API to CSV files.

    Each journal type will be exported to a separate CSV file named
    'journals_<type>.csv' in the output directory.
    """
    client = hb.HappiestBabyClient(get_id_token(email, password, refresh_token, credentials_file))
    click.echo()

    # Determine journal types to export
    if journal_type:
        # Parse comma-separated list
        valid_types = ["breastfeeding", "bottlefeeding", "diaper", "solidfood", "pumping", "sleep"]
        journal_types = [t.strip().lower() for t in journal_type.split(",")]

        # Validate types
        invalid_types = [t for t in journal_types if t not in valid_types]
        if invalid_types:
            click.echo(f"Error: Invalid journal type(s): {', '.join(invalid_types)}", err=True)
            click.echo(f"Valid types: {', '.join(valid_types)}", err=True)
            sys.exit(1)
    else:
        journal_types = ["breastfeeding", "bottlefeeding", "diaper", "solidfood", "pumping", "sleep"]

    # Get baby_id only if needed (not needed for pumping journals only)
    needs_baby_id = any(jtype != "pumping" for jtype in journal_types)
    baby_id = None
    selected_baby_name = None

    if needs_baby_id:
        baby = select_baby(client, baby_name)
        baby_id = baby["_id"]
        selected_baby_name = baby.get("babyName", "Unknown")

    click.echo()

    # Set date range
    to_date = datetime.now(dt_timezone.utc)
    from_date = to_date - timedelta(days=lookback_days)

    click.echo(f"Fetching data from {from_date.date()} to {to_date.date()}")
    click.echo()

    # Create output directory if it doesn't exist
    output_dir.mkdir(parents=True, exist_ok=True)

    # Store journals for consolidated export
    all_journals = {
        "breastfeeding": [],
        "bottlefeeding": [],
        "diaper": [],
        "pumping": [],
        "sleep": [],
    }

    # Export each journal type
    for jtype in journal_types:
        try:
            if jtype == "sleep":
                levels, consolidated = fetch_sleep_sessions(client, baby_id, from_date, to_date, timezone)

                # Export detailed sleep data
                detailed_output = output_dir / "journals_sleep_detailed.csv"
                export_to_csv(levels, str(detailed_output), jtype, units, baby_id, selected_baby_name)

                # Export consolidated sleep data
                consolidated_output = output_dir / "journals_sleep.csv"
                export_to_csv(consolidated, str(consolidated_output), jtype, units, baby_id, selected_baby_name)

                # Store consolidated sleep for all-journals export
                all_journals["sleep"] = consolidated
            else:
                if jtype == "pumping":
                    journals = fetch_pumping_journals(client, from_date, to_date)
                else:
                    journals = fetch_journals(client, baby_id, jtype, from_date, to_date)
                output_file = output_dir / f"journals_{jtype}.csv"
                export_to_csv(journals, str(output_file), jtype, units, baby_id, selected_baby_name)

                # Store journals for all-journals export
                if jtype in all_journals:
                    all_journals[jtype] = journals

            click.echo()
        except hb.HappiestBabyError as e:
            click.echo(f"  Error fetching {jtype}: {e}", err=True)
            click.echo()
        except Exception as e:
            click.echo(f"  Unexpected error for {jtype}: {e}", err=True)
            click.echo()

    # Export consolidated journal with all types
    click.echo("Creating consolidated journal...")

    # Clean and flatten all journal entries
    cleaned_journals = {}
    for jtype, journals in all_journals.items():
        if journals:
            preserve_lists = (jtype == "diaper")
            flattened = [flatten_dict(journal, preserve_lists=preserve_lists) for journal in journals]
            cleaned = [clean_journal_entry(entry, jtype, units, baby_id, selected_baby_name) for entry in flattened]
            cleaned_journals[jtype] = cleaned
        else:
            cleaned_journals[jtype] = []

    consolidated_all = consolidate_all_journals(
        breastfeeding=cleaned_journals.get("breastfeeding", []),
        bottlefeeding=cleaned_journals.get("bottlefeeding", []),
        diaper=cleaned_journals.get("diaper", []),
        pumping=cleaned_journals.get("pumping", []),
        sleep=cleaned_journals.get("sleep", []),
        baby_name=selected_baby_name,
    )

    if consolidated_all:
        consolidated_file = output_dir / "journals_consolidated.csv"
        fieldnames = [
            "babyName", "startTime", "journalType", "type",
            "amount_left", "amount_right", "amount_total",
            "duration_left", "duration_right", "duration_total"
        ]

        with open(consolidated_file, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(consolidated_all)

        click.echo(f"  Exported {len(consolidated_all)} entries to {consolidated_file}")
        click.echo()

    click.echo("Export complete!")


if __name__ == "__main__":
    main()
