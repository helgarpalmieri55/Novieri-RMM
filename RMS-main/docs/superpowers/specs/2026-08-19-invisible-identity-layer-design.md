# The invisible identity layer — Design

**Date:** 2026-08-19
**Status:** approved direction (operator chose "A now, revisit B later"); spec pending operator review
**Amends:** `docs/modules/console-auth/spec.md` (§3.4's launcher-removal logic now extends to every identity surface), `docs/00-vision.md` commercial-readiness list

## 0. The decision, and the test it must pass

ITMS ships with two administration surfaces: its own console, and Keycloak's.
Commercializing it — cloud subscriptions *and* self-hosted, per the vision doc —
makes that a product defect: a buyer does not know what Keycloak is and must
never need to.

**The buyer test, which every section below is measured against:**

> After installation, no task in normal operation — adding a person, resetting
> a password, configuring email, branding sign-in — requires anything except
> ITMS itself. No second portal. No SSH. No mention of the word "Keycloak"
> anywhere a user can see. The backend is touched only by the vendor, for
> updates and fixes.

### Options considered

- **A — keep Keycloak, make it an invisible appliance component.** CHOSEN.
  Weeks of UI/config surface; both deployment models served by one stack;
  console-auth §1's security model (credentials never enter the ITMS process)
  survives intact.
- **B — remove Keycloak, build native auth.** Recorded as *revisit-later*, with
  the trigger stated: if Keycloak's operational weight in production (JVM
  footprint, upgrade breakage, import fragility of the class that crash-looped
  the console on 2026-08-19) costs more support time than owning password
  hashing, brute-force protection, sessions, TOTP, a WebAuthn server, and
  SAML/OIDC brokering forever. Nobody buys an RMM for its login form; B is a
  last resort, not a roadmap item.
- **C — rented IdP (Auth0/WorkOS/Cognito) for cloud, Keycloak for self-host.**
  REJECTED: two auth stacks breaks the vision doc's "commercialization is a
  deployment change, not a rewrite", and the self-hosted half cannot depend on
  someone else's cloud.

**Why A is honest and not a fudge:** Atera shows one portal because Atera is
the operator, not because nothing sits behind their login. Half the industry
runs a rented IdP behind their own login page. The customer-visible property is
"one product", never "no IdP" — and Postgres is the precedent inside this very
stack: a component nobody asks the buyer to administer is a component that does
not exist for them.

---

## 1. Cycle 1 — kill every user-visible seam

### 1.1 In-flow account actions instead of the account console

The profile page's three deep links ("opens the account console") are replaced
by **application-initiated actions**: ITMS redirects to the OIDC auth endpoint
with `kc_action=UPDATE_PASSWORD`, `kc_action=CONFIGURE_TOTP`, or
`kc_action=webauthn-register-passwordless`. The user completes exactly that
step on the same ITMS-themed pages used for sign-in, and is redirected back to
the profile page. The account console is never rendered.

Mechanics: the frontend already holds `oidcAuthority`; the redirect is
`{authority}/protocol/openid-connect/auth?client_id=it-suite-web&redirect_uri=
{console}&response_type=code&scope=openid&kc_action={action}`. On return the
existing callback path completes the code exchange as any sign-in does. No new
credential, no new backend surface.

**Not verified until run against a live realm:** that each of the three
actions completes and round-trips under the committed theme. Stated here the
way the Keycloak-events spec states its own live-instance gap.

### 1.2 The account console and admin console leave the public network

`deploy/docker-compose.yml`'s Traefik rule for Keycloak currently routes
`/realms || /resources || /admin || /js`. It changes to route **only what
sign-in needs**: `/realms` and `/resources` and `/js` stay (the login pages,
theme assets, and OIDC endpoints live under them); **`/admin` is removed** from
the public entrypoint. The account console lives under
`/realms/it-suite/account` and cannot be un-routed by path prefix alone, so it
is disabled at the realm instead (`accountTheme` interactions replaced by 1.1;
the client `account-console` disabled in the realm). Break-glass: the vendor
reaches the admin console over an SSH tunnel to the container port —
documented in one appendix, marked vendor-only. The operator said it plainly:
the backend is theirs alone, so SSH-for-disaster is acceptable *there* and
nowhere else.

