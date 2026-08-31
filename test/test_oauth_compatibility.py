"""Regression tests for the OAuth compatibility layer in app/entrypoint.py.

`ads_mcp` is stubbed so the suite runs without the Google Ads SDK. See DESIGN.md
for how to run it.
"""

from __future__ import annotations

import sys
import types
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

BASE = "https://abc123.execute-api.us-east-1.amazonaws.com/prod"
ADS_SCOPE = "https://www.googleapis.com/auth/adwords"
OAUTH_CLIENT_ID = "1234567890-abcdef.apps.googleusercontent.com"


def _install_ads_mcp_stub() -> None:
    """Stand in for upstream so entrypoint can be imported in isolation."""
    from fastmcp import FastMCP

    coordinator = types.ModuleType("ads_mcp.coordinator")
    coordinator.mcp = FastMCP("google-ads-mcp-stub")

    @coordinator.mcp.tool()
    def search(customer_id: str, query: str) -> str:  # noqa: D401
        """Stub of upstream's GAQL search tool."""
        return "stub"

    root = types.ModuleType("ads_mcp")
    resources = types.ModuleType("ads_mcp.resources")

    sys.modules["ads_mcp"] = root
    sys.modules["ads_mcp.coordinator"] = coordinator
    sys.modules["ads_mcp.resources"] = resources
    for name in ("discovery", "metrics", "release_notes", "segments"):
        mod = types.ModuleType(f"ads_mcp.resources.{name}")
        sys.modules[f"ads_mcp.resources.{name}"] = mod
        setattr(resources, name, mod)


