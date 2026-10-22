# MCP server — design

**Status:** approved by the operator on 2026-10-09: the design section by
section, then this written spec as a whole, **including the exact wording of
invariant 1c in §9**, which goes into CLAUDE.md with the implementation.

**Operator decisions (2026-10-09):**

| # | Question | Answer |
|---|---|---|
| Q1 | What may an AI do over MCP? | Everything the person's role allows; critical things only under approval. |
| Q2 | The acts 1b(f), 2a(e) and three specs reserve to a person? | Amend the rules: the AI may reach them too (new invariant 1c). |
| Q3 | Where is approval given? | In the AI client: granular tools, each set to *Always allow* or *Needs approval* there, "just like any MCP server today". |
| Q4 | Which AI apps may connect? | Any. Open dynamic client registration, hardened (§3.3). |
| Q5 | A platform switch for the restricted acts? | No. Rejected: the client's per-tool setting is the gate. |
| Q6 | How does the connection authenticate? | **OAuth, and only OAuth.** |

This is independent of the Spanish design written the same day
(`2026-10-09-spanish-language-design.md`).

---

## 1. What the operator does

1. **Settings → AI connections** (a new settings tab, `SETTINGS_TABS`):
   turn on **Allow AI apps to connect over MCP** for the organization. It is
   off until an org-admin turns it on. The tab shows the connection address,
   `https://itms.toeshee.com/mcp`.
2. Grant `mcp.use` to the roles that may connect. Org-admins hold it by
   default.
3. In Claude (or any MCP client): **Add custom connector**, paste the
   address. Claude registers itself with the platform's sign-in, opens the
   ITMS sign-in page, the person signs in (MFA as usual) and approves the
   consent screen naming the app.
4. In Claude's connector settings, each tool is set to **Always allow** or
   **Needs approval**. Read-only tools are marked so, and destructive ones
   too, so clients can default sensibly.
5. **Settings → AI connections** lists each AI app with access, who uses it
   and when it was last used, with **Revoke**: per person, or for every
   person in the organization.

## 2. Transport and placement

- **Streamable HTTP** at `/mcp`, the current MCP transport, served by the
  existing `api` container. There is no new service.
- The official Python SDK (`mcp`) is added to `backend/pyproject.toml`. The
  server runs **stateless** (`stateless_http`, JSON responses), so any
  uvicorn worker can serve any request and nothing is pinned to a process.
- It is mounted in `create_app()` (`app/main.py`), and `lifespan` starts the
  SDK's session manager.
- **Traefik:** the `api` router's rule gains ``PathPrefix(`/mcp`)`` and
  ``Path(`/.well-known/oauth-protected-resource`)`` (plus its
  `/mcp`-suffixed form). Without this, the web catch-all would answer those
  paths with `index.html` and a 200. Streaming already passes through the
  stack; the in-app AI's SSE route proves it.
- The address is `ITS_PUBLIC_CONSOLE_URL` + `/mcp`. With that unset, `/mcp`
  answers 503 saying so, as the MSI download does.

## 3. OAuth, and only OAuth

### 3.1 The flow (MCP authorization spec, current revision)

1. An unauthenticated request to `/mcp` gets **401** with
   `WWW-Authenticate: Bearer resource_metadata="https://itms.toeshee.com/.well-known/oauth-protected-resource/mcp"`.
2. That **protected resource metadata** (RFC 9728) names:
   - `resource`: `https://itms.toeshee.com/mcp`;
   - `authorization_servers`: the Keycloak realm issuer,
     `https://itms.toeshee.com/realms/it-suite`;
   - `scopes_supported`: `["mcp"]`.
3. The client reads the realm's metadata, which Keycloak serves at
   `/realms/it-suite/.well-known/openid-configuration` and
   `/realms/it-suite/.well-known/oauth-authorization-server`. It registers
   itself at Keycloak's registration endpoint (§3.3) and runs the
   authorization-code flow **with PKCE**, requesting scope `mcp`.
4. The person signs in at Keycloak, with the realm's MFA and passkey
   policies, and consents.
5. The client calls `/mcp` with `Authorization: Bearer <access token>`.

There is no API key, no static token, no basic auth and no client-credentials
grant. A request without a valid OAuth access token gets only the 401 above.

### 3.2 MCP tokens are a separate kind of token

- The realm gains a client scope **`mcp`** with two mappers:
  - an **audience mapper** for `https://itms.toeshee.com/mcp`;
  - the **`org` user-attribute mapper**, the same mapper `it-suite-web`
    carries per client today.
