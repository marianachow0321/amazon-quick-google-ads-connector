# Google Ads MCP connector for Amazon Quick

Deploys the official [`googleads/google-ads-mcp`](https://github.com/googleads/google-ads-mcp)
server on AWS Lambda, behind the OAuth compatibility layer Amazon Quick's MCP
client needs. Google hosts no remote MCP endpoint for Google Ads, so this repo is
what you deploy to stand up your own.

> **Setting this up? Start with [SETUP.md](SETUP.md)** — a start-to-finish
> runbook (manager account, developer token, OAuth client, deploy, Quick, account
> linking) with a callout at every step that is easy to get wrong.
>
> This README is the operational reference. For *why* the connector is built the
> way it is, see [DESIGN.md](DESIGN.md).

## What you need

- A **Google Ads developer token** with at least Explorer access (Test-level
  cannot reach production accounts), from the API Center of a manager account (MCC).
- **Google Ads API enabled** in a Google Cloud project, and an **OAuth 2.0 client**
  (Web application type) in that project.
- An **Amazon Quick Enterprise** subscription.

## Deploy

Works in AWS CloudShell (Docker pre-installed, image builds in ~30 s).

```bash
git clone https://github.com/marianachow0321/amazon-quick-google-ads-connector.git
cd amazon-quick-google-ads-connector
npm install

# First time in this account/region only:
npx cdk bootstrap

npx cdk deploy \
  -c oauthClientId=YOUR_CLIENT_ID.apps.googleusercontent.com \
  -c developerToken=YOUR_DEVELOPER_TOKEN \
  -c loginCustomerId=1234567890
```

All three `-c` flags are **required**:

- `oauthClientId` — your Google Cloud OAuth client ID (also an access control).
- `developerToken` — your 22-character token. For anything shared, use
  `developerTokenSecretArn` instead (from Secrets Manager).
- `loginCustomerId` — your manager account (MCC) ID, digits only.

See [SETUP.md](SETUP.md) Phase 3 for what each flag does and the Secrets Manager
path.

The deploy prints the **MCP server endpoint**, **Authorization URL**, and the
exact **redirect URI** to allowlist on your OAuth client.

## Configure Amazon Quick

Quick console → **Connectors** → **Create for your team** → **Model Context Protocol (MCP)**.

| Field | Value |
|---|---|
| MCP server endpoint | the `MCP server endpoint` output (no trailing slash) |
| Connection type | Public network |
| Authentication | User authentication (OAuth) |
| Authorization URL | the `Authorization URL` output |
| Token URL | `https://oauth2.googleapis.com/token` |
| Client ID | your Google Cloud OAuth client ID |
| Client Secret | your Google Cloud OAuth client secret |
| Public OAuth client | **No** (see [SETUP.md](SETUP.md) Phase 2.3) |

On success, three tools register: `customers_list_accessible_customers`,
`metadata_get_resource_metadata`, `search_search`.

## Troubleshooting

These are the Google Ads-side issues this connector cannot solve for you. For the
Quick side, see Quick's own [MCP integration
guide](https://docs.aws.amazon.com/quicksuite/latest/userguide/mcp-integration.html).

**`USER_PERMISSION_DENIED` on a client account.** If you set `loginCustomerId`
and still hit *"User doesn't have permission to access customer,"* the cause is
usually that the account is not linked under your manager account yet (or the
invitation is still pending). The fix is to link it, not to change the header —
see [SETUP.md](SETUP.md) Phase 5 for the steps.

**"Metrics cannot be requested for a manager account."** You queried the MCC
itself, which holds no campaigns of its own. Query one of the client accounts
under it instead.

**"Only approved for test accounts."** The developer token is Test-level and
cannot read production accounts from any deployment — see SETUP.md Phase 1.2 on
getting Explorer.

**Cold start is ~8 s; the first call after a deploy is ~28 s.** That first call is
Lambda pulling and optimising the container image — a one-off, not a fault. Warm
requests are ~0.7 s.

## Security notes

This deploys a **public internet endpoint that carries a high-privilege developer
token**, so understand the exposure before you ship it.

**Developer token.** With `-c developerTokenSecretArn` it is read from Secrets
Manager at init and never appears in the template or console, and the function is
granted read on that one secret only. With `-c developerToken` it is a plain
environment variable — fine for a demo, not for anything shared.

**Keep the OAuth consent screen on Testing (or Internal).** The daily operation
limit is counted per developer token across everyone who consents, so limiting
who can consent protects your quota.

**No WAF.** Abuse control is API Gateway throttling (100 rps / 200 burst). A
sustained flood still costs Lambda invocations; add a WAF WebACL if this becomes
more than a demo.

The endpoint is public but not open: every request's token is validated against
Google and pinned to `oauthClientId`, the function has a least-privilege IAM
role, and `/oauth/authorize` is not an open redirect.
