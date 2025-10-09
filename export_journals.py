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
"""

import csv
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
import requests


def load_token(token_file: str) -> str:
    """Load authentication token from file."""
    token_path = Path(token_file)
    if not token_path.exists():
        click.echo(f"Error: Token file '{token_file}' not found", err=True)
        sys.exit(1)

    with open(token_path, "r") as f:
        token = f.read().strip()
        # Remove "Bearer " prefix if present
        if token.startswith("Bearer "):
            token = token[7:]  # Remove "Bearer " (7 characters)
        return token


def fetch_journals(
    baby_id: str,
    journal_type: str,
    from_date: datetime,
    to_date: datetime,
    token: str,
) -> List[Dict[str, Any]]:
    """Fetch journals of a specific type for a date range."""
    base_url = "https://api-us-east-1-prod.happiestbaby.com"
    endpoint = f"/cs/me/v11/babies/{baby_id}/journals/tracking"

    params = {
        "fromDateTime": from_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "toDateTime": to_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "journalType": journal_type,
    }

    headers = {
        "Authorization": f"Bearer {token}",
    }

    url = f"{base_url}{endpoint}"
    click.echo(f"Fetching {journal_type} journals from {from_date.date()} to {to_date.date()}...")

    response = requests.get(url, params=params, headers=headers, timeout=30)
    response.raise_for_status()

    data = response.json()
    click.echo(f"  Retrieved {len(data)} entries")
    return data


def fetch_pumping_journals(
    from_date: datetime,
    to_date: datetime,
    token: str,
) -> List[Dict[str, Any]]:
    """Fetch pumping journals for a date range."""
    base_url = "https://api-us-east-1-prod.happiestbaby.com"
    endpoint = "/cs/me/v11/pumping-journals/tracking"

    params = {
        "fromDateTime": from_date.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "toDateTime": to_date.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }

    headers = {
        "Authorization": f"Bearer {token}",
    }

    url = f"{base_url}{endpoint}"
    click.echo(f"Fetching pumping journals from {from_date.date()} to {to_date.date()}...")

    response = requests.get(url, params=params, headers=headers, timeout=30)
    response.raise_for_status()

    data = response.json()
    click.echo(f"  Retrieved {len(data)} entries")
    return data


def fetch_sleep_sessions(
    baby_id: str,
    from_date: datetime,
    to_date: datetime,
    token: str,
    timezone: str = "America/Los_Angeles",
) -> List[Dict[str, Any]]:
    """Fetch sleep sessions for a date range."""
    base_url = "https://api-us-east-1-prod.happiestbaby.com"
    endpoint = f"/ss/me/v11/babies/{baby_id}/sessions/daily"

    headers = {
        "Authorization": f"Bearer {token}",
    }

    url = f"{base_url}{endpoint}"
    click.echo(f"Fetching sleep sessions from {from_date.date()} to {to_date.date()}...")

    all_sessions = []
    current_date = from_date.replace(hour=6, minute=0, second=0, microsecond=0)

    # The API fetches sessions per day, so iterate through each day
    while current_date.date() <= to_date.date():
        params = {
            "detailedLevels": "true",
            "levels": "true",
            "startTime": current_date.strftime("%Y-%m-%d %H:%M:%S.000"),
            "timezone": timezone,
        }

        response = requests.get(url, params=params, headers=headers, timeout=30)
        response.raise_for_status()

        data = response.json()

        # The API returns a dict with 'levels' containing the actual session list
        if isinstance(data, dict) and 'levels' in data:
            sessions = data['levels']
        elif isinstance(data, list):
            sessions = data
        else:
            sessions = []

        all_sessions.extend(sessions)
        current_date += timedelta(days=1)

    click.echo(f"  Retrieved {len(all_sessions)} entries")
    return all_sessions


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
) -> Dict[str, Any]:
    """Clean and transform a journal entry according to export rules."""
    cleaned = {}

    for key, value in entry.items():
        # Skip id and userId fields
        if key in ("id", "userId"):
            continue

        # Remove 'data_' prefix
        if key.startswith("data_"):
            key = key[5:]  # Remove 'data_' prefix

        # Handle units - keep only imperial or metric
        if units == "imperial":
            # Skip metric fields
            if key.endswith("_ml") or key.endswith("_metric"):
                continue
        else:  # metric
            # Skip imperial fields
            if key.endswith("_oz") or key.endswith("_imperial"):
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
    """Order fields with time fields first and audit fields last."""
    time_fields = []
    audit_fields = []
    other_fields = []

    for field in fieldnames:
        if field in ("startTime", "endTime"):
            time_fields.append(field)
        elif field in ("createdAt", "updatedAt"):
            audit_fields.append(field)
        else:
            other_fields.append(field)

    # Sort within each group
    time_fields.sort()  # endTime, startTime alphabetically
    # But we want startTime first, then endTime
    if "startTime" in time_fields and "endTime" in time_fields:
        time_fields = ["startTime", "endTime"]

    other_fields.sort()
    audit_fields.sort()  # createdAt, updatedAt alphabetically

    return time_fields + other_fields + audit_fields


def export_to_csv(
    journals: List[Dict[str, Any]],
    output_file: str,
    journal_type: str,
    units: str,
) -> None:
    """Export journal data to CSV file."""
    if not journals:
        click.echo(f"  No data to export to {output_file}")
        return

    # Flatten all dictionaries, preserving lists for diaper type
    preserve_lists = (journal_type == "diaper")
    flattened = [flatten_dict(journal, preserve_lists=preserve_lists) for journal in journals]

    # Clean entries
    cleaned = [clean_journal_entry(entry, journal_type, units) for entry in flattened]

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
    "--token-file",
    default=".token",
    help="Path to file containing authentication token",
    show_default=True,
)
@click.option(
    "--request-log",
    default="request-logs.csv",
    help="Path to request log CSV file",
    show_default=True,
)
@click.option(
    "--baby-id",
    help="Baby ID (if not provided, will be extracted from request logs)",
)
@click.option(
    "--lookback-days",
    type=int,
    default=730,
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
    multiple=True,
    type=click.Choice(["breastfeeding", "bottlefeeding", "diaper", "solidfood", "pumping", "sleep"], case_sensitive=False),
    help="Journal type(s) to export (can be specified multiple times; if not specified, exports all types)",
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
    default="America/Los_Angeles",
    help="Timezone for sleep session data",
    show_default=True,
)
def main(
    token_file: str,
    request_log: str,
    baby_id: Optional[str],
    lookback_days: int,
    output_dir: Path,
    journal_type: tuple,
    units: str,
    timezone: str,
):
    """Export journal data from Happiest Baby API to CSV files.

    Each journal type will be exported to a separate CSV file named
    'journals_<type>.csv' in the output directory.
    """
    # Load token
    token = load_token(token_file)

    # Determine journal types to export
    if journal_type:
        journal_types = list(journal_type)
    else:
        journal_types = ["breastfeeding", "bottlefeeding", "diaper", "solidfood", "pumping", "sleep"]

    # Get baby_id only if needed (not needed for pumping journals only)
    needs_baby_id = any(jtype != "pumping" for jtype in journal_types)
    if needs_baby_id:
        if baby_id is None:
            baby_id = extract_baby_id_from_logs(request_log)
            click.echo(f"Extracted baby_id from logs: {baby_id}")
        else:
            click.echo(f"Using provided baby_id: {baby_id}")

    # Set date range
    to_date = datetime.utcnow()
    from_date = to_date - timedelta(days=lookback_days)

    click.echo(f"Fetching data from {from_date.date()} to {to_date.date()}")
    click.echo()

    # Create output directory if it doesn't exist
    output_dir.mkdir(parents=True, exist_ok=True)

    # Export each journal type
    for jtype in journal_types:
        try:
            if jtype == "pumping":
                journals = fetch_pumping_journals(from_date, to_date, token)
            elif jtype == "sleep":
                journals = fetch_sleep_sessions(baby_id, from_date, to_date, token, timezone)
            else:
                journals = fetch_journals(baby_id, jtype, from_date, to_date, token)

            output_file = output_dir / f"journals_{jtype}.csv"

            export_to_csv(journals, str(output_file), jtype, units)
            click.echo()
        except requests.exceptions.HTTPError as e:
            click.echo(f"  Error fetching {jtype}: {e}", err=True)
            click.echo()
        except Exception as e:
            click.echo(f"  Unexpected error for {jtype}: {e}", err=True)
            click.echo()

    click.echo("Export complete!")


if __name__ == "__main__":
    main()