- `/mcp` accepts a token only if **its audience includes the MCP resource**,
  in addition to the existing checks (RS256, issuer, `exp`/`iat`/`sub`, the
  org claim and the user's status — `security.validate_token`, `deps.auth_context`).
- **Console tokens do not work on `/mcp`.** `it-suite-web` never requests
  scope `mcp`, so its tokens have no MCP audience.
- **MCP tokens do not work on `/api`.** Registered AI apps are not given the
  console's `it-suite` audience, which `it-suite-web` carries per client and
  not as a realm default. A token held by an AI app therefore cannot be
  replayed against `/api` to reach what MCP excludes, such as credential
  checkout.

### 3.3 Open registration, hardened (Q4)

Keycloak's anonymous client registration is enabled for the realm, and
`keycloak-init` converges its policies on every deploy:

- **Trusted Hosts** is removed for anonymous registration. This is the
  operator's choice: any AI app may register. **Only on an explicit opt-in**
  (operator decision, 2026-10-15): `keycloak-init` removes it only when
  `ITS_MCP_REGISTRATION=open` and `ITS_PUBLIC_CONSOLE_URL` are both set, and
  keeps or restores it otherwise, naming the missing setting. Everything else
  in this section converges either way, so opening later is the flag and a
  redeploy.
- **Consent Required**: on. The consent screen names the registering app, so
  a person sees which app they are letting in.
- **Allowed Client Scopes**: `mcp`, `openid` and `profile` only. A registered
  app can never obtain the console audience.
- **Allowed Protocol Mappers**: none beyond the scope's own. Apps cannot add
  mappers, for example one that copies another attribute into a claim.
- **Full Scope Allowed**: off.
- **Max Clients**: 200 registered apps per realm.
- Registered apps are public clients, authorization-code flow with PKCE only:
  no direct-access grants, no implicit flow, no service accounts. A
  client-policy executor enforces this (`pkce-enforcer`, `confidential-client`
  off, `secure-redirect-uris`).

Registration alone grants nothing. Every call still needs a person's
sign-in, `mcp.use` and the organization switch.

### 3.4 Revocation is immediate

- **Revoke** for one person removes that person's consent and sessions for
  the app in Keycloak, through the admin client
  (`user_admin/kc_client.py`).
- An access token is a signed JWT that stays valid until it expires, so
  revocation is also recorded in a new table, `mcp_revocation`, with
  `(org_id, user_id or null, client_id, revoked_at)`. `/mcp` refuses any
  token whose `azp` is a revoked client for that person (or for the whole
  organization) and whose `iat` is earlier than `revoked_at`. The refusal
  takes effect on the next request, not at token expiry.
- Registered apps are realm-wide and shared by every organization in a
  multi-tenant install, so **Revoke never deletes the Keycloak client**. One
  organization's revocation never reaches another's users.

## 4. Tools

### 4.1 One tool per action, generated from the live API

- At start-up, every route under `/api/v1` is enumerated with
  `router_inspect.flatten(app)` (`app.routes` alone misses the wrapped
  routers), and each becomes one MCP tool unless it is excluded (§5).
- **Name:** derived from method and path. `GET /devices` becomes
  `devices_list`, `GET /devices/{device_id}` `devices_get`, and
  `POST /devices/{device_id}/review/approve` `devices_review_approve`. An
  override table fixes awkward names. A test asserts names are unique,
  `[a-z0-9_]`, and at most 64 characters.
- **Input schema:** path, query and body parameters, taken from the route's
  OpenAPI operation, so validation is exactly the route's own.
- **Description:** today almost no route has a summary (one `Field(description=…)`
  in the whole codebase), and an AI cannot use a tool it cannot read. A
  description table, `app/mcp/descriptions.py`, keyed by `"METHOD /path"`,
  gives every exposed tool one or two sentences. A test fails if an exposed
  route has none. Module code is not edited for this.
- **Annotations** (MCP tool annotations):
  - `readOnlyHint` on every `GET`;
  - `destructiveHint` on every `DELETE` and on a named set: push install and
    runs, update rollouts, rejecting a device (which uninstalls its agent),
    remote sessions, patch apply, script run, backup restore, app deploy;
  - `idempotentHint` on `PUT`.

  These are hints for the client's defaults; the gate is the person's
  per-tool setting.

### 4.2 Only what the person's role allows

- `tools/list` returns only tools whose route permission the person holds.
  The route's permission is read with `rbac.route_permission(route)`, which
  walks the route's `require_permission` dependency.
- A route with no permission dependency is excluded unless it is listed as
  intentionally open to any signed-in user, such as `GET /me`.
- **Areas:** `/mcp?areas=devices,patch` lists only those areas, by first path
  segment. About 545 actions is a lot for one AI app; a person can add
  narrower connectors instead of one huge one.

### 4.3 How a tool runs

The pattern of the in-app AI (`core/ai/executor_staff.invoke_tool`):

1. Validate the arguments against the tool's input schema.
2. Build the URL only from the route's path template; path values are
   quoted, everything else becomes query or body.
3. Call the route **in-process** through the full ASGI stack
   (`httpx.ASGITransport`), with the caller's own bearer token.
4. The API recognises an in-process MCP call by an **ASGI scope extension**,
   not a header: a network request cannot set a scope key. `deps.auth_context`
   then validates the token against the MCP audience instead of `it-suite`,
   and marks the request's channel as `mcp`.
5. Authentication, tenancy (RLS), RBAC, validation and audit all run as the
   person, exactly as for the console. Nothing is reimplemented.
6. The JSON result passes the secret scrub (§5.3). Status codes map to MCP
   errors: 401 → re-authenticate, 403 → names the missing permission,
   404, 409, 422 with the detail, 429 (§7).
7. Timeout: 30 s per call.

## 5. Never exposed

Invariant 3 is **not** amended. These are refused twice: no tool is ever
generated for them, and the API itself refuses them when the request's channel
is `mcp`, so a future change to tool generation cannot expose them by
accident.

### 5.1 Plaintext secrets

- `POST /credentials/{id}/checkout`
- `GET /licenses/{id}/key`
- `POST /enrollment-tokens`
- `PUT /install-link`, `POST /install-link/regenerate` (they return the link
  code)
- `POST /agent/releases/{id}/install-command`,
  `POST /agent/releases/{id}/deployment-token` (they mint tokens)
- `POST /backup/recovery-keys/{id}/challenge`, `/prove`
- `POST /setup/claim`

The excluded permission codes `credentials.*` (checkout) and
`licenses.keys.read` are excluded by permission as well as by route, as the
in-app AI's registry already does (`_FORBIDDEN_GATES`).

### 5.2 Files and streams

The AI gets an item's details, not its bytes:

- `POST /install-link/msi`
- attachments, recordings and document content
- every CSV and XLSX export
- the in-app AI's SSE stream and chat routes (an AI calling the platform's
  own AI is circular)

