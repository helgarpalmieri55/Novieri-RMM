# Console sign-in — spec

**Status:** **built**, 2026-08-18, to the plan at
`docs/superpowers/plans/2026-08-18-console-auth.md`. Approved 2026-08-18
(operator chose "brand Keycloak, skip the launcher").

**Which falsifiers have tests.** 1, 2 (by proxy), 4 and 7 are in
`backend/tests/test_console_auth_realm.py`; 5 and 6 are in
`frontend/src/App.auth.test.tsx`. **Falsifier 3 is not tested** — asserting
that a federated token carries the `org` claim needs a real Entra tenant and a
real browser. The mapper is asserted as committed; the claim is not observed.
`deploy/README.md` gives the operator a one-liner to decode the token, which is
a procedure, not a test.

**Nothing here has rendered in a running Keycloak.** The theme is FreeMarker
executed by a Java process this suite does not run, so falsifier 2's real form
— "does a password field appear during a passkey-only flow" — needs a live
instance. The committed proxy asserts the template asks Keycloak which fields
to show rather than hardcoding them, and it was falsified by planting the
hardcoded form.

**Half of this module is an operator procedure, by construction.** Realm import
runs only against an empty database, so on a running install the WebAuthn
policy, the passkey browser flow and the Microsoft provider are applied by hand
(`deploy/README.md`, "Branding and extending sign-in"). The committed realm is
what a *future* deployment gets. Nothing in this repository can verify the live
realm, and no test here should be read as saying it has.

**Still open:** Keycloak's events do not reach the ITMS audit chain (§4, §7.1).
Passkey registration and identity-provider linking are credential changes on
privileged accounts, and today they exist only in Keycloak's own event log —
which is not tamper-evident and not in the chain.

**What this is.** The screen an operator meets before the console: email and
password, MFA, a passkey, and "Sign in with Microsoft", on one page, in ITMS
livery.

**Why it needs a spec at all.** The obvious implementation — put the password
field in the React app — is forbidden by this project's own go-live rules, and
the two capabilities being asked for (passkey, Microsoft) do not exist in the
realm today. Both are covered below.

---

## 1. Where the password field lives, and why it is not in ITMS

`deploy/GO-LIVE.md` §2 states the prohibition directly:

> **If you enabled `directAccessGrantsEnabled` on the `it-suite-web` client** to
> script the first-boot verification, **turn it back off.** A public client with
> the password grant enabled turns every leaked password into an API token with
> no browser and no MFA prompt.

An in-app email/password form needs that grant. It also puts fleet-admin
credentials through the console process, and — this is the part that makes it
self-defeating — it routes around the MFA prompt that was requested in the same
breath. The realm currently has `directAccessGrantsEnabled: false`, which is
correct, and this module does not change it.

So the form lives on **Keycloak's** login page, and the work is to make that page
look like ITMS and offer everything the reference screen offers. That costs
nothing visually: Keycloak themes are HTML/CSS.

---

## 2. What the realm has and has not

Read from `deploy/keycloak/realm-it-suite.json` on 2026-08-18:

| Capability | State |
|---|---|
| Username/password | ✅ works — this is what the current login already does |
| TOTP MFA | ✅ `CONFIGURE_TOTP` is a default required action, so every account created after the import is prompted to enrol |
| **Passkey / WebAuthn** | ❌ **no `webAuthnPolicy*` keys at all.** Not disabled — absent |
| **Microsoft / Entra** | ❌ `identityProviders` is absent |
| Password grant | ✅ correctly `false` |
| `org` attribute | ✅ declared in the realm's user profile, which is required — Keycloak 26 **silently discards** any attribute not declared there |

So passkey and Microsoft are **additions**, not restyling. The email/password
form and MFA already exist and only need branding.

**And a deployment fact that governs all of this: realm import runs only against
an empty database.** Editing `realm-it-suite.json` changes nothing on a running
install. Every item below therefore has two halves — the committed realm JSON for
future deployments, and an admin-API change for the live one. A spec that only
does the first ships a feature nobody can see.

---

## 3. Design

### 3.1 The theme

A Keycloak theme named `itms`, in the repository at
`deploy/keycloak/themes/itms/login/`, bind-mounted read-only into the container
at `/opt/keycloak/themes/itms`, with the realm's `loginTheme` set to `itms`.

A bind mount rather than baking it into an image: the theme is static assets that
an operator may legitimately want to re-brand, and the Keycloak image is pinned
and not built here.

