"""End-to-end test of the OAuth flow and MCP tools, with Happiest Baby mocked out.

Run with: pip install -r requirements.txt pytest && pytest tests
"""

import base64
import hashlib
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import happiest_baby as hb  # noqa: E402
from mcp_server.app import build_app  # noqa: E402
from mcp_server.config import Settings  # noqa: E402

BASE = "https://hb.example.com"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
EMAIL = "parent@example.com"
PASSWORD = "correct horse"
BABY = {"_id": "baby1", "babyName": "Chloe"}
DEVICE = {"name": "Nursery", "serialNumber": "SN1", "awsIoT": {"clientEndpoint": "iot.example.com", "thingName": "t1"}}


class FakeHappiestBaby:
    """Stands in for Cognito, the REST API, and the SNOO."""

    def __init__(self):
        self.created = []
        self.revoked = []
        self.snoo = {"state": "ONLINE", "hold": "off"}

    def authenticate_password(self, email, password):
        if (email, password) != (EMAIL, PASSWORD):
            raise hb.AuthError("Incorrect username or password.")
        return {"IdToken": "id-1", "RefreshToken": "hb-refresh", "ExpiresIn": 3600}

    def refresh_session(self, refresh_token):
        if refresh_token in self.revoked or refresh_token != "hb-refresh":
            raise hb.AuthError("Refresh Token has been revoked")
        return {"IdToken": "id-2", "ExpiresIn": 3600}

    def revoke_refresh_token(self, refresh_token):
        self.revoked.append(refresh_token)

    def request(self, client, method, endpoint, **kwargs):
        assert client.id_token == "id-2"
        if endpoint.endswith("/babies"):
            return [BABY]
        if endpoint.endswith("/devices"):
            return {"snoo": [DEVICE]}
        if method == "POST" and endpoint.endswith("/journals"):
            self.created.append(kwargs["json"])
            return {"id": "j1", **kwargs["json"]}
        if "journals/tracking" in endpoint:
            return [{"id": "j0", "type": kwargs["params"]["journalType"], "startTime": "2026-10-14T10:00:00.000Z",
                     "data": {"types": ["pee"]}}]
        raise AssertionError(f"unexpected {method} {endpoint}")

    def exchange(self, id_token, device, command, done, timeout=20.0, port=443):
        assert id_token == "id-2" and device["serialNumber"] == "SN1"
        if command and command["command"] == "go_to_state":
            self.snoo = {"state": command["state"], "hold": command["hold"]}
        state = {"state_machine": {**self.snoo, "is_active_session": True, "time_left": 240},
                 "left_safety_clip": 1, "right_safety_clip": 1}
        return state if done(state) else None


@pytest.fixture
def fake(monkeypatch):
    fake = FakeHappiestBaby()
    monkeypatch.setattr(hb, "authenticate_password", fake.authenticate_password)
    monkeypatch.setattr(hb, "refresh_session", fake.refresh_session)
    monkeypatch.setattr(hb, "revoke_refresh_token", fake.revoke_refresh_token)
    monkeypatch.setattr(hb.HappiestBabyClient, "request", lambda self, *a, **k: fake.request(self, *a, **k))
    monkeypatch.setattr("happiest_baby.snoo.exchange", fake.exchange)
    return fake


@pytest.fixture
def client(fake):
    settings = Settings(
        base_url=BASE,
        token_secrets=[Fernet.generate_key().decode()],
        allowed_emails=frozenset({EMAIL}),
        default_timezone="America/New_York",
        allow_loopback_redirects=True,
    )
    return TestClient(build_app(settings), base_url=BASE)


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def register(client, redirect=CALLBACK):
    return client.post("/register", json={
        "client_name": "Claude",
        "redirect_uris": [redirect],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    })


def sign_in(client, email=EMAIL, password=PASSWORD):
    """Run register -> authorize -> login; return (client_id, verifier, login response)."""
    client_id = register(client).json()["client_id"]
    verifier, challenge = pkce()
    r = client.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK, "state": "st8",
        "code_challenge": challenge, "code_challenge_method": "S256", "scope": "snoo", "resource": f"{BASE}/mcp",
    }, follow_redirects=False)
    assert r.status_code == 302, r.text
    login_url = urlparse(r.headers["location"])
    assert login_url.path == "/login"
    req = parse_qs(login_url.query)["req"][0]

    form = client.get(f"/login?req={req}")
    assert form.status_code == 200 and "claude.ai" in form.text and "Claude" in form.text

    r = client.post("/login", data={"req": req, "email": email, "password": password}, follow_redirects=False)
    return client_id, verifier, r


def get_tokens(client):
    client_id, verifier, r = sign_in(client)
    assert r.status_code == 302, r.text
    back = urlparse(r.headers["location"])
    assert f"{back.scheme}://{back.netloc}{back.path}" == CALLBACK
    query = parse_qs(back.query)
    assert query["state"] == ["st8"]
    r = client.post("/token", data={
        "grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": CALLBACK,
        "client_id": client_id, "code_verifier": verifier,
    })
    assert r.status_code == 200, r.text
    return client_id, r.json()