### 5.3 The scrub

Every result passes `looks_like_secret_key` (`core/secrets.py`) at every
depth. A secret-looking key blocks the **whole** result rather than redacting
it, as the in-app AI does. Many legitimate fields contain such words
(`token_expires_at`, `has_token`). So a static test walks every exposed
route's response model, and any field name that matches must be either on a
reviewed allowlist (`(route, field_path)`, with a reason) or the route is
excluded. The scrub never surprises anyone at runtime.

### 5.4 Every route is decided

A test partitions every `/api/v1` route into **tool** or **excluded, with a
reason**. A new route that is in neither fails the build, as
`approved_actions.NON_APPROVABLE_REASONS` does for job types. Becoming
AI-reachable is therefore always a reviewed decision. This replaces, for MCP
only, the in-app AI's §0.2 rule that every tool is hand-listed (Q1). The
in-app AI's own registry is unchanged.

## 6. Who may connect

- **New permission `mcp.use`**, seeded to org-admin by migration
  (`tests/test_rbac.py:283` requires org-admin to hold every non-operator
  code). Other roles get it through role editing.
- **Organization switch** `mcp_enabled` in `Organization.settings`, added to
  `SETTINGS_ALLOWLIST`. That allowlist forbids security-control keys unless
  enforcement is wired and tested; this one is, by §10. Off by default.
- Both are checked on **every** request, not only at connection time, so
  turning the switch off or removing the permission stops the next call.

## 7. Audit and limits

- **Every tool call** writes `mcp.tool_called` as the person
  (`actor_type="user"`; the platform has no AI actor type, by design,
  `core/ai/act.py:1069`). Its metadata holds the tool, the registered app
  (`azp`), the outcome status and a SHA-256 of the arguments. The arguments
  themselves are not stored: they can carry anything a person typed.
- **Every audit event written during an MCP call** carries
  `via: "mcp"` and the app in its metadata, added automatically by
  `audit_service.record` from the request's channel. An approval, a push run
  or a device rejection done by an AI is therefore distinguishable from the
  same act done in the console.
- Connecting, consent and revocation are audited too: `mcp.app_revoked`, and
  `mcp.settings_changed` for the switch.
- **Rate limit:** 120 requests per minute per person, using the Redis counter
  pattern of `ai_admin/api.py` (`_bump`). Every request that passes the gate
  counts, once, at the gate (final review, 2026-10-15): a ping or an
  initialize has cost the gate's reads as a call has. An excess `tools/call`
  gets 429, which becomes an MCP error saying when to retry (the first per
  window is audited); an excess `tools/list` gets a JSON-RPC error; protocol
  housekeeping over the limit is answered, since it reads and changes nothing.

## 8. Console: Settings → AI connections