### 1.3 The word audit

No user-visible string may say "Keycloak". Today at least one does — the
profile page's "Keycloak asks for your current password first". All user-facing
copy says "the identity provider" or, better, nothing (a buyer does not care
what asks for the current password; the sentence works without an actor).

**Guard:** a frontend test greps every string literal in `frontend/src` for
the word (case-insensitive) and fails naming the file. A backend equivalent
covers API `detail` strings in responses reachable by a console user.
Code comments, internal module names, and vendor-facing docs are exempt — the
guard is about what a USER can see, and a guard broader than its claim would
just get deleted.

---

## 2. Cycle 2 — configuration moves into ITMS Settings

### 2.1 Settings → Email

A new Settings tab: host, port, TLS mode, from-address, display name,
credentials. Saving writes the realm's SMTP configuration through the admin
API. A **"send a test message"** button sends to the saved **sending address** and
reports delivery the honest way: "sent to <mailbox> — check that mailbox; a
mail server can accept and still drop silently", not a green checkmark that
only proves the relay answered.

*Amended 2026-09-23.* This section originally said the test goes to "the
signed-in operator's own address". That was never possible. Keycloak's
`testSMTPConnection` sends to the account that authenticated the call, which is
the provisioning service account. That account has no address, so every press
returned a 500 (`Logged in user does not have an e-mail.`), and the realm will
not let it borrow the operator's address (409, duplicate email). The service
account now holds the saved sending address for the length of one call and
is left with no address afterwards, whatever it held before. The route still takes no address, and
pointing the test anywhere else means changing the sending address, a separate
privileged and audited write that real mail uses too. See
`backend/app/modules/user_admin/realm_config.py`.

The SMTP password transits ITMS once, at configuration time, and is stored by
Keycloak — never persisted by ITMS, never echoed back in the form (the saved
state shows host/port/from and `password: set`). This does not violate
invariant 3's *stored*-credential rule, and the spec says so explicitly rather
than leaving the question open: the vault governs credentials ITMS stores; a
write-through field ITMS never retains is configuration, not custody. The
audit event records that email settings changed, never the values.

**Visibility rule (same one keycloak_events established):** until email is
configured, the Users tab shows a banner — "Email isn't set up, so invitations
can't be sent — Settings → Email" — and the create-user flow warns before, not
after. An unconfigured state must be visible where it bites, not discovered
from a failed invitation.

### 2.2 The email-domain allowlist becomes a setting, not an env var

`ITS_CONSOLE_EMAIL_DOMAIN` (env, restart to change, SSH preflight script)
contradicts the buyer test twice. It moves to a DB-backed org setting editable
in Settings → Organization, with the preflight becoming an **in-UI check**: the
form shows, before saving, exactly which existing users would be locked out —
the same query `scripts/preflight_email_domain.py` runs, rendered instead of
printed. Saving with the operator's own account in the refusal list is blocked
outright, closing the self-lockout hole structurally instead of procedurally.

Break-glass changes shape and is stated: the env var is demoted to an
**override switch** (`ITS_CONSOLE_EMAIL_DOMAIN_OVERRIDE=off`) that disables the
gate regardless of the DB setting — vendor-only, for the case where a bad
setting locked everyone out and no UI is reachable. The three doors read the
DB setting through a short-TTL cache so a change applies without restart.

The existing SSH preflight script is retired from the runbook (kept in
`scripts/` for the vendor).

---

## 3. Cycle 3 — first boot is a setup screen, not a runbook

### 3.1 What the buyer experiences

