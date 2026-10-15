"""A stateless OAuth 2.1 authorization server where users sign in with their Happiest Baby account.

Flow: Claude registers itself (DCR) and sends the user to /authorize. The SDK validates
the request and calls authorize(), which seals it and redirects to /login. The user
enters their Happiest Baby email and password; the server signs in to Cognito once,
seals the resulting Happiest Baby refresh token into an authorization code, and
redirects back to Claude. The token endpoint turns that into:

- an access token holding a short-lived Cognito IdToken (what the tools use), and
- a refresh token holding the Happiest Baby refresh token (used to mint new IdTokens).

The password is used once and never stored. Nothing is stored server-side; see sealing.py.
The time zone chosen on the sign-in page (prefilled from the browser) travels in every
token too, as that user's default for reading and writing times.

Limits of being stateless: an authorization code can be replayed until it expires
(5 minutes, and only together with the PKCE verifier), and an old refresh token keeps
working after rotation. Revoking a refresh token revokes its Happiest Baby refresh
token at Cognito, which ends every token derived from that sign-in.
"""

import base64
import hashlib
import html
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anyio.to_thread
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

import happiest_baby as hb

from .config import Settings
from .sealing import Sealer, fingerprint

SCOPE = "snoo"
AUTH_REQUEST_TTL = 15 * 60
CODE_TTL = 5 * 60
ACCESS_TTL_MAX = 60 * 60
REFRESH_TTL = 180 * 24 * 60 * 60  # in practice Cognito's own refresh token lifetime is the limit

CLAUDE_CALLBACKS = {
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
}


class HBAuthorizationCode(AuthorizationCode):
    hb_refresh_token: str
    timezone: Optional[str] = None


class HBRefreshToken(RefreshToken):
    hb_refresh_token: str
    timezone: Optional[str] = None


class HBAccessToken(AccessToken):
    id_token: str
    timezone: Optional[str] = None


def valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def redirect_uri_allowed(uri: str, allow_loopback: bool) -> bool:
    """Only Claude's hosted callback, plus loopback redirects for Claude Code."""
    if uri in CLAUDE_CALLBACKS:
        return True
    parsed = urlparse(uri)
    return allow_loopback and parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")