The theme extends Keycloak's `base` (not `keycloak`) and supplies:

- `theme.properties` — `parent=base`, stylesheet list
- `login.ftl` — the credential form, plus the identity-provider block
- `resources/css/itms.css` (new) — the design tokens, copied from
  `frontend/src/index.css`: `--purple: #6f36d9`, `--radius: 16px`, the Inter
  stack. **Copied, not imported** — Keycloak cannot reach the React app's CSS,
  and a second copy of four tokens is cheaper than a build step that couples them.

**The one thing the theme must not do is invent its own form.** Keycloak's
`login.ftl` renders the form fields the *authentication flow* asks for; a theme
that hardcodes username and password will show a password box during a
passkey-only flow. Override the markup, keep the field loop.

### 3.2 Passkeys

Enable WebAuthn **passwordless** so the reference screen's "Sign in with a
passkey" is a first factor, not a second one:

- realm `webAuthnPolicyPasswordless*` keys: `rpEntityName: ITMS`,
  `signatureAlgorithms: [ES256, RS256]`, `attestationConveyancePreference: none`,
  `requireResidentKey: Yes`, `userVerificationRequirement: required`
- a browser flow with a **Passkey** alternative alongside the username/password
  alternative
- `webauthn-register-passwordless` as an optional required-action so an operator
  can enrol a key from their account page

**The policy decision, stated so it is not stumbled into:** a passwordless
passkey with `userVerificationRequirement: required` is *already* two factors —
possession of the authenticator plus the biometric or PIN that unlocks it. So a
passkey sign-in **does not** additionally prompt for TOTP, and that is correct
rather than a gap. Password sign-in still does.

What this means operationally: **an account with a passkey and no TOTP is
protected only as well as that one authenticator.** Lose the phone, lose the
account, and recovery is an admin resetting credentials. The console should
require TOTP enrolment *as well* for accounts holding `org-admin` or
`super-admin` — which is a realm-level required-action decision, not something
the theme can enforce.

### 3.3 Microsoft / Entra

An OIDC identity provider, alias `microsoft`, pointing at the operator's tenant.

Three things that are easy to get wrong and expensive to diagnose:

1. **The client secret must not be in the realm JSON.** That file is in git.
   The secret goes in `.env` and reaches the realm by admin API at deploy time,
   or by an operator in the admin console. The committed JSON carries the
   provider with an empty secret and a comment saying so.
2. **The `org` attribute mapper is mandatory, and its absence is masked.** A
   federated user arrives with no `org` attribute unless a mapper sets one.
   With `ITS_MULTI_TENANT=false` the API falls back to `ITS_DEFAULT_ORG_SLUG`,
   so the login *works* and every federated user silently lands in the default
   org. That is fine for a single-org deployment and a tenancy hole the moment
   there are two. The mapper is therefore required now, while there is one org
   and the mistake is invisible.
3. **`emailVerified` and account linking.** Decide explicitly whether a
   Microsoft identity whose email matches an existing local account links to it
   or creates a second one. Auto-linking on unverified email is an account
   takeover primitive; this spec requires `trustEmail: false` and the
   first-broker-login flow's "review profile" step left on, meaning a match
   prompts rather than silently links.

### 3.4 Removing the ITMS launcher

Today `frontend/src/App.tsx` renders a `LoginPage` at `/login` with one button
that calls `signIn()`. The operator chose to remove it: unauthenticated access
redirects straight to Keycloak.

The change is small — the `/login` route and `LoginPage` go, and the
`isAuthenticated()` guard calls `signIn()` instead of `<Navigate to="/login">`.
**Two failure modes the launcher was accidentally covering, which now need
explicit handling:**

1. **Keycloak unreachable.** The launcher rendered regardless; an auto-redirect
   has nothing to render if discovery fails. This is exactly the defect just
   fixed in `lib/auth.ts`, where a failed discovery fetch produced a button that
   did nothing and no error anywhere. The replacement **must** show a real error
   state — "sign-in is unavailable", with the authority it tried — rather than a
   blank page or a spinner. That is a required test, not a nicety.
2. **Redirect loops.** `/auth/callback` must be exempt from the guard, and a
   failed callback must land on the error state rather than re-entering
   `signIn()`. A loop here is indistinguishable from a hung page.

---

## 4. What must be audited

`auth.login` already exists and appears in the chain. Additions:

- passkey registration and removal (a credential change on a privileged account)
- identity-provider link created / removed (this is how an account gains a second
  way in)

