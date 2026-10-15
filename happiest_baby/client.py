"""Cognito auth and the Happiest Baby REST API."""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

API_BASE_URL = "https://api-us-east-1-prod.happiestbaby.com"
COGNITO_ENDPOINT = "https://cognito-idp.us-east-1.amazonaws.com/"
CLIENT_ID = "6kqofhc8hm394ielqdkvli0oea"

BABIES_ENDPOINT = "/us/me/v10/babies"
DEVICES_ENDPOINT = "/hds/me/v11/devices"
JOURNALS_ENDPOINT = "/cs/me/v11/babies/{baby_id}/journals/tracking"
PUMPING_ENDPOINT = "/cs/me/v11/pumping-journals/tracking"
SLEEP_ENDPOINT = "/ss/me/v11/babies/{baby_id}/sessions/daily"
JOURNAL_CREATE_ENDPOINT = "/cs/me/v11/journals"
JOURNAL_ITEM_ENDPOINT = "/cs/me/v11/journals/{journal_id}"
PUMPING_CREATE_ENDPOINT = "/cs/me/v11/pumping-journals"

JOURNAL_TYPES = ["breastfeeding", "bottlefeeding", "diaper", "solidfood", "pumping"]
TIMEOUT = 30
SLEEP_FETCH_WORKERS = 8


class HappiestBabyError(Exception):
    """A request to Happiest Baby failed; the message is safe to show the user."""


class AuthError(HappiestBabyError):
    """Credentials or a refresh token were rejected (Cognito NotAuthorizedException)."""


