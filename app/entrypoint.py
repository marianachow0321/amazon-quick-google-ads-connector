"""Amazon Quick <-> Google Ads MCP connector — entrypoint.

Runs the upstream `google-ads-mcp` server (unmodified, installed as a
dependency) and adds the OAuth compatibility layer Amazon Quick's MCP client
needs. It bridges three gaps: it builds the ASGI app itself to skip FastMCP's
OAuth Proxy (so this service holds no tokens), serves three well-known discovery
documents, and requests `openid` alongside the Ads scope (Google omits the `sub`
claim the verifier requires unless `openid` is present).

Why each is load-bearing is in DESIGN.md ("Why a compatibility layer exists").
Don't remove `openid` from the scopes or drop a well-known route without reading
it — the tests in test/ guard both.
"""

from __future__ import annotations

import json
import logging
import os
from urllib.parse import urlencode

import uvicorn
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from ads_mcp.coordinator import mcp
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.google import GoogleTokenVerifier

# Importing the resource modules registers them on the `mcp` object. Upstream's
# server.py does the same; the imports look unused but are required.
from ads_mcp.resources import (  # noqa: F401
    discovery,
    metrics,
    release_notes,
    segments,
)

# Google Ads has no read-only scope; this scope permits writes. Read-only comes
# only from upstream registering read tools -- see DESIGN.md.
ADS_SCOPE = "https://www.googleapis.com/auth/adwords"

# `openid` is structural, not optional: GoogleTokenVerifier rejects any token
# whose tokeninfo has no `sub`, and Google returns `sub` only when `openid` is
# present. Still narrower than upstream's four scopes -- see DESIGN.md.
REQUESTED_SCOPES = f"openid {ADS_SCOPE}"
ADVERTISED_SCOPES = ["openid", ADS_SCOPE]

GOOGLE_AUTHORIZE_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

logger = logging.getLogger(__name__)

# FastMCP's Google verifier logs *why* it rejected a token (bad tokeninfo,
# missing `aud`, missing scope) only at DEBUG. Scoped to that one logger; it
# records reasons, never credentials.
logging.getLogger("fastmcp.server.auth.providers.google").setLevel(logging.DEBUG)

PORT = int(os.environ.get("PORT", "8080"))

# Public base URL of this service (API Gateway stage URL). Injected by CDK.
BASE_URL = os.environ.get("PROXY_URL", f"http://localhost:{PORT}").rstrip("/")


def _load_developer_token() -> None:
    """Resolve the developer token into `GOOGLE_ADS_DEVELOPER_TOKEN` (what
    upstream reads), preferring a Secrets Manager ARN over the plaintext env
    var. See README / DESIGN.md for why the ARN path is preferred.
    """
    if os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN"):
        return

    secret_arn = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN_SECRET_ARN")
    if not secret_arn:
        raise RuntimeError(
            "No developer token available. Set GOOGLE_ADS_DEVELOPER_TOKEN, or "
            "set GOOGLE_ADS_DEVELOPER_TOKEN_SECRET_ARN to a Secrets Manager "
            "secret holding it. The Google Ads API rejects every request "
            "without a developer token."
        )

    # Imported lazily: boto3 is only needed on the Secrets Manager path, and its
    # import is not free on a cold start.
    import boto3

    region = secret_arn.split(":")[3]
    client = boto3.client("secretsmanager", region_name=region)
    secret = client.get_secret_value(SecretId=secret_arn)
    token = secret.get("SecretString")
    if not token:
        raise RuntimeError(
            f"Secret {secret_arn} has no SecretString. Store the developer "
            "token as a plaintext secret, not as binary."
        )
    os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"] = token.strip()