Keycloak has its own event log; these need to reach **ITMS's** hash-chained audit
log to be evidence in the same chain as everything else.

**Built 2026-08-18, as a pull.** `backend/app/modules/keycloak_events/` sweeps
Keycloak's admin events endpoint hourly and writes the four events above into
the chain as `auth.passkey_registered`, `auth.passkey_removed`,
`auth.identity_linked` and `auth.identity_unlinked`. §7.1 below records the
choice and what it costs; `deploy/README.md`, "Forwarding Keycloak's credential
events", is the operator's half.

Three things about it that are properties of the design rather than of the
implementation:

- **Logins are deliberately not forwarded.** `auth.login` already exists in this
  chain, written by the API when a token is presented. A second source would put
  two rows of different provenance behind one fact, and forwarding every login
  in the realm would bury the four events that matter.
- **An event whose Keycloak subject resolves to no ITMS user — or, legally, to
  users in two orgs — is counted and reported, never written.** The realm is
  shared by every tenant; filing one tenant's credential change into another's
  evidence is worse than not forwarding it, and the audit log is append-only.
- **An unconfigured deployment reports `keycloak_events: ok, configured:
  false`** on the platform status page rather than being silent. An audit
  feature that quietly does nothing is worse than one that does not exist.

---

## 5. Falsifiers

1. **`directAccessGrantsEnabled` is true on any client.** A test over the realm
   JSON, and a runtime check against the live realm during preflight.
2. **The theme renders a password field during a passkey-only flow.** Requires a
   real Keycloak; a template test that asserts the field loop is used rather than
   hardcoded markup is the achievable proxy.
3. **A federated login produces a token with no `org` claim.** Assert the mapper
   exists in the realm and that a token minted through the IdP carries it. The
   `ITS_MULTI_TENANT=false` fallback will hide this, so the assertion must be on
   the **claim**, not on whether the request succeeds.
4. **The client secret appears in a committed file.** A grep guard, in the spirit
   of `test_deploy_runbook_secrets.py`.
5. **Sign-in failure shows nothing.** With the authority pointed at a dead host,
   the app must render an error naming the problem. This is the regression that
   just cost a live afternoon.
6. **`/auth/callback` re-enters `signIn()`** on failure. Assert the error state,
   not a loop.
7. **`trustEmail` is true, or review-profile is skipped.** Account-linking
   takeover.