Install (vendor or their IT person: DNS, TLS, `docker compose up -d` — every
appliance has a rack-and-cable step). First browser visit shows an ITMS
**setup screen**: one-time install code, then your name, email, organization
name. Submit → you are the org-admin, signed in, looking at the dashboard.
Zero Keycloak steps, zero `docker compose exec`, zero SQL.

### 3.2 What actually happens underneath

- An **init job** in the compose stack (runs once, idempotent) uses the
  Keycloak bootstrap admin to: create the `it-suite-provisioning` and
  `it-suite-events` service clients, grant their roles, and write their
  secrets to the shared secrets volume. Today's runbook sections telling the
  operator to click these together in the admin console are retired to a
  vendor appendix.
- ITMS serves the setup screen only while `users` is empty AND a valid
  **install code** is presented — the code is generated at first start and
  printed in the api container's log plus written to the secrets volume
  (the Grafana/Portainer pattern). Zero-users alone is not enough: a stranger
  who reaches the URL first must not be able to claim the box.
- Submitting creates the Keycloak user (provisioning client), the org row, the
  org-admin assignment, and audits all of it — the same code paths the Users
  tab already exercises, so the wizard is a thin caller, not a second
  implementation.
- The Keycloak **bootstrap admin is retired by the init job** after the
  service clients exist: its password is rotated to a random value written
  only to the secrets volume. It is not deleted — Keycloak needs one master
  account to exist for vendor break-glass — but no human knows its password
  and no runbook step uses it again. GO-LIVE's "delete the bootstrap admin"
  instruction is superseded by this stronger, automated posture.

### 3.3 GO-LIVE shrinks

Steps 8–10 (first administrator via docker exec + Python, org row via SQL,
role grant via SQL) are replaced by "open the URL, enter the install code".
The runbook keeps: DNS/TLS, `.env` secrets, OpenBao init/unseal, cert
issuance — infrastructure, vendor-side, before the buyer test's clock starts.

---

## 4. The credential accounting, honestly

The `it-suite-provisioning` client widens from `manage-users` to
`manage-users + manage-realm` (SMTP write, realm login settings). This
partially reverses the 2026-08-19 narrowing decision, and the accounting is:

- `manage-users` alone already means whoever owns ITMS owns every identity.
  Realm configuration adds little to that worst case, and nothing to the
  identity-custody question.
- In exchange, the **human** bootstrap admin leaves daily operation entirely
  (3.2) — previously it existed, was documented, and the runbook merely asked
  the operator to please delete it. Net: fewer standing human credentials,
  one wider machine credential, all of it audited through ITMS's chain.
- The events client stays `view-events` only. The widening touches exactly one
  client.

What does NOT change: no password ever transits or rests in ITMS (the SMTP
write-through in 2.1 is the single, stated, non-stored exception), signed-job
invariants are untouched, and the console still never gates security.

## 5. Out of scope, named so it is a backlog and not an omission

The same buyer logic eventually reaches: agent release publishing (today a
curl upload), OpenBao unseal after host restart, backup/restore, in-product
updates, and an SSO (Entra) configuration UI. Each is vendor-side today by the
operator's own definition ("the backend will only be touched by me"), so none
blocks commercialization the way the identity surfaces do — but each is a
future cycle if buyers self-host without a technical vendor relationship.

## 6. Falsifiers

1. **The word guard is vacuous.** Plant "Keycloak" in a rendered string; the
   guard must name the file.
2. **The setup screen is reachable on a populated instance**, or without the
   install code. Either is a takeover path.
3. **`/admin` answers on the public entrypoint** after Cycle 1 — a compose
   test pins the Traefik rule.
4. **The SMTP form echoes the saved password back.**
5. **The allowlist form saves a value that locks out the operator saving it.**
6. **The wizard duplicates instead of reusing**: creating the first admin must
   go through the same service code as the Users tab, asserted by test, or the
   two paths drift.
7. **kc_action flows** — not testable in CI (no live realm); named unverified,
   to be exercised on the operator's deployment like every Keycloak surface
   before them.