class AudienceBoundGoogleTokenVerifier(GoogleTokenVerifier):
    """GoogleTokenVerifier that also pins the token's `aud` to our OAuth client.

    Without the pin, any Ads-scoped Google token -- including one minted for an
    unrelated OAuth app -- would be accepted and would borrow our developer
    token. Stable FastMCP 3.x has no built-in audience check. See DESIGN.md.
    """

    def __init__(self, *, expected_audience: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._expected_audience = expected_audience

    async def verify_token(self, token: str) -> AccessToken | None:
        access_token = await super().verify_token(token)
        if access_token is None:
            return None

        audience = (access_token.claims or {}).get("aud")
        if audience != self._expected_audience:
            logger.warning(
                "Rejected a token issued to a different OAuth client "
                "(expected audience %r, got %r)",
                self._expected_audience,
                audience,
            )
            return None
        return access_token


OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
if not OAUTH_CLIENT_ID:
    raise RuntimeError(
        "GOOGLE_OAUTH_CLIENT_ID is not set. It is required: without it any "
        "Google token bearing the Ads scope would be accepted, which would lend "
        "this service's developer token to arbitrary callers."
    )

_load_developer_token()

# Validate the Google token, require the Ads scope, and require our own OAuth
# client as the audience. `required_scopes` also populates `scopes_supported`.
mcp.auth = AudienceBoundGoogleTokenVerifier(
    required_scopes=[ADS_SCOPE],
    expected_audience=OAUTH_CLIENT_ID,
)


@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
async def oauth_protected_resource(request: Request) -> JSONResponse:
    """RFC 9728 protected-resource metadata -- one of the three discovery docs
    Quick may fetch (see DESIGN.md). `scopes_supported` advertises the scopes."""
    return JSONResponse(
        {
            "resource": BASE_URL,
            "authorization_servers": [BASE_URL],
            "scopes_supported": ADVERTISED_SCOPES,
            "bearer_methods_supported": ["header"],
        }
    )


@mcp.custom_route("/.well-known/openid-configuration", methods=["GET"])
async def openid_configuration(request: Request) -> JSONResponse:
    """OpenID Connect discovery -- the document a live Quick connector was
    observed to fetch. Authorization points at us (so we can force the Ads
    scope); token endpoint and JWKS point at Google, which issues and signs.
    See DESIGN.md for why all three well-known docs are served.
    """
    return JSONResponse(
        {
            "issuer": BASE_URL,
            "authorization_endpoint": f"{BASE_URL}/oauth/authorize",
            "token_endpoint": GOOGLE_TOKEN_ENDPOINT,
            "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
            "userinfo_endpoint": "https://openidconnect.googleapis.com/v1/userinfo",
            "scopes_supported": ADVERTISED_SCOPES,
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_post",
                "client_secret_basic",
            ],
        }
    )


@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
async def oauth_authorization_server(request: Request) -> JSONResponse:
    """RFC 8414 authorization-server metadata. Authorization points at our own
    /oauth/authorize (to force the Ads scope); token points straight at Google."""
    return JSONResponse(
        {
            "issuer": BASE_URL,
            "authorization_endpoint": f"{BASE_URL}/oauth/authorize",
            "token_endpoint": GOOGLE_TOKEN_ENDPOINT,
            "scopes_supported": ADVERTISED_SCOPES,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
        }
    )


@mcp.custom_route("/oauth/authorize", methods=["GET"])
async def oauth_authorize(request: Request) -> RedirectResponse:
    """Rewrite the scope, then hand off to Google.

    Anything the client sent is preserved (client_id, redirect_uri, state, PKCE
    challenge) except `scope`, which we overwrite. `access_type=offline` plus
    `prompt=consent` are needed for Google to return a refresh token.
    """
    params = dict(request.query_params)
    params["scope"] = REQUESTED_SCOPES
    params.setdefault("access_type", "offline")
    params.setdefault("prompt", "consent")
    # RFC 8707 -- Google's authorization endpoint rejects unknown params in some
    # configurations, and we do not need audience binding here.
    params.pop("resource", None)
    return RedirectResponse(
        f"{GOOGLE_AUTHORIZE_ENDPOINT}?{urlencode(params)}", status_code=302
    )


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


class RequestLogMiddleware:
    """Log method, path, JSON-RPC method, and status -- nothing else.

    Pure ASGI (not BaseHTTPMiddleware) so it can't disturb the lifespan FastMCP's
    session manager needs. The path is logged without its query string (PKCE +
    state live there); the JSON-RPC `method` is logged but never `params` (which
    carry customer IDs and query text).
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        rpc_method = None

        if scope["method"] == "POST":
            chunks: list[dict] = []
            while True:
                message = await receive()
                chunks.append(message)
                if not message.get("more_body"):
                    break
            raw = b"".join(m.get("body", b"") for m in chunks)
            try:
                rpc_method = json.loads(raw).get("method")
            except Exception:
                rpc_method = None

            replayed = False

            async def receive_replay():
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {
                        "type": "http.request",
                        "body": raw,
                        "more_body": False,
                    }
                return {"type": "http.disconnect"}

            receive = receive_replay

        status_code = None

        # Presence and scheme only, never the credential -- distinguishes "no
        # token sent" from "token sent but rejected", which the status can't.
        auth_state = "no-auth-header"
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                scheme = value.split(b" ", 1)[0].decode("latin-1", "replace")
                auth_state = f"auth={scheme}({len(value)}b)"
                break

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            logger.info(
                "%s %s%s -> %s [%s]",
                scope["method"],
                scope["path"],
                f" [{rpc_method}]" if rpc_method else "",
                status_code,
                auth_state,
            )


def build_app():
    """Build the ASGI app.

    `stateless_http=True` because Lambda gives no guarantee that two requests
    land in the same execution environment. `json_response=True` makes responses
    plain JSON instead of SSE, which keeps API Gateway in buffered mode.
    """
    return RequestLogMiddleware(
        mcp.http_app(
            path="/",
            stateless_http=True,
            json_response=True,
        )
    )


app = build_app()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, access_log=False)
