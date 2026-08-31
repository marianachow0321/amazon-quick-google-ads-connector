# End-to-end setup guide

A start-to-finish runbook for wiring Google Ads into Amazon Quick through this
connector.

The work splits into four phases:

1. [Google Ads side](#phase-1--google-ads-manager-account--developer-token) — a manager account and a developer token
2. [Google Cloud side](#phase-2--google-cloud-oauth-client) — an OAuth client and consent screen
3. [AWS side](#phase-3--deploy-to-aws) — deploy the connector
4. [Amazon Quick side](#phase-4--create-the-quick-connector) — create and authorize the connector

Then [account linking](#phase-5--link-the-accounts-you-want-to-query) and
[verification](#phase-6--verify).

---

## Phase 1 — Google Ads: manager account + developer token

### 1.1 Create a manager account (MCC)

The developer token lives in the **API Center**, and **only manager accounts
have an API Center**. A plain advertising account will show
*"The API Center is only available to manager accounts."*

Create one at <https://ads.google.com/home/tools/manager-accounts/> → **Go to
Manager Accounts**.

### 1.2 Get the developer token

In the manager account, open <https://ads.google.com/aw/apicenter>, complete the
API Access form, and accept the terms. The token is a 22-character string.

Note its **Access level**: **Test Account** reaches only test accounts, while
**Explorer, Basic, or Standard** can query production. Explorer — which Google
may grant automatically at signup but you cannot request — is enough for this
read-only connector. If you land on Test, the only manual path is a multi-day
Basic Access application.

---

## Phase 2 — Google Cloud: OAuth client

The developer token identifies *the application*; the OAuth client is how each
*user* signs in. They come from different consoles — the token from Google Ads,
the OAuth client from Google Cloud.

### 2.1 Enable the Google Ads API

<https://console.cloud.google.com/flows/enableapi?apiid=googleads.googleapis.com>
— select (or create) a project and enable it.

> **Gotcha:** one Cloud project can carry only one developer token. If the
> project was previously used with a different Google Ads token, create a fresh
> project.

### 2.2 Configure the consent screen (Google Auth Platform)

Google reorganised this area into the **Google Auth Platform**. Work through the
tabs at <https://console.cloud.google.com/auth/overview>:

- **Branding** — leave logo and app-domain links blank for a Testing app; fill:
  - **App name** — any label, e.g. `Google Ads Connector`; it appears on the
    consent screen.
  - **User support email** — pick your own address from the dropdown.
  - **Developer contact information → Email addresses** — your email again.
  - **Authorized domains** — add `amazon.com`. Google only accepts the
    registrable domain, not the `quicksight.aws.amazon.com` subdomain your
    redirect URI uses.
- **Audience**:
  - **User type** — choose **External** (Internal requires a Google Workspace
    organization).
  - **Publishing status** — leave it on **Testing**; don't click *Publish app*
    (this app can't be verified anyway — see the note below).
  - **Test users** — add the Google account you'll sign into Quick with. In
    Testing, only listed accounts can authorize; anyone else is blocked with
    **`access_denied`** at the consent screen.

> **Gotcha:** because the Quick redirect URI forces `amazon.com` into Authorized
> domains and you can't verify ownership of it, this app can **never be
> Published** (`.../adwords` is a sensitive scope). You're limited to **External +
> Testing** (100 test users, counted for the app's lifetime) or, with Google
> Workspace, **Internal** (no cap). For a demo, Testing is fine.

### 2.3 Create the OAuth client

<https://console.cloud.google.com/auth/clients> → **Create client**:

- **Application type: Web application** — this is the only type that accepts an
  `https` redirect URI.
- **Authorized redirect URIs**, substituting your Quick region:
  ```
  https://us-east-1.quicksight.aws.amazon.com/sn/oauthcallback
  ```
  (The deploy prints the exact value; you can come back and correct it.)

Copy the **Client ID** (`NNNN...-xxxx.apps.googleusercontent.com`) **and the
Client Secret** — you need both in Quick.

> **Gotcha:** you will read elsewhere that PKCE public clients need no secret.
> Google's *Web application* client is a **confidential client**: its token
> endpoint rejects the code exchange with `client_secret is missing` even with
> PKCE. There is no public-client type that takes an `https` redirect URI. So in
> Quick you must supply the secret and set *Public OAuth client = No*.

---

## Phase 3 — Deploy to AWS

Prerequisites: Node.js, AWS CDK, and a container builder. Works cleanly in **AWS
CloudShell** (Docker pre-installed, x86_64, image builds in ~30 s). On an Apple
Silicon Mac use Finch and prefix commands with `CDK_DOCKER=finch`.

```bash
git clone https://github.com/marianachow0321/amazon-quick-google-ads-connector.git
cd amazon-quick-google-ads-connector
npm install
npx cdk bootstrap        # first time in the account/region only
```

Deploy — inline token is simplest for a demo:

```bash
npx cdk deploy \
  -c oauthClientId=NNNNNNNNNNNN-xxxx.apps.googleusercontent.com \
  -c developerToken=YOUR_22_CHAR_TOKEN \
  -c loginCustomerId=1234567890
```

- `oauthClientId` — **required**. Your Google Cloud OAuth client ID. The server
  rejects any token whose `aud` is not this ID, so it doubles as an access
  control (see [DESIGN.md](DESIGN.md)).
- `developerToken` / `developerTokenSecretArn` — **required, exactly one**. Your
  22-character token inline, or its Secrets Manager ARN (below).
- `loginCustomerId` — **required**. Your manager account (MCC) ID, digits only,
  no dashes. The token comes from an MCC, so queries run through it; without it
  every client-account query fails with `USER_PERMISSION_DENIED`. (`bin/app.ts`
  refuses to deploy without it.)

Beyond a personal test, use Secrets Manager — inlining leaves the token readable
in the CloudFormation template and Lambda console:

```bash
aws secretsmanager create-secret --name google-ads-developer-token \
    --secret-string YOUR_22_CHAR_TOKEN

npx cdk deploy \
  -c oauthClientId=NNNNNNNNNNNN-xxxx.apps.googleusercontent.com \
  -c developerTokenSecretArn=arn:aws:secretsmanager:us-east-1:123456789012:secret:google-ads-developer-token-AbCdEf \
  -c loginCustomerId=1234567890
```

The deploy prints three outputs: the **MCP server endpoint**, the
**Authorization URL**, and the exact **redirect URI** to allowlist on the OAuth
client (Phase 2.3).

> **Gotcha:** the first request after a deploy can take ~28 s — that is Lambda
> pulling and optimising the container image, a one-off. Steady-state cold start
> is ~8 s, warm ~0.7 s.

---

## Phase 4 — Create the Quick connector

Quick console → **Connectors** → **Create for your team** → **Model Context
Protocol (MCP)**.

| Field | Value |
|---|---|
| Name | `Google Ads` |
| MCP server endpoint | the `MCP server endpoint` output (no trailing slash) |
| Connection type | Public network |
| Authentication | User authentication (OAuth) |
| Authorization URL | the `Authorization URL` output (`.../oauth/authorize`) |
| Token URL | `https://oauth2.googleapis.com/token` |
| Client ID | your Google Cloud OAuth client ID |
| Client Secret | your Google Cloud OAuth client secret |
| **Public OAuth client** | **No** |

Authorize with the Google account you added as a test user. You will see an
"unverified app" warning — that is expected (the app is in Testing and can never
be verified, per Phase 2.2); click through it. The consent screen should ask for
one permission: manage your AdWords campaigns.

On success three tools are registered:
`customers_list_accessible_customers`, `metadata_get_resource_metadata`,
`search_search`.

> **Gotcha — a failed connector cannot be repaired.** Quick freezes the tool list
> at registration. If creation fails, delete it and create a new one (a different
> name is fine) rather than editing.

---

## Phase 5 — Link the accounts you want to query

Listing accounts works immediately, but **querying a client account's data needs
that account linked under your manager account** — and the link must be accepted
on both ends.

1. In the manager account: **Accounts → Sub-account settings → Link existing
   account**, enter the client account's customer ID, send the request.
2. In the client account: **Admin → Access and security → Managers tab →
   Accept**.

> **Gotcha — the `login-customer-id` error misdirects you.** Querying an unlinked
> client account returns *"User doesn't have permission to access customer... the
> manager's customer id must be set in the `login-customer-id` header."* That
> points you at the header — but if you already deployed `loginCustomerId`, the
> real cause is almost always that **the account isn't linked under the manager
> yet, or the invitation is still pending**. Complete the link and the query goes
> through.

> **Note:** a manager account holds no campaigns of its own — querying metrics on
> the MCC itself correctly returns *"Metrics cannot be requested for a manager
> account."* Query the client accounts underneath it.

---

## Phase 6 — Verify

Ask Quick, against a client account that has campaigns:

```
Show me the campaigns in account 1234567890
```

- **Real rows back** → the whole chain works end to end.
- **Empty result** → not an error; that account simply has no campaigns, or the
  link is still propagating (wait a few minutes).
- **Access error** → revisit Phase 5 (linking) or Phase 1.2 (token access level).

---

## Quick reference — what plugs in where

| Value | Comes from | Used in |
|---|---|---|
| Developer token | Google Ads API Center (in the MCC) | CDK `-c developerToken` (or Secrets Manager) |
| Manager account ID | Google Ads account switcher (the "Manager" one) | CDK `-c loginCustomerId` (digits only) |
| OAuth client ID | Google Cloud → Auth → Clients | CDK `-c oauthClientId` **and** Quick |
| OAuth client secret | Google Cloud → Auth → Clients | Quick only |
| MCP server endpoint | CDK deploy output | Quick |
| Authorization URL | CDK deploy output | Quick |
