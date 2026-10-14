"""Server settings, all from environment variables."""

import os
from dataclasses import dataclass
from typing import FrozenSet, List
from zoneinfo import ZoneInfo


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    base_url: str
    token_secrets: List[str]
    allowed_emails: FrozenSet[str]
    default_timezone: str
    allow_loopback_redirects: bool

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/mcp"

    @classmethod
    def from_env(cls) -> "Settings":
        base_url = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
        if not base_url and os.environ.get("VERCEL_PROJECT_PRODUCTION_URL"):
            base_url = f"https://{os.environ['VERCEL_PROJECT_PRODUCTION_URL']}"
        if not base_url:
            raise ConfigError("Set PUBLIC_BASE_URL to the server's public https URL")

        # Comma-separated Fernet keys; the first encrypts, all decrypt (for rotation)
        secrets = [s.strip() for s in os.environ.get("TOKEN_SECRET", "").split(",") if s.strip()]
        if not secrets:
            raise ConfigError(
                "Set TOKEN_SECRET (generate one with: "
                "python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
            )

        allowed = frozenset(
            e.strip().lower() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()
        )

        tz = os.environ.get("DEFAULT_TIMEZONE", "America/New_York")
        ZoneInfo(tz)  # fail fast on a typo

        return cls(
            base_url=base_url,
            token_secrets=secrets,
            allowed_emails=allowed,
            default_timezone=tz,
            allow_loopback_redirects=os.environ.get("ALLOW_LOOPBACK_REDIRECTS", "true").lower() != "false",
        )

    def email_allowed(self, email: str) -> bool:
        return email.strip().lower() in self.allowed_emails