8. **A forwarded Keycloak event lands in an org its user does not belong to.**
   *(Added 2026-08-18 with §4's implementation.)* Asserted on the BYPASSRLS
   admin engine and scoped per org: a global count would be satisfied by the row
   landing in either tenant. Falsified by planting a default-org fallback, which
   turns two tests red.
9. **The same Keycloak event is written twice.** Keycloak's date filters are
   day-granular, so an hourly sweep re-reads the same day twenty-four times. A
   duplicate does not corrupt the hash chain; it pollutes it, and an auditor
   counting passkey registrations would count each one once per sweep.
10. **A login event reaches the ITMS chain.** The firehose, and the failure
    nobody notices until the log is unusable.
11. **Nothing schedules the sweep.** Every other guard here would still pass,
    the probe would read `configured, never run` for ever, and the chain would
    stay empty. The beat entry is asserted against the module's own interval
    constant, not a literal.

---

## 6. Ordering, because half of this is a live-instance change

1. Theme in the repo, `loginTheme` set on the realm JSON — safe, no live effect.
2. Mount the theme, set `loginTheme` on the **live** realm via admin API,
   restart Keycloak. Visible immediately; no auth-flow change yet.
3. WebAuthn policy + browser flow on the live realm. Test with a real key.
4. Entra provider + `org` mapper, secret from `.env`. Test with one account
   before anyone else is federated.
5. Remove the launcher **last** — only once Keycloak's page is known good, so a
   mistake in steps 1–4 leaves a working way in.

Step 5 last is the important one. Removing the launcher before the themed page
works means debugging a redirect you cannot stop.

---

## 7. Open questions

1. ~~**Keycloak events into the ITMS audit chain** (§4).~~ **ANSWERED
   2026-08-18: the sweep.** The push option means shipping a Java event-listener
   SPI into a Keycloak image this project pins and does not build, and giving
   Keycloak a credential to call back into ITMS — a new inbound path,
   authenticated by a secret Keycloak stores. The sweep needs no plugin, no
   image change and no inbound path.

   **What it costs, stated rather than hidden:** up to one hour of lag
   (`SWEEP_INTERVAL_S`). A faster beat would buy nothing — Keycloak's own
   `dateFrom`/`dateTo` filters are day-granular, which is also why every
   forwarded event is deduplicated on a SHA-256 fingerprint rather than trusted
   to a high-water mark.

   **The credential:** a dedicated service-account client (`it-suite-events`)
   holding **`view-events` and nothing else**, with its secret read from a file
   path following `audit_hmac_key_path` — never the bootstrap admin, which can
   reset any password and create any user. If a later version needs a second
   Keycloak role, that is a change to what this credential can do and it is the
   operator's decision, not a patch.

   **What forwarding does not make true.** Keycloak's own log remains the
   source. A Keycloak compromised before a sweep runs can drop an event this
   will never see, and no wording makes that absence detectable from here.
   Forwarding puts the events that *are* emitted into a tamper-evident chain; it
   does not make the emitter trustworthy, and it does not detect deletion at the
   source.

   **Not verified:** nothing in this path has run against a live Keycloak. The
   mapping, the org resolution, the dedupe and the probe are covered by tests
   against a real Postgres; the HTTP conversation with Keycloak's admin API is
   not, and neither is the claim that enabling **Realm settings → Events** is
   what makes the endpoint return rows.
2. **Whether TOTP stays mandatory for privileged roles** once passkeys exist
   (§3.2). Recommended, not decided.
3. ~~**Self-service passkey enrolment** — Keycloak's account console at
   `/realms/it-suite/account` would serve it, but that path is not currently
   routed by Traefik.~~ **ANSWERED 2026-08-19: it is already routed. Nothing
   needs building, and the question's premise was wrong.**

   The question told the next reader to verify rather than assume, so here is
   the verification. `deploy/docker-compose.yml`, on the `keycloak` service:

   ```yaml
   traefik.http.routers.kc.rule: PathPrefix(`/realms`) || PathPrefix(`/resources`) || PathPrefix(`/admin`) || PathPrefix(`/js`)
   traefik.http.routers.kc.entrypoints: websecure
   traefik.http.routers.kc.tls: "true"
   ```

   `/realms/it-suite/account` sits under the first of those prefixes, so the
   account console is reachable at `https://<host>/realms/it-suite/account` on
   the existing `websecure` entrypoint with **no Traefik change at all**. The
   other prefixes matter too, which is why "the path is routed" and "the
   console works" are not the same claim:

   | Prefix | What the account console needs it for |
   |---|---|
   | /realms | the page itself, and its Account REST API under the same path — what the SPA calls to list and delete credentials |
   | /resources | the console's own JS and CSS, served from `/resources/<version>/account/<theme>/` |
   | /js | `keycloak.js`, which the SPA uses to obtain its token |
   | /admin | **not** used by the account console — that is the *admin* console, which `deploy/GO-LIVE.md` §2 requires you to restrict before go-live |

   The runbook already depends on this and did so before the question was
   written: "Branding and extending sign-in → 1. The ITMS login theme" tells
   the operator to check the theme took effect at
   `https://itms.toeshee.com/realms/it-suite/account`.

   **So the self-service half of piece D is a link, not a feature.** The
   console deep-links there per action ("Add a passkey", "Change password",
   "Manage MFA device") — §7.1 above, which chose deep-linking over iframing
   (Keycloak refuses framing by default) and over proxying the actions through
   the provisioning credential (which would make ITMS the thing that can reset
   anybody's MFA).

   **What this does NOT close.** Routing is a property of
   `deploy/docker-compose.yml`, and narrowing that rule later would break every
   deep link silently — a 404 behind a button on a page nobody opens until they
   need it. `backend/tests/test_console_auth_realm.py::test_the_account_console_path_is_actually_routed_by_traefik`
   reads the rule out of the compose file for exactly that reason. And nothing
   here has been loaded in a browser against a live Keycloak: this is a routing
   claim, not a claim that Keycloak 26's account console presents passkey
   enrolment the way an operator expects.

4. **The passwordless browser flow is a runbook step, not a committed flow.**
   *(Recorded 2026-08-18, during implementation.)* The policy keys and the
   `webauthn-register-passwordless` required action are in
   `realm-it-suite.json`; the browser flow carrying a Passkey alternative
   beside username/password is built in the admin console —
   `deploy/README.md`, "Branding and extending sign-in". A hand-authored
   `authenticationFlows` graph does not fail loudly when it is wrong; it locks
   every account out of a console whose only other way in is that same flow.
   Worth committing once a deployment has verified one by hand and can export
   it.
