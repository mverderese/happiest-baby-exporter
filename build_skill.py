#!/usr/bin/env -S uv run
# /// script
# dependencies = [
#   "requests>=2.32.0",
#   "click>=8.1.0",
# ]
# requires-python = ">=3.12"
# ///
"""
Package the Happiest Baby exporter as a Claude skill zip.

Signs in once with your email/password (locally, never sent to Claude) and bundles
the resulting refresh token, not the password, into the skill as credentials.json.
"""

import importlib.util
import json
import os
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import click
import requests

REPO_DIR = Path(__file__).resolve().parent
SKILL_NAME = "happiest-baby"


def load_exporter():
    """Import happiest-baby-exporter.py (hyphenated name, so via importlib)."""
    spec = importlib.util.spec_from_file_location("exporter", REPO_DIR / "happiest-baby-exporter.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_refresh_token_from(path: Path) -> str:
    """Read refresh_token from a credentials.json or a previously built skill zip."""
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            return json.loads(zf.read(f"{SKILL_NAME}/credentials.json"))["refresh_token"]
    return json.loads(path.read_text())["refresh_token"]


@click.command()
@click.option("--email", envvar="HAPPIESTBABY_EMAIL", help="Email address (or HAPPIESTBABY_EMAIL env var)")
@click.option("--password", envvar="HAPPIESTBABY_PASSWORD", help="Password (or HAPPIESTBABY_PASSWORD env var)")
@click.option(
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=REPO_DIR / "dist" / f"{SKILL_NAME}.zip",
    help="Where to write the skill zip",
    show_default=True,
)
@click.option(
    "--revoke",
    "revoke_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Revoke the refresh token inside a previously built zip (or credentials.json) and exit",
)
def main(email, password, output: Path, revoke_path: Path) -> None:
    """Build dist/happiest-baby.zip for upload to Claude (Settings > Capabilities > Skills)."""
    exporter = load_exporter()

    if revoke_path:
        try:
            exporter.revoke_refresh_token(read_refresh_token_from(revoke_path))
        except requests.exceptions.HTTPError as e:
            click.echo(f"Error: Revocation failed - {e}", err=True)
            sys.exit(1)
        click.echo(f"✓ Revoked the refresh token in {revoke_path}")
        return

    email = email or click.prompt("Happiest Baby email")
    password = password or click.prompt("Happiest Baby password", hide_input=True)

    click.echo("Authenticating with Happiest Baby...")
    refresh_token = exporter.authenticate(email, password)["RefreshToken"]
    click.echo("✓ Authentication successful")

    credentials = {
        "refresh_token": refresh_token,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    # Create the file owner-only before writing the secret into it
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f, zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(REPO_DIR / "skill" / "SKILL.md", f"{SKILL_NAME}/SKILL.md")
        zf.write(REPO_DIR / "happiest-baby-exporter.py", f"{SKILL_NAME}/scripts/happiest-baby-exporter.py")
        zf.writestr(f"{SKILL_NAME}/credentials.json", json.dumps(credentials, indent=2) + "\n")

    click.echo(f"✓ Wrote {output}")
    click.echo("Upload it in Claude: Settings > Capabilities > Skills > Upload skill.")
    click.echo("This zip contains a login token; don't share it. Delete it after uploading,")
    click.echo(f"or keep it so you can later run: ./build_skill.py --revoke {output}")


if __name__ == "__main__":
    main()