class StatelessOAuthProvider:
    """Implements mcp.server.auth.provider.OAuthAuthorizationServerProvider."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.sealer = Sealer(settings.token_secrets)

    # --- Clients -----------------------------------------------------------------

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in client_info.redirect_uris or []]
        bad = [u for u in uris if not redirect_uri_allowed(u, self.settings.allow_loopback_redirects)]
        if not uris or bad:
            raise RegistrationError("invalid_redirect_uri", f"Redirect URI not allowed: {', '.join(bad) or 'none'}")
        # The SDK returns this same object to the client, so replacing the random id
        # with a sealed copy of the registration makes the client id self-describing.
        info = client_info.model_dump(mode="json", exclude={"client_id"}, exclude_none=True)
        client_info.client_id = self.sealer.seal("client", {"info": info}, ttl=None)

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        data = self.sealer.open("client", client_id)
        if not data:
            return None
        return OAuthClientInformationFull.model_validate({**data["info"], "client_id": client_id})

    # --- Authorization -------------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        request = self.sealer.seal("authreq", {
            "cid": fingerprint(client.client_id),
            "client_name": client.client_name or "An MCP client",
            "redirect_uri": str(params.redirect_uri),
            "explicit": params.redirect_uri_provided_explicitly,
            "code_challenge": params.code_challenge,
            "state": params.state,
            "scopes": params.scopes or [SCOPE],
            "resource": params.resource,
        }, ttl=AUTH_REQUEST_TTL)
        return f"{self.settings.base_url}/login?req={request}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> Optional[HBAuthorizationCode]:
        data = self.sealer.open("code", authorization_code)
        if not data or data["cid"] != fingerprint(client.client_id):
            return None
        return HBAuthorizationCode(
            code=authorization_code,
            scopes=data["scopes"],
            expires_at=data["exp"],
            client_id=client.client_id,
            code_challenge=data["code_challenge"],
            redirect_uri=data["redirect_uri"],
            redirect_uri_provided_explicitly=data["explicit"],
            resource=data.get("resource"),
            subject=data["email"],
            hb_refresh_token=data["hb_rt"],
            timezone=data.get("tz"),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: HBAuthorizationCode
    ) -> OAuthToken:
        return await self._issue_tokens(
            client, authorization_code.hb_refresh_token, authorization_code.subject or "",
            authorization_code.scopes, authorization_code.resource, authorization_code.timezone,
        )

    # --- Refresh -----------------------------------------------------------------------

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> Optional[HBRefreshToken]:
        data = self.sealer.open("refresh", refresh_token)
        if not data or data["cid"] != fingerprint(client.client_id):
            return None
        if not self.settings.email_allowed(data["email"]):
            return None
        return HBRefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=data["scopes"],
            expires_at=data["exp"],
            resource=data.get("resource"),
            subject=data["email"],
            hb_refresh_token=data["hb_rt"],
            timezone=data.get("tz"),
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: HBRefreshToken, scopes: List[str]
    ) -> OAuthToken:
        return await self._issue_tokens(
            client, refresh_token.hb_refresh_token, refresh_token.subject or "",
            scopes or refresh_token.scopes, refresh_token.resource, refresh_token.timezone,
        )

    async def _issue_tokens(
        self, client: OAuthClientInformationFull, hb_refresh_token: str, email: str,
        scopes: List[str], resource: Optional[str], tz: Optional[str],
    ) -> OAuthToken:
        if not self.settings.email_allowed(email):
            raise TokenError("invalid_grant", "This account is no longer allowed on this server")
        try:
            session = await anyio.to_thread.run_sync(hb.refresh_session, hb_refresh_token)
        except hb.AuthError:
            raise TokenError("invalid_grant", "Happiest Baby sign-in expired; reconnect to sign in again") from None
        except hb.HappiestBabyError as e:
            # Not the client's fault; there's no better OAuth error code for an upstream outage
            raise TokenError("invalid_request", f"Couldn't reach Happiest Baby: {e}") from None

        hb_refresh_token = session.get("RefreshToken") or hb_refresh_token  # if Cognito rotated it
        id_token_ttl = int(session.get("ExpiresIn", 3600))
        access_ttl = max(60, min(ACCESS_TTL_MAX, id_token_ttl - 300))
        common = {"cid": fingerprint(client.client_id), "email": email, "scopes": scopes, "resource": resource,
                  "tz": tz}

        access = self.sealer.seal("access", {**common, "idt": session["IdToken"]}, ttl=access_ttl)
        refresh = self.sealer.seal("refresh", {**common, "hb_rt": hb_refresh_token}, ttl=REFRESH_TTL)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=access_ttl,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    # --- Access tokens and revocation ------------------------------------------------

    async def load_access_token(self, token: str) -> Optional[HBAccessToken]:
        data = self.sealer.open("access", token)
        if not data or not self.settings.email_allowed(data["email"]):
            return None
        return HBAccessToken(
            token=token,
            client_id=data["cid"],
            scopes=data["scopes"],
            expires_at=data["exp"],
            resource=data.get("resource"),
            subject=data["email"],
            id_token=data["idt"],
            timezone=data.get("tz"),
        )

    async def revoke_token(self, token: Any) -> None:
        if isinstance(token, HBRefreshToken):
            try:
                await anyio.to_thread.run_sync(hb.revoke_refresh_token, token.hb_refresh_token)
            except hb.HappiestBabyError:
                pass  # already revoked or expired; revocation must not error (RFC 7009)
        # Access tokens can't be revoked individually; they expire within an hour.

    # --- Sign-in page ----------------------------------------------------------------------

    def login_routes(self) -> List[Route]:
        return [Route("/login", self.login, methods=["GET", "POST"])]

    async def login(self, request: Request) -> Response:
        if request.method == "GET":
            req = request.query_params.get("req", "")
            form: Dict[str, str] = {}
        else:
            form = {k: str(v) for k, v in (await request.form()).items()}
            req = form.get("req", "")

        pending = self.sealer.open("authreq", req)
        if not pending:
            return _page("Sign-in link expired", "<p>This sign-in link is invalid or has expired. "
                         "Go back to Claude and connect again.</p>", status=400)
        if request.method == "GET":
            return self._login_form(req, pending)

        email = form.get("email", "").strip()
        password = form.get("password", "")
        tz = form.get("timezone", "").strip() or self.settings.default_timezone

        def retry(error: str) -> Response:
            return self._login_form(req, pending, email, error, tz)

        if not email or not password:
            return retry("Enter your email and password.")
        if not valid_timezone(tz):
            return retry("Enter a time zone name like America/Chicago or Europe/London.")
        if not self.settings.allowed_emails:
            return retry("This server has no allowed accounts yet. Set ALLOWED_EMAILS in its environment.")
        # Check the allowlist before contacting Cognito, so the server can't be used to test passwords.
        # Both failures share one message so the page doesn't reveal which emails are allowed.
        rejected = "Email or password is incorrect, or this account isn't allowed on this server."
        if not self.settings.email_allowed(email):
            return retry(rejected)
        try:
            result = await anyio.to_thread.run_sync(hb.authenticate_password, email, password)
        except hb.AuthError:
            return retry(rejected)
        except hb.HappiestBabyError:
            return retry("Couldn't reach Happiest Baby. Try again in a minute.")

        code = self.sealer.seal("code", {
            "cid": pending["cid"],
            "redirect_uri": pending["redirect_uri"],
            "explicit": pending["explicit"],
            "code_challenge": pending["code_challenge"],
            "scopes": pending["scopes"],
            "resource": pending.get("resource"),
            "email": email.lower(),
            "hb_rt": result["RefreshToken"],
            "tz": tz,
        }, ttl=CODE_TTL)
        target = construct_redirect_uri(pending["redirect_uri"], code=code, state=pending.get("state"))
        return RedirectResponse(target, status_code=302, headers=_SECURITY_HEADERS)

    def _login_form(
        self, req: str, pending: Dict[str, Any], email: str = "", error: str = "", tz: str = ""
    ) -> Response:
        host = urlparse(pending["redirect_uri"]).netloc or pending["redirect_uri"]
        loopback = host.split(":")[0] in ("localhost", "127.0.0.1")
        warning = (
            "<p class=warn>This request will send access to a program running on this computer "
            f"({html.escape(host)}). Only continue if you just connected Claude Code.</p>"
            if loopback else ""
        )
        body = f"""
