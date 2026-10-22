# Microsoft OAuth for mail — design

**Status:** ready for planning
**Date:** 2026-09-11
**Plan:** `docs/superpowers/plans/2026-09-11-mail-oauth.md`
**Research it stands on:** `docs/superpowers/specs/2026-09-10-microsoft-oauth-findings.md`
(read that first; nothing verified there is re-derived here)

**Operator decisions taken as given, not reopened:** both mail paths are in
scope; the Keycloak upgrade is part of this work rather than a prerequisite to
defer; incoming uses **delegated** sign-in, not an app-only credential; and the
sequencing is **A+B first** (Keycloak upgrade + outgoing OAuth), **then C**
(delegated IMAP OAuth) as a separate cycle.

---

## 0. What this is, and the one thing the findings doc got slightly wrong

The console has no way to connect a Microsoft mailbox at all today. Settings →
Email offers host/port/username/password and nothing else
(`frontend/src/modules/settings/EmailTab.tsx`), and the intake poller
authenticates with `LOGIN <username> "<password>"`
(`backend/app/modules/email_intake/imap.py:601`). Microsoft has disabled basic
auth for IMAP in Exchange Online, so the second of those cannot work against a
Microsoft 365 mailbox at all.

### 0.1 There are **three** mail paths in this tree, not two

The findings doc's table names two. Reading the code turns up a third, and it
matters because it is the only per-organization outgoing path that exists:

| | what sends | where the credential lives | in scope here |
| --- | --- | --- | --- |
| **Outgoing — realm** | **Keycloak**, from `smtpServer` | the realm; ITMS never stores it (`user_admin/realm_config.py`) | **yes (B)** |
| **Incoming — intake** | ITMS worker, `email_intake/poller.py` → `imap.py` | vault, via `email_mailbox.credential_id` | **yes (C)** |
| **Outgoing — notifications** | ITMS, `notification/delivery.py:768` `smtplib.SMTP` | vault, via `notification_channels.credential_id` | **no — named in §8** |

The third path sends ticket and health notifications, is configured per
organization, and holds its own vault credential. It is **deliberately out of
scope** for this cycle, but it is the answer to "each organization wants its own
outgoing mailbox" (§3.4), so it is named rather than omitted.

### 0.2 What this design does not do

It does not make ITMS an outgoing mail sender for invitations and password
resets. Keycloak sends those, and moving that would be a much larger change than
"enable OAuth" (§3.1).

---

## 1. Ground truth measured in this tree

Everything below is a file read on 2026-09-11, not recollection.

1. `deploy/docker-compose.yml:491` pins `quay.io/keycloak/keycloak:26.0`.
2. `user_admin/realm_config.py`'s `SMTP_FIELDS` is an eleven-key allowlist
   (`host, port, from, fromDisplayName, replyTo, envelopeFrom, ssl, starttls,
   auth, user, password`) and **a key not in it cannot reach the realm**.
3. `set_smtp` reads the whole realm representation, replaces one key, and PUTs
   it back — because `PUT /admin/realms/{realm}` is a replace and a partial body
   silently drops the ITMS login theme and the WebAuthn passwordless policy.
4. `PUT /api/v1/settings/email` is gated `orgs.manage`, writes one
   `email.settings_updated` audit event recording **field names only**, and the
   realm write happens inside the audit transaction so a refused write takes the
   audit row with it (`app/api/v1/realm_settings.py`).
5. `email_mailbox.credential_id` is a **NOT NULL** FK to `credentials`
   (`email_intake/models.py`), and `MailboxCreate.password` is
   `min_length=1` — required (`email_intake/schema.py`).
6. The vault is envelope encryption: AES-256-GCM under a per-org DEK, DEK
   wrapped by OpenBao transit; `_aad(org_id, credential_id)` binds every
   ciphertext to its org **and** its credential row, so a ciphertext moved
   between orgs fails to decrypt (`app/core/vault.py`).
7. `poller._checkout_credential` decrypts at point of use, writes an
   `email_intake.credential_used` audit event in the same transaction, and holds
   the plaintext on a `__slots__` object that `poll_mailbox` clears immediately
   after the connection is built.
8. `app/core/egress.py` guards **webhook delivery only**. It does not guard
   `httpx` calls made by `realm_config.py`, and it will not guard a token
   exchange. Anything this design dials outbound is therefore unguarded by
   construction, which is why §4.6 pins the token host in code.
9. There is **no** public-console-URL setting. `backend/app/core/config.py` has
   `s3_public_endpoint` and `gateway_url`; nothing names the browser-facing
   origin of the console.
10. `deploy/scripts/backup.sh` dumps **both** `itsuite` **and** `keycloak`, and
    `restore.sh` restores both. This is what makes §6.2's rollback real.
11. Traefik routes `PathPrefix("/api") || PathPrefix("/enroll/")` to `api` and
    everything else to `web` (`deploy/docker-compose.yml:100,344`). The console
    is a `BrowserRouter` SPA (`frontend/src/App.tsx:420`) carrying a bearer
    token (`frontend/src/lib/api.ts`). §4.3 depends on both facts.

---

## 2. Part A — Keycloak 26.0 → 26.2+

### 2.1 What it actually requires of the operator

- **An image pin change**, `deploy/docker-compose.yml`:
  `quay.io/keycloak/keycloak:26.0` → `quay.io/keycloak/keycloak:26.2` (exact
  patch tag chosen in §8 Q4). Nothing else in the `keycloak` service changes:
  the realm import bind mount, the theme bind mount,
  `KC_SPI_THEME_STATIC_MAX_AGE: -1` and `KC_SPI_THEME_CACHE_THEMES: "false"` all
  stay exactly as they are.