@pytest.fixture(scope="module")
def client(monkeypatch_module=None):
    import os
    import pathlib

    os.environ["PROXY_URL"] = BASE
    # Both are required at import time -- the client ID because it is the
    # audience the verifier pins tokens to, the developer token because the
    # Google Ads API rejects every call without one.
    os.environ["GOOGLE_OAUTH_CLIENT_ID"] = OAUTH_CLIENT_ID
    os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"] = "test-developer-token"
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))
    _install_ads_mcp_stub()

    import entrypoint

    with TestClient(entrypoint.app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_protected_resource_metadata_advertises_ads_scope(client):
    """The whole point: Quick reads scopes from here, not from the 401 header."""
    r = client.get("/.well-known/oauth-protected-resource")
    assert r.status_code == 200
    body = r.json()
    assert ADS_SCOPE in body["scopes_supported"]
    assert "openid" in body["scopes_supported"]
    assert body["resource"] == BASE
    assert body["authorization_servers"] == [BASE]


def test_authorization_server_metadata_points_token_endpoint_at_google(client):
    r = client.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    body = r.json()
    assert body["authorization_endpoint"] == f"{BASE}/oauth/authorize"
    assert body["token_endpoint"] == "https://oauth2.googleapis.com/token"
    assert body["code_challenge_methods_supported"] == ["S256"]


def test_authorize_forces_ads_scope_and_preserves_pkce(client):
    r = client.get(
        "/oauth/authorize",
        params={
            "client_id": "cid.apps.googleusercontent.com",
            "redirect_uri": "https://us-east-1.quicksight.aws.amazon.com/sn/oauthcallback",
            "response_type": "code",
            "state": "xyz",
            "code_challenge": "abc",
            "code_challenge_method": "S256",
            # Quick sends its own defaults here; they must be overwritten.
            "scope": "openid profile",
            # RFC 8707 resource indicator -- stripped, Google can reject it.
            "resource": BASE,
        },
        follow_redirects=False,
    )
    assert r.status_code == 302

    target = urlparse(r.headers["location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == (
        "https://accounts.google.com/o/oauth2/v2/auth"
    )

    q = parse_qs(target.query)
    # openid is load-bearing, not tidy-able: without it Google's tokeninfo omits
    # `sub` and the verifier rejects every token. See DESIGN.md.
    assert q["scope"] == [f"openid {ADS_SCOPE}"]
    assert "resource" not in q
    # refresh tokens require both of these
    assert q["access_type"] == ["offline"]
    assert q["prompt"] == ["consent"]
    # everything the client sent must survive
    assert q["state"] == ["xyz"]
    assert q["code_challenge"] == ["abc"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["client_id"] == ["cid.apps.googleusercontent.com"]


def test_unauthenticated_mcp_call_is_rejected(client):
    """No token must give 401, not 200 -- proves auth is actually enforced."""
    r = client.post(
        "/",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").startswith("Bearer")


def test_mcp_endpoint_is_mounted_at_root(client):
    """MCP must answer at "/" (Quick POSTs there): not-404 is the point."""
    response = client.post(
        "/",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert response.status_code != 404


# --- audience binding: reject tokens minted for a different OAuth client, so
# this deployment's developer token cannot be borrowed. Parent verifier stubbed.


def _entrypoint():
    import entrypoint

    return entrypoint


def _fake_parent_returning(aud, monkeypatch):
    """Make the superclass return a verified token carrying `aud`."""
    from fastmcp.server.auth import AccessToken
    from fastmcp.server.auth.providers.google import GoogleTokenVerifier

    async def fake_verify(self, token):
        if aud is None:
            return None
        return AccessToken(
            token=token,
            client_id="google-user-sub",
            scopes=[ADS_SCOPE],
            expires_at=None,
            claims={"sub": "google-user-sub", "aud": aud},
        )

    monkeypatch.setattr(GoogleTokenVerifier, "verify_token", fake_verify)


@pytest.mark.anyio
async def test_token_from_another_oauth_client_is_rejected(client, monkeypatch):
    _fake_parent_returning("999-someone-elses.apps.googleusercontent.com", monkeypatch)
    verifier = _entrypoint().AudienceBoundGoogleTokenVerifier(
        required_scopes=[ADS_SCOPE], expected_audience=OAUTH_CLIENT_ID
    )
    assert await verifier.verify_token("any-token") is None


@pytest.mark.anyio
async def test_token_from_our_oauth_client_is_accepted(client, monkeypatch):
    _fake_parent_returning(OAUTH_CLIENT_ID, monkeypatch)
    verifier = _entrypoint().AudienceBoundGoogleTokenVerifier(
        required_scopes=[ADS_SCOPE], expected_audience=OAUTH_CLIENT_ID
    )
    result = await verifier.verify_token("any-token")
    assert result is not None
    assert result.claims["aud"] == OAUTH_CLIENT_ID


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- supply chain -----------------------------------------------------------


def test_upstream_is_pinned_to_a_commit_not_a_branch():
    """GADS_MCP_REF must be a commit SHA, not a branch. See DESIGN.md for why."""
    import pathlib
    import re

    dockerfile = (
        pathlib.Path(__file__).resolve().parents[1] / "app" / "Dockerfile"
    ).read_text()

    match = re.search(r"^ARG GADS_MCP_REF=(\S+)", dockerfile, re.MULTILINE)
    assert match, "ARG GADS_MCP_REF not found in app/Dockerfile"

    ref = match.group(1)
    assert re.fullmatch(r"[0-9a-f]{40}", ref), (
        f"GADS_MCP_REF is {ref!r}, which is not a 40-character commit SHA. "
        "A branch or tag here means upstream can change what this deployment is "
        "allowed to do. Pin a commit."
    )


def test_fastmcp_has_an_upper_bound():
    """fastmcp must be capped (any bound, not specifically <4). See DESIGN.md."""
    import pathlib
    import re

    dockerfile = (
        pathlib.Path(__file__).resolve().parents[1] / "app" / "Dockerfile"
    ).read_text()

    specs = re.findall(r'"(fastmcp[^"]*)"', dockerfile)
    assert specs, "no fastmcp requirement found in app/Dockerfile"

    for spec in specs:
        assert "<" in spec, (
            f"fastmcp requirement {spec!r} has no upper bound. Upstream declares "
            "only a lower bound, so resolution will select the newest release, "
            "including pre-releases. Cap it."
        )


def test_openid_configuration_is_served(client):
    """OIDC discovery doc is served and points auth at us, token at Google."""
    doc = client.get("/.well-known/openid-configuration").json()

    assert doc["issuer"] == BASE
    assert doc["authorization_endpoint"] == f"{BASE}/oauth/authorize"
    assert doc["token_endpoint"] == "https://oauth2.googleapis.com/token"
    assert ADS_SCOPE in doc["scopes_supported"]
    assert "S256" in doc["code_challenge_methods_supported"]
