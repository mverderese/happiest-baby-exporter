"""Shared Happiest Baby / SNOO client used by happiest-baby-actions.py and the MCP server.

Everything here raises HappiestBabyError (or a subclass) instead of exiting, so callers
decide how to report failures. The API is undocumented and reverse-engineered (see
python-snoo), so any of it may break if Happiest Baby changes it.
"""

from .client import (
    API_BASE_URL,
    AuthError,
    HappiestBabyClient,
    HappiestBabyError,
    authenticate_password,
    consolidate_sleep_levels,
    refresh_session,
    revoke_refresh_token,
)
from .snoo import LEVELS, SnooError, SnooNotConfirmed, describe_state, snoo_command, snoo_status
from .timeutil import api_time, parse_time, show_time

__all__ = [
    "API_BASE_URL",
    "AuthError",
    "HappiestBabyClient",
    "HappiestBabyError",
    "LEVELS",
    "SnooError",
    "SnooNotConfirmed",
    "api_time",
    "authenticate_password",
    "consolidate_sleep_levels",
    "describe_state",
    "parse_time",
    "refresh_session",
    "revoke_refresh_token",
    "show_time",
    "snoo_command",
    "snoo_status",
]