def _cognito(target: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    headers = {
        "Content-Type": "application/x-amz-json-1.1",
        "X-Amz-Target": f"AWSCognitoIdentityProviderService.{target}",
    }
    try:
        response = requests.post(
            COGNITO_ENDPOINT, json={**payload, "ClientId": CLIENT_ID}, headers=headers, timeout=TIMEOUT
        )
    except requests.exceptions.RequestException as e:
        raise HappiestBabyError(f"Network error reaching Happiest Baby sign-in - {e}") from e
    if response.ok:
        return response.json()
    try:
        error = response.json()
    except ValueError:
        error = {}
    error_type = error.get("__type", "")
    message = error.get("message") or response.reason
    if error_type in ("NotAuthorizedException", "UserNotFoundException"):
        raise AuthError(message)
    raise HappiestBabyError(f"Happiest Baby sign-in failed ({error_type or response.status_code}): {message}")


def _auth_result(data: Dict[str, Any]) -> Dict[str, Any]:
    if "AuthenticationResult" not in data:
        # e.g. a ChallengeName such as MFA, which this client doesn't support
        raise AuthError(f"Sign-in needs a step this tool doesn't support ({data.get('ChallengeName', 'unknown')})")
    return data["AuthenticationResult"]


def authenticate_password(email: str, password: str) -> Dict[str, Any]:
    """Sign in; returns Cognito's AuthenticationResult (IdToken, RefreshToken, ExpiresIn, ...)."""
    return _auth_result(_cognito(
        "InitiateAuth",
        {"AuthFlow": "USER_PASSWORD_AUTH", "AuthParameters": {"USERNAME": email, "PASSWORD": password}},
    ))


def refresh_session(refresh_token: str) -> Dict[str, Any]:
    """Exchange a refresh token; the result has IdToken, ExpiresIn, and RefreshToken only if rotated."""
    return _auth_result(_cognito(
        "InitiateAuth", {"AuthFlow": "REFRESH_TOKEN_AUTH", "AuthParameters": {"REFRESH_TOKEN": refresh_token}}
    ))


def revoke_refresh_token(refresh_token: str) -> None:
    _cognito("RevokeToken", {"Token": refresh_token})


class HappiestBabyClient:
    """REST calls made with a Cognito IdToken."""

    def __init__(self, id_token: str):
        self.id_token = id_token

    def request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        url = f"{API_BASE_URL}{endpoint}"
        headers = {"Authorization": f"Bearer {self.id_token}"}
        try:
            response = requests.request(method, url, headers=headers, timeout=TIMEOUT, **kwargs)
        except requests.exceptions.RequestException as e:
            raise HappiestBabyError(f"Network error reaching Happiest Baby - {e}") from e
        if response.status_code == 401:
            raise AuthError("Happiest Baby rejected the login token")
        if not response.ok:
            raise HappiestBabyError(
                f"{method} {endpoint} failed with HTTP {response.status_code}: {response.text[:500]}"
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    # --- Babies and devices ---------------------------------------------------

    def babies(self) -> List[Dict[str, Any]]:
        return self.request("GET", BABIES_ENDPOINT) or []

    def select_baby(self, baby_name: Optional[str]) -> Dict[str, Any]:
        """The named baby, or the only baby; raises listing the names when ambiguous."""
        babies = self.babies()
        names = ", ".join(b.get("babyName", "Unknown") for b in babies)
        if not babies:
            raise HappiestBabyError("No babies found on this account")
        if baby_name:
            matches = [b for b in babies if b.get("babyName", "").lower() == baby_name.lower()]
            if not matches:
                raise HappiestBabyError(f"No baby named '{baby_name}'; babies on this account: {names}")
            return matches[0]
        if len(babies) > 1:
            raise HappiestBabyError(f"More than one baby on this account; give baby_name as one of: {names}")
        return babies[0]

    def devices(self) -> List[Dict[str, Any]]:
        data = self.request("GET", DEVICES_ENDPOINT)
        return data.get("snoo", []) if isinstance(data, dict) else []

    def select_device(self, device_name: Optional[str]) -> Dict[str, Any]:
        """The named SNOO (by name or serial), or the only one."""
        devices = self.devices()
        names = ", ".join(f"{d.get('name', 'Unnamed')} ({d.get('serialNumber')})" for d in devices)
        if not devices:
            raise HappiestBabyError("No SNOO found on this account")
        if device_name:
            wanted = device_name.lower()
            matches = [
                d for d in devices
                if wanted in (str(d.get("name", "")).lower(), str(d.get("serialNumber", "")).lower())
            ]
        else:
            matches = devices if len(devices) == 1 else []
        if len(matches) != 1:
            raise HappiestBabyError(f"Specify which SNOO by name or serial: {names}")
        device = matches[0]
        if not (device.get("awsIoT") or {}).get("clientEndpoint"):
            raise HappiestBabyError(f"{device.get('name', 'This SNOO')} has no AWS IoT endpoint; update its firmware")
        return device

    # --- Journals ---------------------------------------------------------------

    def journals(self, baby_id: str, journal_type: str, from_date: datetime, to_date: datetime) -> List[Dict[str, Any]]:
        params = {
            "fromDateTime": from_date.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "toDateTime": to_date.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "journalType": journal_type,
        }
        return self.request("GET", JOURNALS_ENDPOINT.format(baby_id=baby_id), params=params) or []

    def pumping_journals(self, from_date: datetime, to_date: datetime) -> List[Dict[str, Any]]:
        params = {
            "fromDateTime": from_date.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "toDateTime": to_date.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }
        return self.request("GET", PUMPING_ENDPOINT, params=params) or []

    def create_journal(self, payload: Dict[str, Any]) -> Any:
        endpoint = PUMPING_CREATE_ENDPOINT if payload.get("type") == "pumping" else JOURNAL_CREATE_ENDPOINT
        return self.request("POST", endpoint, json=payload)

    def delete_journal(self, journal_id: str) -> None:
        if not journal_id.replace("-", "").replace("_", "").isalnum():
            raise HappiestBabyError("That doesn't look like a journal id")
        self.request("DELETE", JOURNAL_ITEM_ENDPOINT.format(journal_id=journal_id))

    # --- SNOO sleep sessions ------------------------------------------------------

    def sleep_sessions(self, baby_id: str, from_date: datetime, to_date: datetime, tz: str) -> List[Dict[str, Any]]:
        """Sleep sessions (SNOO and manually logged) overlapping the range, one entry per session.

        The API groups sessions into days starting at 6 AM, so the day before is fetched too:
        a session that began before the first day's 6 AM is then returned whole.
        """
        days = []
        day = from_date.replace(hour=6, minute=0, second=0, microsecond=0) - timedelta(days=1)
        while day.date() <= to_date.date():
            days.append(day)
            day += timedelta(days=1)

        def fetch(day: datetime) -> Any:
            params = {
                "detailedLevels": "true",
                "levels": "true",
                "startTime": day.strftime("%Y-%m-%d %H:%M:%S.000"),
                "timezone": tz,
            }
            return self.request("GET", SLEEP_ENDPOINT.format(baby_id=baby_id), params=params)

        # One request per day; fetched concurrently so a month fits in a serverless time limit
        levels: List[Dict[str, Any]] = []
        detailed: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=SLEEP_FETCH_WORKERS) as pool:
            for data in pool.map(fetch, days):
                if isinstance(data, dict):
                    levels.extend(data.get("levels", []))
                    detailed.extend(data.get("detailedLevels", []))
                elif isinstance(data, list):
                    levels.extend(data)
        return [s for s in consolidate_sleep_levels(levels, detailed) if not _ended_before(s, from_date)]


def _parse_api_time(value: str) -> datetime:
    """Parse an API time such as 2026-10-12T14:50:00-0400."""
    return datetime.strptime(value[:19] + value[-5:], "%Y-%m-%dT%H:%M:%S%z")


def _ended_before(session: Dict[str, Any], when: datetime) -> bool:
    try:
        start = _parse_api_time(session["startTime"])
    except (TypeError, ValueError):
        return False
    return start + timedelta(seconds=session["totalSeconds"]) < when


def consolidate_sleep_levels(
    levels: List[Dict[str, Any]], detailed: Optional[List[Dict[str, Any]]] = None
) -> List[Dict[str, Any]]:
    """Merge per-level rows into sessions: start, summed seconds per level type, and whether the
    session was logged manually in the app (with its tag, e.g. in_arms, and note, if any)."""
    by_session: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for level in levels:
        if level.get("sessionId"):
            by_session[level["sessionId"]].append(level)
    details: Dict[str, Dict[str, Any]] = {}
    for row in detailed or []:
        info = details.setdefault(row.get("sessionId"), {})
        for key in ("label", "note"):
            if row.get(key):
                info[key] = row[key]
        if row.get("level") == "MANUAL":
            info["manual"] = True
        start = (row.get("sessionDetails") or {}).get("startFormatted")
        if start:
            info["start"] = start

    sessions = []
    for session_id, rows in by_session.items():
        rows.sort(key=lambda r: r.get("startTime", ""))
        seconds_by_type: Dict[str, float] = defaultdict(float)
        for row in rows:
            if isinstance(row.get("stateDuration"), (int, float)):
                seconds_by_type[row.get("type") or "unknown"] += row["stateDuration"]
        info = details.get(session_id, {})
        manual = info.get("manual") or "manual" in seconds_by_type
        session = {
            "sessionId": session_id,
            "source": "manual" if manual else "snoo",
            "startTime": info.get("start") or rows[0].get("startTimeFormatted") or rows[0].get("startTime"),
            "totalSeconds": int(sum(seconds_by_type.values())),
            "secondsByType": {k: int(v) for k, v in sorted(seconds_by_type.items())},
        }
        session.update({key: info[key] for key in ("label", "note") if key in info})
        sessions.append(session)
    sessions.sort(key=lambda s: s.get("startTime") or "")
    return sessions