- **A backup taken first**, `deploy/scripts/backup.sh`.
- Nothing else. `keycloak-init` re-runs on every `up` and converges its
  allowlist; no manual console step is added.

### 2.2 The irreversible step, named

**Keycloak migrates its own database schema on first start of the new version,
and it does not migrate back.** That schema lives in the `keycloak` database in
the shared Postgres (`KC_DB_URL: jdbc:postgresql://postgres:5432/keycloak`). The
moment the 26.2 container starts, the 26.0 binary can no longer read that
database.

So: **re-pinning the image to `26.0` is not a rollback.** It produces a 26.0
Keycloak pointed at a 26.2 schema, which refuses to start. This is precisely the
case `docs/superpowers/specs/2026-08-15-deploy-rollback-design.md` D1 anticipates
— rollback is restore-from-backup — and it is the reason §6.2's sequence takes
the backup *before* `docker compose up -d` and not after.

### 2.3 The three upgrade risks the findings doc flagged, and what to do about each

| risk | what to check | where |
| --- | --- | --- |
| realm import compatibility | Import only runs against an empty database, so an existing install is **not** re-imported on upgrade. The risk is only for a *fresh* install on 26.2. | assert a from-scratch compose boot on 26.2 still yields `loginTheme: itms` — `deploy` CI job |
| the ITMS login theme | Theme template APIs are a Keycloak SPI and can move between minors. The theme is a bind mount (`deploy/keycloak/themes/itms/`), so a break is visible as an unstyled sign-in page. | boot 26.2 and fetch the sign-in page; assert it carries the ITMS theme's own marker, not Keycloak's default |
| the admin REST contract | `realm_config.py` depends on `GET/PUT /admin/realms/{realm}`, `POST /admin/realms/{realm}/testSMTPConnection`, and `GET /admin/realms/{realm}/users/{id}` | the existing `keycloak-init` job exercises the admin API on every `up`; §6.1 item 10 adds the SMTP round trip to the `deploy` CI job |

---

## 3. Part B — outgoing OAuth (Settings → Email)

### 3.1 Decision B1: Keycloak's own XOAUTH2, not a token this platform obtains

**Chosen: Keycloak's own SMTP XOAUTH2 with the client-credentials grant (26.2+).**

The alternative — this platform obtains the token — is not merely worse, it does
not connect to anything. **Keycloak opens the SMTP connection, not ITMS.** A
token ITMS obtained would have to be handed to Keycloak, and the realm's
`smtpServer` block has no field that accepts a bearer token; it accepts a *token
URL and a client credential* and does the exchange itself. The only way ITMS
could own the token is for ITMS to take over sending invitations and
password-reset mail — a different project, and one that would put a mail sender
on the critical path of sign-in.

**The trade-off, stated plainly, because it is real:**

- The Azure client secret for the outgoing app **lives in the realm, not in this
  platform's vault**. That is not a new exception: it is exactly what the SMTP
  password does today, and `realm_config.py`'s docstring already accounts for it
  as "the ONE stated exception to 'no password ever enters the ITMS process'" —
  write-through, never stored, never logged, never returned. Invariant 3 governs
  *stored* credentials; this one is not stored by us.
- This platform therefore **cannot rotate, expire-check, or audit the use of**
  that secret. It can audit that an operator changed it
  (`email.settings_updated`, field names only), and nothing more.
- Keycloak's client-credentials grant is **app-only**. It sends as a mailbox the
  Azure app has been granted rights over. Microsoft requires a tenant admin to
  grant that, and — as the findings doc records for the IMAP equivalent —
  app-only permissions are tenant-wide unless scoped with
  `New-ApplicationAccessPolicy`. §8 Q1 carries the part of this that is
  unverified.

### 3.2 What changes on this platform

Small, and all of it inside the existing shape:

1. `SMTP_FIELDS` gains five keys, in the realm's own spelling:
   `authType`, `authTokenUrl`, `authTokenScope`, `authTokenClientId`,
   `authTokenClientSecret`.
2. `EmailSettingsIn` gains `auth_type: Literal["password", "token"] = "password"`
   plus `token_url`, `token_scope`, `token_client_id`, and `token_client_secret`.
3. `EmailSettingsOut` gains `auth_type` and `token_client_id` — **never** the
   secret. `authenticated` keeps its current meaning.
4. `_as_realm_block` learns a third branch: no username → clear everything;
   username + `auth_type=password` → today's behaviour, byte for byte;
   `auth_type=token` → write the four token keys and **clear `password`**.
5. The Email tab grows a two-choice control, "Password" / "Microsoft (OAuth)",
   rendered with the same `Chips` component the encryption control already uses, and
   swaps the credential fields beneath it.

### 3.3 Decision B2: `token_client_secret` follows the password's rule exactly

Optional on the way in; absent means "keep what is stored"; `set_smtp` sends the
realm's own mask back unchanged. Never returned by `EmailSettingsOut` — there is
no field that could carry it. `_refuse_unusable` gains the mirror of its
existing rule: **a token client secret with no client id is refused**, not
quietly dropped, for the same reason a password with no username is.

And the same trap in the other direction: switching `auth_type` from `token` to
`password` must **clear** the four token keys, exactly as switching StartTLS to
SSL removes `starttls`. A realm carrying both a password and a token client
leaves the relay to decide what was meant.

### 3.4 What this means for a multi-tenant deployment