- The switch, the address, and a short **Connect Claude** guide.
- The table of AI apps with access in this organization: app name (from its
  registration), people using it, last used (from `mcp.tool_called`),
  **Revoke for me**, and for org-admins **Revoke for everyone**.
- A person without `mcp.use` sees the tab read-only, with the reason.

## 9. Invariant 1c (for CLAUDE.md, after 2a)

Approved in principle on 2026-10-09, with the restricted-acts switch removed
(Q5). The final wording below is what the operator approves with this spec:

> **1c. An AI connected over the platform's MCP server acts as the person who connected it, and may reach what that person may reach — including acts that 1b(f), 2a(e) and the self-update, uninstall and screen-share specs reserve to a person.** *(Approved by the operator 2026-10-09. Invariants 1, 2 and 3 are unchanged.)* All of the following are required:
>
> a. **The session is the person, and only OAuth makes one.** A token issued by the platform's identity provider to that person after their own sign-in, carrying the MCP audience and no other platform audience. RBAC, tenancy and audit are identical to the console's.
>
> b. **Every MCP call is audited** with the channel `mcp`, the registered AI client and the tool.
>
> c. **No plaintext secret reaches an MCP response.** Credential checkout, license keys, enrollment tokens, install-link codes and setup codes are never tools, the API refuses them on the MCP channel, and the scrub blocks rather than redacts.
>
> d. **Off until enabled.** MCP access is off for an organization until an org-admin enables it. A person needs `mcp.use`, and every registered AI client can be revoked, with effect on its next call.
>
> e. **The API's own bounds still hold.** Named addresses and batch ceilings (2a(f)), no group target for lock or erase (1b(f)), the push lockout guard.
>
> **What 1c deliberately does not claim.** The platform cannot tell whether a person or the AI app approved a call: approval happens in the AI client, which the operator chose on 2026-10-09. A compromised AI client, or text in a ticket that the client acts on, holds the person's full authority. That is bounded by their RBAC, their per-tool settings in the client, the audit trail and revocation — not eliminated.

The same PR adds a one-line pointer to 1c in each spec that reserves an act
to a person:

- `docs/modules/agent-distribution/spec-self-update.md` §8.5;
- `docs/modules/agent-distribution/spec-push-install.md`;
- `docs/superpowers/specs/2026-09-28-install-link-and-windows-push-design.md`
  §3 (reject uninstalls);
- `docs/modules/remote-control/spec-screen-share.md` §3.10;
- `docs/modules/mdm/spec.md` (lock and erase; MDM is not built yet, so 1c
  governs it when it is). The approved-actions refusals
(`approved_actions.NON_APPROVABLE_REASONS`) are unchanged: they govern
frozen automation templates, which 1c does not touch.

## 10. Testing

- **Partition:** every `/api/v1` route is a tool or excluded with a reason
  (§5.4).
- **Never-exposed:** for each route in §5, no tool exists, and a direct
  in-process MCP-channel call is refused by the API.
- **RBAC filtering:** a technician's `tools/list` omits an org-admin-only
  tool, and calling it anyway returns 403.
- **Tokens:**
  - a console token on `/mcp` gets 401;
  - an MCP token on `/api` gets 401;
  - a token with a revoked `azp` gets 401 from the next request;
  - a token without the `org` claim gets 401.
- **Switch and permission:** off → every call refused; `mcp.use` removed →
  refused on the next call.
- **Scrub:** a planted secret-looking field blocks the whole result; the
  response-model walk passes only reviewed fields.
- **Audit:** a tool call writes `mcp.tool_called`, and a write tool's own
  audit event carries `via: "mcp"`.
- **End to end:** the SDK's own client lists and calls tools against the
  test app.
- **Keycloak, in CI:** the deploy job's Keycloak 26.2 step boots the realm,
  runs `keycloak-init`, registers a client anonymously, completes a PKCE
  code flow for a test user with scope `mcp`, and asserts the token's
  audience is the MCP resource only. It also asserts that a registration
  asking for the `it-suite` audience scope is refused.

## 11. Deploy

- **Migration:** `mcp.use` permission, and the `mcp_revocation` table (with
  RLS, like every tenant table).
- **Image rebuild:** yes (the `mcp` SDK).
- **No new `.env` keys:** the resource address comes from
  `ITS_PUBLIC_CONSOLE_URL`.
- **`keycloak-init`** converges the `mcp` client scope, the registration
  policies and the client-policy executors on every deploy.
- **Traefik:** the `api` router rule change ships in `docker-compose.yml`.

## 12. Out of scope

- MCP **resources** and **prompts**: tools only, for now.
- Approvals enforced by the platform rather than the client (offered and
  declined on 2026-10-09).
- A local (stdio) MCP server.
