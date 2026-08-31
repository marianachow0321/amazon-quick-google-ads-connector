# Design notes

Why this connector is built the way it is. You do not need any of this to deploy
it — [SETUP.md](SETUP.md) is the runbook and [README.md](README.md) is the
operational reference. This document is for anyone changing the code, who needs
to know which decisions are load-bearing.

## Why a compatibility layer exists

The upstream [`googleads/google-ads-mcp`](https://github.com/googleads/google-ads-mcp)
is consumed as an unmodified dependency. Three specific incompatibilities between
what it does by default and what Amazon Quick needs are bridged in
[`app/entrypoint.py`](app/entrypoint.py):

| Problem | Fix |
|---|---|
| Upstream only serves streamable-HTTP when `GOOGLE_ADS_MCP_OAUTH_CLIENT_ID`/`_SECRET` are set, and that branch builds FastMCP's `GoogleProvider` — OAuth Proxy, where this server becomes the authorization server, mints its own JWTs and holds the Google tokens. Upstream does support persisting those tokens (`GOOGLE_ADS_MCP_STORAGE_TYPE` takes `filetree`, `redis`, `firestore` or `memory`), and documents Cloud Run as the target. On Lambda none land well: `filetree` needs a writable disk, `memory` dies with the execution environment, `firestore` is GCP, and `redis` means ElastiCache plus a VPC-attached function — paying for a token store, and cold-start ENI setup, to hold tokens we have no need to hold | Let Quick exchange the code directly with Google, so this service stores nothing. Build the ASGI app ourselves and skip OAuth Proxy entirely |
| Discovery: **against a live connector, Quick fetched `/.well-known/openid-configuration` and looped on it returning 404**, never obtained a token, and every MCP call sat at 401 until creation failed. (Note: [Quick's docs](https://docs.aws.amazon.com/quicksuite/latest/userguide/mcp-integration.html) describe discovery as RFC 9728 protected-resource metadata with a fallback to the well-known root — the observed behaviour was the OIDC well-known document, so serve that too rather than relying on one path) | Serve an OIDC discovery document whose `authorization_endpoint` is ours (so we can force the scope) and whose `token_endpoint` and `jwks_uri` point at Google. The RFC 9728 protected-resource document is also served, so both discovery paths are covered |
| Google's tokeninfo omits the `sub` claim unless the token carries `openid`, and FastMCP's `GoogleTokenVerifier` rejects any token without `sub`. Requesting only the Ads scope fails *every* token with `Google tokeninfo missing 'sub' claim` | Request `openid` alongside the Ads scope |

## Scope divergence from upstream

In OAuth-Proxy mode upstream requests four scopes: `openid`, `userinfo.email`,
`userinfo.profile` and `adwords`. This connector requests two: `openid` and
`https://www.googleapis.com/auth/adwords`.

`openid` is not optional — the verifier structurally needs it (see the third row
above). What this connector drops is `userinfo.email` and `userinfo.profile`:
those drive OAuth Proxy's consent and identity flow, which we do not use, and no
tool reads the user's identity.

Because that exchange happens directly against Google (above), the bearer token
arriving at the server *is* a Google access token — and upstream's
`_create_credentials()` already prefers the FastMCP access token over Application
Default Credentials, so no patch to upstream is required.

## Architecture

```
                  ┌──────────────────────────────────────────────┐
   Amazon Quick   │  API Gateway (REST, REGIONAL, throttled)     │
        │         └──────────────────────┬───────────────────────┘
        │                                │  proxy, verbatim
        │                                ▼
        │         ┌──────────────────────────────────────────────┐
        │         │  Lambda container                            │
        │         │    Lambda Web Adapter → uvicorn              │
        │         │      /                        MCP (JSON)     │
        │         │      /.well-known/*           OAuth metadata │
        │         │      /oauth/authorize         302 → Google   │
        │         │      /health                                 │
        │         │    google-ads-mcp (upstream, unmodified)     │
        │         └──────────────────────┬───────────────────────┘
        │                                │  Bearer = Google token
        │                                ▼
        │                        Google Ads API
        │
        └── token exchange goes straight to oauth2.googleapis.com
            (this service never sees or stores tokens)
```

Standard AWS Lambda with a container image, fronted by the Lambda Web Adapter so
the same image runs under `docker run -p 8080:8080` for local debugging. Not
App Runner (closed to new customers), not Fargate (a standing 24/7 charge for a
sporadic workload), not the standalone MicroVM primitive (lifecycle
orchestration, 8h state cap, no CDK story).

## Supply-chain pinning

The image pins the things that can change *behaviour or privilege*: the upstream
commit, the Lambda Web Adapter version, the uv version, and `fastmcp<4`.

The upstream pin is a security control, not hygiene. Because this connector is
read-only *only* by virtue of upstream's tool surface (see "'Read-only' is
enforced by tool surface" below), tracking a moving branch would let a future
upstream release silently gain write access to the advertising accounts. Bump
`GADS_MCP_REF` deliberately, after reading the diff. A test
(`test_upstream_is_pinned_to_a_commit_not_a_branch`) fails if the ref is not a
40-char SHA.

The `fastmcp<4` cap exists because upstream declares only `fastmcp>=3.2.0`, and
resolution otherwise selects a 4.x pre-release. A test
(`test_fastmcp_has_an_upper_bound`) enforces that a cap exists — not that it is
`<4` specifically, so a deliberate move to `<5` after testing is allowed.

The base image is the deliberate exception: `python:3.14-slim` is a floating tag,
so every rebuild picks up Debian's security updates. Pinning it to a digest would
buy byte-for-byte reproducibility at the cost of freezing the image at one
moment's CVE state — for an OS layer that trade is the wrong way round. Upstream's
tool list and FastMCP's major version change what this service is *allowed to
do*; the base image only changes what it is *built on*.

## Why `oauthClientId` is mandatory

The MCP endpoint is public, and the server attaches *your* developer token to
every upstream call. The verifier pins the token's `aud` claim to `oauthClientId`
(`AudienceBoundGoogleTokenVerifier`), and the container refuses to start without
it.

Without the pin, the verifier would only check that a token carries the Ads
scope. Anyone could then spin up their own Google OAuth client (a five-minute,
free task), mint an Ads-scoped token through it — that token's `aud` is *their*
client ID — and present it to your endpoint. The server would accept it and query
their Google Ads accounts using your developer token, burning your quota and your
API-policy standing.

This is orthogonal to keeping the consent screen on Testing, and both are needed:

- **`aud` pin** blocks tokens minted through *any other* OAuth client.
- **Testing consent screen** blocks unauthorized people from consenting through
  *your* client.

Neither substitutes for the other — each closes a cheap, distinct attack path.

## "Read-only" is enforced by tool surface, not by scope

Upstream's own comment says so: Google publishes no read-only Ads scope, so the
token this service accepts *permits writes*. The connector is read-only because
upstream registers only read tools. Protect the endpoint and the developer token
accordingly, and see the upstream pin above.

## Cold start and the 29 s timeout

Cold start is ~8 s (first call after a deploy ~28 s while Lambda pulls and
optimises the image). The cost is the Google Ads SDK importing thousands of
generated protobuf modules at init — the expense is descriptor registration, not
parsing, so precompiling bytecode does not help (measured: no meaningful
difference). Raising memory does not help either — the import is
single-threaded, so the extra vCPU that more memory buys goes unused (measured cold start at 1024/2048/3008 MB: ~7.9 / ~6.5 / ~6.5 s; the
2048->3008 step is within noise). Do not spend time on either optimisation.

The function timeout is 29 s to match API Gateway's REST integration ceiling: the
gateway returns 504 at 29 s regardless, and a longer Lambda timeout would only
bill for work the caller can no longer receive. The ceiling is raisable on
Regional REST APIs (Service Quotas `L-E5AE38E3`), but it is an account-level
change that can cost throttle capacity, and an 8 s cold start leaves ~21 s for
the query — ample for GAQL — so the numbers do not call for it. HTTP APIs cap at
30 s and cannot be raised, which is one reason this uses a REST API.

## Quick reads tools, not Resources

Quick registers MCP *tools* as actions and does not read MCP *Resources*.
Upstream ships four Resources (`discovery-document`, `metrics`, `segments`,
`release-notes`) that exist to ground an LLM in the Google Ads API surface; under
Quick they are inert, so `metadata_get_resource_metadata` (a tool) carries that
weight instead. Whether this measurably hurts GAQL quality is **untested** — noted here
so a maintainer who sees weak queries knows this is a candidate cause, and that
pasting metric/segment summaries into the agent instructions is a possible
mitigation. Do not present it as a known problem; it is a hypothesis.

## Running the tests

```bash
python3 -m venv .venv
./.venv/bin/pip install "fastmcp>=3.2.0,<4" uvicorn httpx pytest anyio
./.venv/bin/python -m pytest test -q
```

The suite stubs out `ads_mcp`, so it runs in seconds without the Google Ads SDK.
It covers what is most likely to silently break the handshake or the security
model: the advertised scopes, `/oauth/authorize` forcing scope while preserving
PKCE, rejection of a token from a different OAuth client, and the two supply-chain
invariants above (`test_upstream_is_pinned_to_a_commit_not_a_branch`,
`test_fastmcp_has_an_upper_bound`).