**There is one realm on this deployment, so Settings → Email is platform-wide.**
That is already true and already documented in `realm_settings.py`'s docstring
("an `orgs.manage` holder in one organization changes the relay that every
organization's invitations go through"). OAuth does not change it — and it does
not make it worse, because the field being changed was always realm state.

What OAuth *does* change is how expensive the honest fix becomes. Per-org
outgoing mail on this path would need a realm per organization: a separate Azure
app registration, a separate client secret, and a separate `smtpServer` block
each. That is a different product shape, not a setting.

**The realistic per-org answer is the third path from §0.1.** Notification email
channels are already per-organization, already ITMS-owned, and already hold
their own vault credential. If an organization needs to send from its own
mailbox, that is where it belongs. Invitations and password resets come from the
platform's own identity provider and sending those from one platform address is
defensible; sending an organization's ticket notifications from that same
address is not. This is named, and it is **not** built in this cycle (§8 Q3).

---

## 4. Part C — delegated IMAP OAuth (email intake)

Ships as its own cycle after A+B.

### 4.1 Decision C1: the refresh token is a vault credential of a new type

`email_mailbox.credential_id` already points at a `credentials` row whose
`ciphertext` is envelope-encrypted under the org DEK with
`aad = "{org_id}|{credential_id}"`. The refresh token goes there, with
`credentials.type = 'email_mailbox_oauth'` instead of `'email_mailbox'`.

**Nothing new is invented.** `mailboxes._store_secret` already rotates a secret
in place on an existing credential row, and `poller._checkout_credential`
already decrypts at point of use and writes the audit event. Both are reused
unchanged.

**Where it lives:** `credentials.ciphertext`, AES-256-GCM, DEK wrapped by
OpenBao transit. Never in `email_mailbox`, never in a response model
(`MailboxOut` has no field that could carry it), never in `last_error`
(`_sanitize_excerpt` is the only writer), never in audit metadata.

**Who can decrypt it:** the API and worker processes, and only while OpenBao is
unsealed. A database dump on its own decrypts nothing — the DEK is wrapped by a
key that never leaves OpenBao. No console user can read it: there is no route
that returns it and `credentials.checkout` does not cover it, because the intake
poller checks it out as `actor_type="system"` on its own schedule rather than on
a user's request.

**The access token is never stored.** It is fetched per poll, held on the same
`__slots__` plan object the password uses today, and dropped immediately after
the IMAP connection is authenticated.

### 4.2 Decision C2: refresh-token rotation is persisted before the token is used

Microsoft's token endpoint may return a **new** `refresh_token` alongside the
access token. If the response carries one, it is written to the vault **before**
the access token is used to open an IMAP connection. The ordering is the whole
point: a crash between "redeemed" and "stored" with the opposite ordering leaves
a mailbox holding a refresh token the provider has already retired, and the
symptom is a mailbox that silently stops polling weeks later.

### 4.3 Decision C3 — the most consequential one: the callback lands on the **console**, not on the API

Microsoft redirects the browser to

```
https://<console-origin>/settings/email-intake/oauth/callback?code=…&state=…
```

which Traefik routes to `web` (everything that is not `/api` or `/enroll/`), so
it is an **SPA route**. The SPA reads `code` and `state` from the query, calls
`history.replaceState` to strip them out of the address bar, and then makes an
ordinary authenticated call with the operator's own bearer token:

```
POST /api/v1/email-intake/oauth/complete   { "state": "...", "code": "..." }
```

**Why this and not an API callback route.** An API route that Microsoft redirects
to arrives with no `Authorization` header. Authorising it would mean inventing a
second authentication scheme whose only credential is the `state` parameter —
and then `state` would be simultaneously the CSRF defence, the tenant binding
and the authentication. The brief is right that `state` must not be the only
thing binding a callback to a tenant; the clean way to satisfy that is not to
add a second check beside `state`, it is to **make the privileged call an
ordinary authenticated call** and leave `state` doing only the job it is
designed for.

What that buys, against each invariant:

- **Invariant 6 (RBAC server-side).** `/oauth/complete` is
  `Depends(require_permission("email_intake.admin"))` like every other write on
  that router. No new gate, no new scheme.
- **Invariant 5 (tenancy at the database layer).** The org is taken from the
  caller's token and the work runs inside `org_session(ctx.org_id)`, so RLS
  constrains it. The pending-authorization row is fetched **inside** that
  session; a row belonging to another organization is not visible, so a stolen
  `state` replayed by a user of a different organization finds nothing. `state`
  is not what binds the callback to the tenant — the caller's own token and RLS
  are, and `state` is a third, independent check.
- **CSRF.** `state` is 256 bits of `secrets.token_urlsafe`, single-use, 10-minute
  TTL, and bound to `(org_id, user_id, mailbox_id)` at mint time. A completion
  whose `state` row names a different `user_id` than the caller is refused.
  Because completion is a POST carrying a bearer token from the SPA's own
  origin, there is no ambient-authority request an attacker can forge.
- **Replay.** The row is consumed with `UPDATE … SET consumed_at = now() WHERE
  consumed_at IS NULL RETURNING *` inside the completing transaction. A second
  POST with the same `state` returns zero rows and is refused 409. Microsoft's
  authorization code is single-use at their end too; this does not rely on that.

### 4.4 The Connect flow, click by click

**Prerequisite (one-time, per organization):** Settings → Email Intake →
*Microsoft connection* holds the Azure app registration — directory (tenant) id,
application (client) id, client secret. The secret goes to the vault as a
`credentials` row of type `email_oauth_client` (this one *is* stored by us, so
invariant 3 applies in full). Each on-prem install registers its own Azure app,
because each has its own hostname and this platform ships no client secret
(findings doc).

1. **The operator creates the mailbox** with *Authentication: Microsoft* —
   address, folder, default category, no password field. It is saved with
   `auth_kind='microsoft_oauth'`, `credential_id = NULL`, `active = false`. The
   list shows it as **Not connected**.
2. **They press "Connect with Microsoft."** The console calls
   `POST /api/v1/email-intake/mailboxes/{id}/oauth/start` (`email_intake.admin`).
   The server, inside `org_session`:
   - generates `code_verifier` (43–128 chars), derives
     `code_challenge = BASE64URL(SHA256(verifier))`;
   - generates `state`;
   - writes one `email_oauth_authorization` row: `org_id`, `mailbox_id`,
     `user_id`, `state`, **sealed** verifier (`vault.seal_bytes` — the verifier
     is not a credential in invariant 3's sense, but sealing it costs one call
     and removes the argument), `expires_at = now + 10 min`,
     `consumed_at = NULL`;
   - writes audit `email_intake.oauth_started`;
   - returns the authorize URL.
3. **The console navigates the browser** to that URL —
   `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize` with
   `response_type=code`, `client_id`, `redirect_uri`,
   `scope=https://outlook.office.com/IMAP.AccessAsUser.All offline_access`,
   `state`, `code_challenge`, `code_challenge_method=S256`, and
   `prompt=select_account` (so a second mailbox is not silently connected as the
   already-signed-in account).
4. **The person signs in and consents** as the mailbox's own account.
5. **Microsoft redirects** to the console route in §4.3.
6. **The SPA posts `code` + `state`** with the operator's bearer token.
7. **The server completes**, inside one `org_session` transaction:
   - consumes the authorization row (single-use UPDATE … RETURNING);
   - refuses if expired, if `user_id` ≠ caller, or if zero rows;
   - unseals the verifier, POSTs the token endpoint with `grant_type=
     authorization_code`, `code`, `redirect_uri`, `client_id`, `client_secret`
     (vault checkout), `code_verifier`;
   - on success, stores the returned `refresh_token` through
     `mailboxes._store_secret`, sets `credential_id`, `credential_state='ok'`,
     clears `last_error`, and leaves `active` for the operator to switch on;
   - writes audit `email_intake.oauth_connected`.
8. **The console shows "Connected as `<address>`"** and offers *Disconnect*.

### 4.5 Failure paths — all four the brief names, plus two it does not

| what happens | what the platform does |
| --- | --- |
| **The user cancels at Microsoft** | Microsoft redirects with `error=access_denied` and the `state`. The SPA posts nothing; it calls `POST /oauth/abandon` with the `state`, which consumes the row and writes `email_intake.oauth_abandoned`. The mailbox stays as it was — never half-connected. An abandoned row that is never posted simply expires in 10 minutes. |
| **Consent withdrawn later** (the person revokes the app from their account) | The next refresh exchange returns HTTP 400 `error=invalid_grant`. The poller sets `credential_state='needs_reconsent'`, writes `last_error` = "Microsoft sign-in is no longer valid — reconnect this mailbox", writes audit `email_intake.oauth_revoked`, and **stops attempting**. It does not retry on a schedule: retrying a dead refresh token is noise, not resilience. |
| **The refresh token expires** (inactivity past the provider's window) | Identical to the row above — `invalid_grant` is the same answer, and the operator remedy is the same: Reconnect. The console does not try to tell the two apart, because the provider's response does not reliably tell them apart either. |
| **The tenant admin revokes the app registration** or deletes the client secret | The token exchange fails with `invalid_client` / `unauthorized_client`. This is a *configuration* fault, not a consent fault, so the mailbox goes to `credential_state='needs_reconsent'` but `last_error` names the Microsoft connection rather than the mailbox: "The Microsoft application this organisation is registered with was refused. Check Settings → Email Intake → Microsoft connection." Reconnecting the mailbox will not fix it and the message must not suggest it will. |
| **The token endpoint is unreachable** (network, DNS, proxy) | Transient. `credential_state` is untouched, `last_error` records a sanitized transport reason, the next poll retries. A network blip must not present as "your consent was revoked". |
| **XOAUTH2 is refused by the IMAP server** with a valid-looking token | Microsoft answers a failed `AUTHENTICATE XOAUTH2` with a **base64 JSON challenge**, and the client must send an empty response line to finish the exchange or the connection hangs until the deadline. `imap.py`'s `_ConversationDeadline` would eventually close it, but a hang per poll interval is a defect. The XOAUTH2 path sends the empty continuation explicitly and classifies the result as `stage="login", reason="rejected"`, exactly as `_login` does — the JSON blob is never copied into `last_error` verbatim. |

### 4.6 Decision C4: the token endpoint host is pinned in code

The operator supplies a **tenant id** (a GUID or a verified domain), not a URL.
The authorize and token endpoints are built from a module constant
`https://login.microsoftonline.com`. A free-form token URL on a per-org setting
would be a refresh-token exfiltration primitive configurable by anyone holding
`email_intake.admin`: point it at a host you control and the platform will POST
the refresh token and the client secret to you, per poll, forever. `egress.py`
does not guard this call (§1.8), so the pin is the whole control.

*(The outgoing path in §3 takes `authTokenUrl` as a free-form field, because
Keycloak's own field is free-form and the value is stored in the realm rather
than dialled by this platform. The asymmetry is deliberate and is the reason
this section exists.)*

### 4.7 Schema changes

Two new columns on `email_mailbox` and one new table, in one forward-only
migration (`0083_email_intake_oauth.py`):

```
email_mailbox
  + auth_kind        varchar(20) NOT NULL DEFAULT 'password'
                     CHECK (auth_kind IN ('password','microsoft_oauth'))
  + credential_state varchar(20) NOT NULL DEFAULT 'ok'
                     CHECK (credential_state IN ('ok','needs_reconsent'))
  ~ credential_id    uuid NULL                       -- was NOT NULL
  + CHECK (credential_id IS NOT NULL OR active IS FALSE)
```

**Relaxing `credential_id` to nullable needs justifying, because the module spec
says NOT NULL on purpose.** `docs/modules/email-intake/spec.md` §2.1 states it
because "there is no unauthenticated IMAP". That property is preserved, and in a
stronger form: the new `CHECK (credential_id IS NOT NULL OR active IS FALSE)`
says *a mailbox with no credential can never be polled*, and `poller._claim`
already filters `active.is_(True)`. The nullable window exists only between "the
operator created an OAuth mailbox" and "someone consented", which is a real state
the old schema could not represent — the alternative was to invent a
pending-mailbox side table holding a copy of every mailbox field. **This spec
amends `docs/modules/email-intake/spec.md` §2.1 as amendment A5.**

```
email_oauth_app                     -- one per org: the Azure registration
  org_id uuid PK FK organizations
  tenant_id     varchar(200) NOT NULL
  client_id     varchar(200) NOT NULL
  credential_id uuid NOT NULL FK credentials   -- the client secret, vaulted
  created_by, created_at, updated_at

email_oauth_authorization           -- one per in-flight consent, single use
  id uuid7 PK
  org_id uuid FK organizations
  mailbox_id uuid FK email_mailbox
  user_id uuid FK users
  state varchar(64) NOT NULL
  sealed_verifier text NOT NULL     -- vault.seal_bytes
  expires_at timestamptz NOT NULL
  consumed_at timestamptz NULL
  created_at timestamptz NOT NULL
  UNIQUE (org_id, state)
```

Both tables are RLS-protected like every other tenant table; the `deploy` CI job
already asserts that every tenant table is (`.github/workflows/ci.yml`, "assert
every tenant table is RLS-protected"), so a missing policy fails CI rather than
review.

### 4.8 Audit events (invariant 4)

Six, all through `audit_service.record` inside the same transaction as the work,
all carrying **identifiers and never token material**:

| action | actor | when |
| --- | --- | --- |
| `email_intake.oauth_app_configured` | user | the Azure registration is saved or its secret rotated; metadata = field names only |
| `email_intake.oauth_started` | user | `/oauth/start` mints an authorization |
| `email_intake.oauth_connected` | user | a refresh token is stored |
| `email_intake.oauth_abandoned` | user | the operator cancelled at Microsoft |
| `email_intake.oauth_disconnected` | user | the credential is destroyed |
| `email_intake.oauth_revoked` | system | a refresh exchange returned `invalid_grant` |

The existing `email_intake.credential_used` is unchanged and still fires on every
checkout, so "which process decrypted this mailbox's refresh token, and when"
stays answerable.

---

## 5. Compatibility — what must not regress

### 5.1 A deployment that never touches Microsoft

This is most installs, and it is the load-bearing requirement.

- **Outgoing.** `auth_type` defaults to `"password"`. In that branch
  `_as_realm_block` produces **exactly** the dict it produces today — the four
  token keys are absent, and an absent key in `SMTP_FIELDS` cannot reach the
  realm. The test for this is a byte comparison, not an inspection (§6).
- **Incoming.** `auth_kind` defaults to `'password'`, `credential_state` defaults
  to `'ok'`, and every existing row gets those defaults in the migration.
  `imap.py`'s `_login` is untouched; the XOAUTH2 path is a **new function beside
  it**, selected by `auth_kind`, not a rewrite of it.
- **Config.** The new `ITS_PUBLIC_CONSOLE_URL` (§5.2) is optional. Absent, the
  OAuth routes answer 503 naming the missing setting — the same three-way
  contract `KeycloakAdminConfig.from_settings` already uses (configured /
  not configured / configured-but-broken). Nothing else changes behaviour.

### 5.2 Decision C5: the redirect URI comes from config, never from the request

A new setting, `ITS_PUBLIC_CONSOLE_URL` (e.g. `https://itms.toeshee.com`). The
redirect URI is `{ITS_PUBLIC_CONSOLE_URL}/settings/email-intake/oauth/callback`,
built server-side and sent to Microsoft on both legs.

**Not from the `Host` header, and not from `X-Forwarded-Host`.** Those are
attacker-controllable; deriving a redirect URI from them is the classic
host-header injection that turns an authorization code into someone else's. The
value is also registered in the Azure app, so a mismatch is refused by Microsoft
before anything happens — but this platform must not be relying on the provider
to catch its own mistake.

### 5.3 Going back from OAuth to a password

**Outgoing:** switch the Email tab's control back to Password and enter a
password. The save clears the four token keys. Recoverable, in one form
submission, by design.

**Incoming:** *Disconnect* destroys the vault credential (via
`mailboxes._delete_secret`, which the delete path already uses) and sets
`credential_id = NULL`, `active = false`. Changing `auth_kind` back to
`'password'` then requires a password, exactly as creating a password mailbox
does. **A disconnect does not silently fall back to a stored password** — there
is none, and a mailbox that appeared to keep working after a disconnect would be
worse than one that stopped.

### 5.4 What happens to an existing password relay when OAuth is connected

Nothing is migrated and nothing is guessed. The operator switches the control;
the save writes `authType=token` and **clears** `password` in the same PUT.
Until they switch it, the relay keeps using the password. The console states the
consequence before the save, on the button's own confirmation: *"Saving will
replace the stored relay password with a Microsoft sign-in. The password is not
kept."*

---

## 6. What is testable without a Microsoft tenant

"Mock the provider" is not a strategy, so here is what each layer actually
proves, and — where it proves nothing — that is said.

### 6.1 Real tests with real assertions

1. **The bytes on the IMAP wire.** `backend/tests/test_email_intake_imap.py`
   already stands up a socket server and inspects "what a passive listener would
   have seen". The XOAUTH2 path is tested the same way: assert the client sends
   `AUTHENTICATE XOAUTH2 <b64>`, that the base64 decodes to exactly
   `user=<address>\x01auth=Bearer <token>\x01\x01`, and — the assertion that
   matters — that **the refresh token appears nowhere in the captured stream**.
   This is not a mock; it is the protocol.
2. **The failure continuation.** Same harness: a server that answers the
   `AUTHENTICATE` with a base64 JSON challenge and then `NO`. Assert the client
   sends the empty continuation line and raises `ImapError(stage="login",
   reason="rejected")` **within the deadline**, rather than hanging. This is the
   §4.5 defect, and it is reachable without Microsoft.
3. **The token exchange, as a request.** `httpx.MockTransport`, following
   `backend/tests/test_realm_settings_api.py`'s `FakeRealm` pattern. It proves
   what this platform **sends**: the grant type, that `code_verifier` is present
   and is the pre-image of the challenge sent on the authorize leg, that the
   client secret came from the vault, that the URL host is
   `login.microsoftonline.com` and cannot be moved by configuration. It proves
   nothing about what Microsoft would answer — see §6.2.
4. **Rotation ordering (§4.2).** Mock transport returns a *new* `refresh_token`;
   assert the vault write happened **before** the IMAP client factory was
   called, by having the fake factory read the credential row and assert it
   already holds the new value.
5. **`invalid_grant` → `needs_reconsent`.** Mock transport returns 400
   `invalid_grant`; assert `credential_state`, `last_error`, the audit event, and
   that the **next** poll does not attempt another exchange.
6. **State single-use and cross-org replay.** Two orgs, two sessions. Assert: a
   second completion with the same `state` is 409; a completion posted by a user
   of another organization finds no row (RLS) and is refused; a completion posted
   by a *different user of the same organization* is refused on the `user_id`
   check; an expired row is refused.
7. **Nothing leaks.** Scan the **raw response bytes** of every new route for the
   literal refresh token and the literal client secret — the form
   `test_realm_settings_api.py` already uses, chosen because a renamed field
   satisfies a weaker "key is absent" assertion while still shipping the value.
   Same scan over `last_error` and over every audit `metadata` dict written.
8. **The password path is byte-identical.** Call `_as_realm_block` with a
   password-mode body and compare the dict to a frozen literal of today's
   output. If a key appears, the test fails. This is the §5.1 guarantee.
9. **Migration round-trip.** The existing `migrations` CI job runs
   `upgrade head → downgrade base → upgrade head`, so the new migration's
   `downgrade()` is exercised on every push whether or not anyone thought about
   it.
10. **The Keycloak upgrade.** The existing `deploy` CI job boots the compose
    stack. Extend it: boot 26.2 from scratch, assert the realm has
    `loginTheme: itms`, assert `GET /admin/realms/it-suite` still answers the
    shape `realm_config.py` parses, and assert a `PUT` with `authType=token`
    round-trips the four token fields back on the next `GET`. **That last one is
    the single most valuable test in this list**, because it is the only thing
    that proves 26.2 actually accepts the fields the findings doc read about in
    documentation.

### 6.2 What cannot be tested here, stated rather than papered over

No test in this repository can show that **Microsoft accepts** what this platform
sends: that the scope string is right, that the Azure app permission is the right
one (§8 Q1), that a shared mailbox can be reached delegated (§8 Q7), or that the
tenant's Conditional Access policies permit the flow. `MockTransport` answers
what the tests say.

That gap is closed by a **written acceptance checklist an operator runs once,
against the real tenant**, recorded in `deploy/README.md` alongside the existing
"UNVERIFIED as a sequence" convention this tree already uses:

1. Save the Email tab in Microsoft mode; press *Send test message*; confirm the
   message arrives.
2. Connect an intake mailbox; confirm the poll status goes green and a test
   message becomes a ticket.
3. Revoke the app from the mailbox account's *My Account → Apps* page; confirm
   the console shows **Reconnect** within one poll interval and does not retry.
4. Reconnect; confirm polling resumes.

Until an operator has run those four, this feature is **shipped but unverified
against a live tenant**, and it is described that way — the same standard
`realm_config.py` holds itself to.

---

## 7. Deploy and rollback

### 7.1 The upgrade (A+B)

The full sequence is in the plan's final task, verbatim and copy-pasteable. The
two things that make it different from an ordinary deploy:

- **`deploy/scripts/backup.sh` runs first, and is not optional**, because §2.2's
  Keycloak schema migration is one-way.
- `docker compose pull keycloak` is added, because the pin moved and
  `up -d` alone will use the image already on the host if the tag were reused.
  The tag is not reused here, but the pull is stated so the sequence is
  self-contained.

### 7.2 Rolling back

- **Application code only** (a bad Email tab, a bad route):
  `IT_SUITE_VERSION=<old sha> docker compose up -d`, per
  `docs/superpowers/specs/2026-08-15-deploy-rollback-design.md`. The Keycloak
  upgrade is untouched, and 26.2 serves a 26.0-era realm perfectly well.
- **The Keycloak upgrade itself:** **re-pinning the image is not enough** (§2.2).
  The sequence is: `docker compose stop keycloak`, restore with
  `deploy/scripts/restore.sh <backup-dir>` (it restores `keycloak.dump` as well
  as `itsuite.dump`), revert the image pin, `docker compose up -d`. **This
  restores the application database too**, so anything written since the backup
  is lost — which is why the backup is taken immediately before the upgrade and
  not the night before.
- **A new migration:** forward-only. Rollback is restore-from-backup, same as
  above.

---

## 8. Open questions for the operator

These are the places where inventing an answer would be worse than asking. They
are in the order they will block work.

**Q1 — the Azure app permission for *outgoing* SMTP (blocks B's acceptance).**
The findings doc verified the *Keycloak* side: `authType`, `authTokenUrl`,
`authTokenScope`, `authTokenClientId`, `authTokenClientSecret`, client-credentials
grant, Microsoft supported. It did **not** verify the *Azure* side. Belief, not
verified: the app permission is `SMTP.SendAsApp`, the scope to request is
`https://outlook.office365.com/.default`, SMTP AUTH must be enabled on the
sending mailbox, and the app may need scoping with `New-ApplicationAccessPolicy`
so it cannot send as every mailbox in the tenant. **Please confirm against
current Microsoft documentation before B is built**, because the scope string is
a field in a form and getting it wrong is a support call, not a crash.

**Q2 — one Azure app or two?** The findings doc says the same app can serve both
halves. One registration is less to manage; two means the outgoing app-only
permission (tenant-wide unless policy-scoped) and the incoming delegated
permission are not carried by the same credential, and the outgoing secret —
which lives in the realm rather than in this platform's vault (§3.1) — does not
also unlock the incoming path. **This design recommends two**, and will build
either.

**Q3 — is one platform-wide outgoing sender acceptable?** §3.4: Settings → Email
is realm state, so one mailbox sends every organization's invitations. That is
already true today. If a tenant must send from its own address, the answer is the
notification-channel path, which is a separate cycle. **Confirm that deferring it
is fine.**

**Q4 — which 26.x tag?** `26.2` is the floor. The newest 26.x picks up fixes but
widens the change; `26.2` is the smallest step that gets the feature. **Which,
and do you want to take the jump to the newest 26.x in the same window?**

**Q5 — where does the callback come back to?** `ITS_PUBLIC_CONSOLE_URL` (§5.2)
must be the exact browser-facing origin, and the same string must be registered
as a redirect URI in the Azure app. **Please confirm the value** — presumably
`https://itms.toeshee.com`, but the `.env` does not record it anywhere today, so
this design will not guess it.

**Q6 — who can grant admin consent in the Azure tenant?** Delegated
`IMAP.AccessAsUser.All` may require tenant-admin consent depending on tenant
policy. If the operator running the console is not that person, the "Connect"
flow ends at a Microsoft consent screen they cannot clear, and the remedy is an
admin-consent URL rather than anything this platform can do. **Worth knowing
before the first attempt, not during it.**

**Q7 — is the intake mailbox a *shared* mailbox?** This is the one that most
often turns a working design into a support ticket. Delegated IMAP against a
shared mailbox is not a sign-in *as* the shared mailbox — it is a sign-in as a
licensed user who has full access, plus a proxy username form on the IMAP
`AUTHENTICATE` line (`user@domain/shared@domain`). **If the support mailbox is
shared, say so now**: it changes what the username field on the mailbox form
means, and the test in §6.1 item 1 pins that exact string.

**Q8 — Conditional Access and MFA.** If the tenant requires MFA or a compliant
device for the account that will own the intake mailbox, the interactive consent
in §4.4 will satisfy it once, and subsequent refreshes may or may not — that
depends on policy this design cannot see. **If there is a CA policy on this
account, name it**; the mitigation is usually an exclusion for the app, arranged
before the first connect rather than after the first silent failure.

---

## 9. Operator answers, 2026-09-11

The open questions in §8 were put to the operator. These are their answers and
what each settles. Where an answer narrows the design, the narrower design is
now the design — §8 stays as the record of what was uncertain and why.

**The support mailbox is a NORMAL USER MAILBOX, not a shared one.** This is the
simple case and it removes the trap §8 flagged as most likely to turn a working
design into a support ticket: there is no Full Access delegation, no licensed
proxy user, and the IMAP username is the address itself rather than the
`user@domain/shared@domain` proxy form. The wire-level test pins the plain
address. If this ever changes to a shared mailbox, the username field's meaning
changes with it and that test is the thing that will tell you.

**Two Azure app registrations, one per direction.** The outgoing client secret
has to live inside the Keycloak realm — platform-wide, and neither rotatable
nor auditable by this platform, which §4 accounts for in full. With two apps
that exposure does not also carry read access to mailbox contents. The cost is
a second registration and a second consent, which is the right trade.

**The operator can grant admin consent themselves.** The Connect flow can
therefore complete in one sitting, and the design does not need the
hand-the-consent-URL-to-someone-else path §8 asked about. Do not build it.

**SMTP AUTH is already enabled on the account that will send.** This is the
prerequisite most often missing, and it is confirmed. **It is not the whole of
question 1.** Three things had to hold and the operator has answered one:

| Prerequisite | State |
|---|---|
| SMTP AUTH enabled on the mailbox | **Confirmed by the operator** |
| App registration granted `SMTP.SendAsApp` | **Still unverified** |
| Scope `https://outlook.office365.com/.default` accepted by the token endpoint | **Still unverified** |

The two unverified rows are properties of the *app registration*, not of the
mailbox, so the operator's answer does not reach them. They are cheap to check
and the operator holds the access to check them — the steps are in the plan's
Appendix A. Cycle A (the Keycloak upgrade) does not depend on either and
proceeds regardless; cycle B's first task is to confirm them, because
discovering a missing grant after the token-acquisition path is written is the
expensive order to find out.

**Assumed, and stated here so it is corrected rather than discovered:**
`ITS_PUBLIC_CONSOLE_URL` is taken to be `https://itms.toeshee.com`, the origin
this deployment already serves the console on. The callback lands there. If the
console is ever reached on another origin — a second hostname, a staging copy —
that value is wrong and the redirect will fail closed at Microsoft's end with a
redirect-URI mismatch, which is the safe direction for this particular mistake.

---

## 10. As built, 2026-09-23

Cycles B and C were built together on one branch; the plan's
`0083_email_intake_oauth` shipped as migration **0110** (after 0100).

**§9's two unverified Q1 rows, checked against Microsoft's documentation**
(*Authenticate an IMAP, POP or SMTP connection using OAuth*, revision of
2025-10-17), not yet against the tenant: the application permission is
`SMTP.SendAsApp` under *Office 365 Exchange Online*, and the client-credentials
scope is `https://outlook.office365.com/.default`. The same page adds two steps
this design did not name, now in `deploy/README.md`: the app's service
principal must be registered in Exchange (`New-ServicePrincipal`, with the
**Enterprise application** object id) and granted the sending mailbox
(`Add-MailboxPermission`). That grant is also what scopes the app to one
mailbox: **`New-ApplicationAccessPolicy` governs Graph and EWS, not SMTP or
IMAP**, so §3.1's "may need scoping with New-ApplicationAccessPolicy" is
corrected here. Keycloak 26.2's source was read for the two assumptions
`set_smtp` rests on: it masks `authTokenClientSecret` on read and
`testSMTPConnection` substitutes the stored value for the mask, as it does for
`password`.

**Departures from the plan, each deliberate.**

- Settings → Email reads back the token address and scope (not secrets) as
  `microsoft_sign_in_url` / `microsoft_scope`, and the client id as
  `microsoft_client_id` (a `token_*` response name reads as a secret to the AI
  layer's scrubber), and
  the tab asks for a *Directory (tenant) ID* and builds the token address from
  it; the raw address and scope sit under *Advanced*. Plain-English labels per
  `docs/design/settings-language.md`: "Microsoft 365", never "OAuth".
- `/oauth/complete` spends the `state` in its own committed transaction
  **before** calling Microsoft, and only for the person who started it and only
  while unexpired, so a colleague or a replay cannot burn it and no database
  transaction is held across Microsoft's latency.
- Every decrypt is audited: the refresh token *and* the client secret each
  write `email_intake.credential_used` at point of use.
- A mailbox can be switched between password and Microsoft on edit (the old
  credential is destroyed, the mailbox switched off), and
  `POST /mailboxes/{id}/oauth/disconnect` is its own route with
  `email_intake.oauth_disconnected`.
- `invalid_client` / `unauthorized_client` set `needs_reconsent` with a message
  naming the Microsoft connection; saving that connection returns such
  mailboxes to `ok` so they are retried.
- The notification channels' own SMTP (§0.1, third path) is still password
  only. It remains deferred, as §3.4 and §8 Q3 record.

## 11. Follow-up: Microsoft Graph with an application access policy

The platform review (`docs/reviews/2026-09-23-platform-critique.md`) prefers
Microsoft Graph with **application** permissions, scoped by an Exchange
application access policy (or RBAC for Applications), over delegated IMAP. The
operator chose delegated sign-in for this cycle and this cycle follows that
decision, but the case for revisiting it is real and is recorded here:

- **No person-bound token.** The delegated refresh token belongs to the account
  that consented. Disable that account, revoke its sessions or (by tenant
  policy) change its password, and intake stops until someone reconnects. An
  app-only credential survives staff changes.
- **Scoping is an Exchange object, not a habit.** `Mail.Read` +
  `Mail.ReadWrite` (to set read state) app-only is tenant-wide until an
  application access policy or an RBAC-for-Applications management scope limits
  it to the support mailbox; with the policy it is one mailbox by construction.
- **What it would take:** a Graph reader beside `imap.py` (list unread, fetch
  MIME, mark read) behind the same `ImapClient` protocol; the client-credentials
  exchange from `tokens.py` with scope `https://graph.microsoft.com/.default`;
  no consent flow and no callback route; one more `auth_kind` value. The
  refresh-token column, `needs_reconsent` and the console's Reconnect would
  remain for the delegated path.
- A lighter middle step exists too: IMAP with **`IMAP.AccessAsApp`**
  (client credentials, scoped by `Add-MailboxPermission` to one mailbox) keeps
  the IMAP code and drops the person-bound token.

Not built. It is a change to an operator decision, so it needs that decision
reopened first.

## 12. The third path, as built 2026-09-23

Notification email channels (§0.1's third path) now offer **Sign-in:
Microsoft 365**, closing §8 Q3's per-organisation sender.

- **Client credentials, not delegated.** `SMTP.SendAsApp`, scope
  `https://outlook.office365.com/.default`, XOAUTH2 to `smtp.office365.com:587`
  with StartTLS. Host, port and TLS are pinned in code (`microsoft.SMTP_HOST`)
  and refused on save if the config says otherwise: the token can send as the
  mailbox, so it never goes anywhere but Microsoft (the §4.6 argument).
- **Reuses the intake code.** `email_intake.tokens.app_access_token` shares
  `_exchange` (pinned token host, provider text never read) with the refresh
  path; no second HTTP client.
- **Vaulted, all three.** Tenant ID, client ID and client secret are one JSON
  value on the channel's `credentials` row, written through write-only request
  fields and never returned. Each send checks it out and writes
  `notification.credential_used` (`sign_in: microsoft`), attributed to the user
  on a test send.
- **Failures:** a refused application is terminal (`sign_in_refused`), an
  unreachable token endpoint retries (`sign_in_unavailable`), and a refused
  XOAUTH2 answers Microsoft's 334 challenge with an empty line so the server
  finishes with 535 instead of holding the connection.
- **Password channels were broken from the console and are fixed:** the UI
  stored a bare password where `delivery.py` expects `{username, password}`.
  Email channels now take `smtp_username`/`smtp_password`; a bare `secret` on
  an email channel is refused on save.