def mcp_call(client, token, method, params=None, id_=1):
    r = client.post("/mcp", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }, json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})
    assert r.status_code == 200, r.text
    return r.json()


def call_tool(client, token, name, arguments):
    result = mcp_call(client, token, "tools/call", {"name": name, "arguments": arguments})["result"]
    return result


# --- Discovery -------------------------------------------------------------------


def test_unauthenticated_mcp_points_to_metadata(client):
    r = client.post("/mcp", json={})
    assert r.status_code == 401
    assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"' in r.headers["www-authenticate"]

    prm = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == f"{BASE}/mcp"
    assert prm["authorization_servers"] == [BASE]

    asm = client.get("/.well-known/oauth-authorization-server").json()
    assert asm["issuer"] == BASE
    assert asm["registration_endpoint"] == f"{BASE}/register"
    assert "S256" in asm["code_challenge_methods_supported"]


def test_confidential_client_needs_its_secret(client):
    r = client.post("/register", json={
        "redirect_uris": [CALLBACK], "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "client_secret_post",
    })
    assert r.status_code == 201
    registered = r.json()
    assert registered["client_secret"] and registered["client_secret"] not in registered["client_id"]
    for secret, status in (("wrong", 401), (registered["client_secret"], 400)):
        # 400 with the right secret: authenticated, then rejected for the bogus code
        r = client.post("/token", data={
            "grant_type": "authorization_code", "code": "hbk_bogus", "redirect_uri": CALLBACK,
            "client_id": registered["client_id"], "client_secret": secret, "code_verifier": "x" * 50,
        })
        assert r.status_code == status, r.text


def test_registration_rejects_foreign_redirects(client):
    assert register(client, "https://evil.example.com/cb").status_code == 400
    assert register(client, "http://localhost:4567/callback").status_code == 201


# --- Sign-in -------------------------------------------------------------------------


def test_wrong_password_and_unlisted_email(client, fake):
    _, _, r = sign_in(client, password="nope")
    assert r.status_code == 400 and "incorrect" in r.text
    _, _, r = sign_in(client, email="stranger@example.com")
    assert r.status_code == 400 and "isn&#x27;t allowed" in r.text


def test_code_requires_pkce_verifier(client):
    client_id, _, r = sign_in(client)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    r = client.post("/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK,
        "client_id": client_id, "code_verifier": "x" * 50,
    })
    assert r.status_code == 400


def test_tampered_or_wrong_type_tokens_rejected(client):
    _, tokens = get_tokens(client)
    access = tokens["access_token"]
    tampered = access[:-4] + ("AAAA" if not access.endswith("AAAA") else "BBBB")
    for bad in (tampered, tokens["refresh_token"], "hba_garbage"):
        r = client.post("/mcp", headers={"Authorization": f"Bearer {bad}"}, json={})
        assert r.status_code == 401


# --- Tools -----------------------------------------------------------------------------


def test_tools_end_to_end(client, fake):
    _, tokens = get_tokens(client)
    token = tokens["access_token"]

    init = mcp_call(client, token, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    })
    assert "Happiest Baby" in init["result"]["serverInfo"]["name"]

    names = {t["name"] for t in mcp_call(client, token, "tools/list")["result"]["tools"]}
    assert {"snoo_status", "snoo_set_level", "log_diaper", "get_journal_entries"} <= names

    result = call_tool(client, token, "snoo_set_level", {"level": "2", "hold": True})
    assert not result.get("isError"), result
    assert result["structuredContent"]["result"]["summary"].startswith("LEVEL2, hold on")

    result = call_tool(client, token, "log_diaper", {"pee": True, "poo": True, "time": "2026-10-14T03:10:00-04:00"})
    assert not result.get("isError"), result
    assert fake.created == [{
        "type": "diaper", "data": {"types": ["pee", "poo"]},
        "startTime": "2026-10-14T03:10:00.000-04:00", "babyId": "baby1",
    }]

    result = call_tool(client, token, "log_diaper", {"pee": False, "poo": False})
    assert result["isError"]

    result = call_tool(client, token, "get_journal_entries", {"journal_type": "diaper"})
    assert not result.get("isError"), result


# --- Refresh and revoke --------------------------------------------------------------------


def test_refresh_then_revoke(client, fake):
    client_id, tokens = get_tokens(client)
    r = client.post("/token", data={
        "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id,
    })
    assert r.status_code == 200, r.text
    refreshed = r.json()
    assert refreshed["access_token"] != tokens["access_token"]

    # The SDK's revocation request model requires client_secret to be present, even if empty
    r = client.post("/revoke", data={"token": refreshed["refresh_token"], "client_id": client_id, "client_secret": ""})
    assert r.status_code == 200
    assert fake.revoked == ["hb-refresh"]

    # Every refresh token from that sign-in now fails, since Cognito revoked the underlying token
    r = client.post("/token", data={
        "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id,
    })
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