<p><strong>{html.escape(pending['client_name'])}</strong> wants to control your SNOO and read and
write your baby's journal. You'll be sent back to <strong>{html.escape(host)}</strong>.</p>
{warning}
{f'<p class=error role=alert>{html.escape(error)}</p>' if error else ''}
<form method=post action=/login>
  <input type=hidden name=req value="{html.escape(req)}">
  <label>Happiest Baby email<input name=email type=email autocomplete=username required
    value="{html.escape(email)}"></label>
  <label>Password<input name=password type=password autocomplete=current-password required></label>
  <label>Time zone<input id=tz name=timezone required autocapitalize=off spellcheck=false
    value="{html.escape(tz or self.settings.default_timezone)}"{' data-keep' if tz else ''}></label>
  <button type=submit>Sign in and allow</button>
</form>
<script>{_TZ_SCRIPT}</script>
<p class=small>Your password is sent to Happiest Baby once to sign in and is never stored. Times you
give Claude, like "10:30", are read in this time zone unless you say otherwise.</p>"""
        return _page("Connect your SNOO", body, status=400 if error else 200)


# Prefills the time zone field from the browser, unless the user already typed one
_TZ_SCRIPT = ("const f=document.getElementById('tz');"
              "if(!f.hasAttribute('data-keep')){try{f.value=Intl.DateTimeFormat().resolvedOptions().timeZone||f.value}"
              "catch(e){}}")
_TZ_SCRIPT_HASH = base64.b64encode(hashlib.sha256(_TZ_SCRIPT.encode()).digest()).decode()

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    # form-action also governs the redirect after sign-in, to Claude or a Claude Code loopback port
    "Content-Security-Policy": f"default-src 'none'; style-src 'unsafe-inline'; script-src 'sha256-{_TZ_SCRIPT_HASH}'; "
                               "form-action 'self' https: http://localhost:* http://127.0.0.1:*; "
                               "frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
}


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html>
<html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  :root {{ color-scheme: light dark; --fg:#1d1d1f; --bg:#f5f5f7; --card:#fff; --muted:#6e6e73; --accent:#0a66c2; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --fg:#f5f5f7; --bg:#111; --card:#1c1c1e; --muted:#a1a1a6; --accent:#4c9ee8; }} }}
  body {{ margin:0; font:16px/1.5 system-ui,-apple-system,sans-serif; color:var(--fg); background:var(--bg); }}
  main {{ max-width:420px; margin:8vh auto; padding:24px; background:var(--card); border-radius:16px; }}
  h1 {{ font-size:1.4rem; margin-top:0; }}
  label {{ display:block; margin:14px 0; font-weight:600; }}
  input {{ display:block; width:100%; box-sizing:border-box; margin-top:6px; padding:12px; font:inherit;
          border:1px solid var(--muted); border-radius:10px; background:transparent; color:inherit; }}
  button {{ width:100%; padding:12px; font:inherit; font-weight:600; color:#fff; background:var(--accent);
           border:0; border-radius:10px; cursor:pointer; }}
  .error {{ color:#d70015; }} .warn {{ color:#b25000; }} .small {{ color:var(--muted); font-size:.85rem; }}
  @media (max-width:480px) {{ main {{ margin:0; border-radius:0; min-height:100vh; padding:16px; }} }}
</style></head>
<body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>""", status_code=status, headers=_SECURITY_HEADERS)


def info_page(settings: Settings) -> HTMLResponse:
    body = f"""<p>This is a private Model Context Protocol server for a Happiest Baby / SNOO account.</p>
<p>To use it, add <code>{html.escape(settings.mcp_url)}</code> as a custom connector in Claude
(Settings → Connectors → Add custom connector).</p>"""
    return _page("Happiest Baby MCP server", body)
