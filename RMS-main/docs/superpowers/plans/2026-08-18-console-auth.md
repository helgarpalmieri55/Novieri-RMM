# Console sign-in Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** The screen an operator meets before the console is Keycloak's, in ITMS livery, offering email + password with MFA, a passkey, and "Sign in with Microsoft" — with the ITMS launcher removed and a real error state when Keycloak is unreachable.

**Architecture:** The password field stays on Keycloak (`deploy/GO-LIVE.md` §2 forbids `directAccessGrantsEnabled`, which an in-app form would need). So the work is a Keycloak theme bind-mounted from the repo, realm additions for WebAuthn-passwordless and an Entra OIDC provider, and a small change in `App.tsx`/`lib/auth.ts` to redirect straight to Keycloak. **Realm import runs only against an empty database**, so every realm change ships twice: in the committed JSON for future deployments, and as an admin-API runbook step for the live one.

**Tech Stack:** Keycloak 26.0 (FreeMarker `.ftl` templates, `theme.properties`), React + TypeScript (strict) + vitest, Python 3.12 + pytest for the repo-level guards.

## Global Constraints

Copied verbatim from `docs/modules/console-auth/spec.md` — every task's requirements implicitly include these.

- **`directAccessGrantsEnabled` stays `false` on every client.** A public client with the password grant turns every leaked password into an API token with no browser and no MFA prompt (`deploy/GO-LIVE.md` §2).
- **The Entra client secret never enters a committed file.** It reaches the realm from `.env` by admin API, or by an operator in the admin console. The committed JSON carries the provider with an **empty** secret and a comment saying so.
- **The theme must not invent its own form.** Keycloak's `login.ftl` renders the fields the *authentication flow* asks for; hardcoded username/password markup shows a password box during a passkey-only flow. Override the markup, keep the field loop.
- **`trustEmail: false`, and the first-broker-login "review profile" step stays on.** Auto-linking a Microsoft identity to a local account on unverified email is an account-takeover primitive.
- **The `org` attribute mapper on the IdP is mandatory.** With `ITS_MULTI_TENANT=false` the API falls back to `ITS_DEFAULT_ORG_SLUG`, so a missing mapper still *logs in* and silently lands every federated user in the default org. Assert on the **claim**, never on whether the request succeeded.
- **Design tokens, copied not imported:** `--purple: #6f36d9`, `--radius: 16px`, the Inter stack — from `frontend/src/index.css`. Keycloak cannot reach the React app's CSS.
- **Theme path:** repo `deploy/keycloak/themes/itms/login/`, mounted read-only at `/opt/keycloak/themes/itms`, realm `loginTheme: itms`.
- **WebAuthn is `webAuthnPolicyPasswordless*`** (a first factor), with `rpEntityName: ITMS`, `signatureAlgorithms: [ES256, RS256]`, `attestationConveyancePreference: none`, `requireResidentKey: Yes`, `userVerificationRequirement: required`. A passkey sign-in does **not** additionally prompt for TOTP; that is correct, not a gap.
- **Ordering is a safety property (spec §6):** the launcher is removed **last**, only once Keycloak's page is known good. Removing it earlier means debugging a redirect you cannot stop.

## Not in this plan, named rather than dropped

**Keycloak's events do not reach the ITMS audit chain** (spec §4). Passkey
registration and identity-provider linking are credential changes on privileged
accounts, and invariant 4 wants them in the same hash-chained log as job
dispatch. The spec leaves the mechanism genuinely unresolved (§7.1: an event
listener SPI that pushes and needs a credential, or a worker sweep that pulls
and lags), and choosing one is its own cycle. **Until then, those two events
exist only in Keycloak's own event log** — which is not tamper-evident and is
not in the chain. That is a gap this plan does not close, and the spec's status
block (Task 6) must say so.

---

## File Structure

| File | Responsibility |
|---|---|
| `deploy/keycloak/themes/itms/login/theme.properties` | Declares `parent=base` and the stylesheet list |
| `deploy/keycloak/themes/itms/login/login.ftl` | The credential form + identity-provider block, over Keycloak's field loop |
| `deploy/keycloak/themes/itms/login/resources/css/itms.css` | The four design tokens and the page's layout |
| `deploy/keycloak/realm-it-suite.json` | `loginTheme`, `webAuthnPolicyPasswordless*`, the `microsoft` IdP with an empty secret, its `org` mapper |
| `deploy/docker-compose.yml` | The theme bind mount |
| `backend/tests/test_console_auth_realm.py` | Guards over the committed realm JSON and the theme (falsifiers 1, 2, 4, 7) |
| `frontend/src/lib/auth.ts` | `signIn()` gains a failure path that surfaces, rather than a rejected promise nobody renders |
| `frontend/src/App.tsx` | `/login` and `LoginPage` removed; `Protected` calls `signIn()`; a `SignInUnavailable` error state |
| `frontend/src/App.auth.test.tsx` | Falsifiers 5 and 6 |
| `deploy/README.md` | The live-instance half: admin-API steps 2–4 of spec §6 |

---

### Task 1: The theme, and the guards that keep it honest

**Files:**
- Create: `deploy/keycloak/themes/itms/login/theme.properties`
- Create: `deploy/keycloak/themes/itms/login/login.ftl`
- Create: `deploy/keycloak/themes/itms/login/resources/css/itms.css`
- Create: `backend/tests/test_console_auth_realm.py`
- Modify: `deploy/docker-compose.yml` (keycloak `volumes:`)
- Modify: `deploy/keycloak/realm-it-suite.json` (`"loginTheme": "itms"`)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: the theme directory at the path Task 2's realm keys and Task 4's runbook both name; `backend/tests/test_console_auth_realm.py` with `REALM` and `THEME` module constants that Tasks 2 and 3 add tests to.

- [x] **Step 1: Write the failing guards**

`backend/tests/test_console_auth_realm.py`:

```python
"""The committed realm and theme, pinned (docs/modules/console-auth/spec.md §5).

WHY A TEXT-LEVEL GUARD IS THE RIGHT TOOL. None of this runs in the test suite:
there is no Keycloak here, the theme is FreeMarker rendered by a Java process,
and the realm JSON is consumed by an importer that only runs against an empty
database. So the achievable check is over what is COMMITTED -- and that is
where three of the spec's falsifiers actually live, because each one is a
property of a file somebody edits.

WHAT THIS CANNOT SHOW, stated so nobody reads green here as "sign-in works":
it never renders a page, never mints a token, and says nothing about the LIVE
realm, which diverges from this file the moment an operator touches the admin
console. `deploy/README.md`'s admin-API steps are the other half.
"""

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
REALM_PATH = REPO / "deploy/keycloak/realm-it-suite.json"
THEME = REPO / "deploy/keycloak/themes/itms/login"
REALM = json.loads(REALM_PATH.read_text())


def test_the_realm_file_is_where_this_thinks_it_is() -> None:
    """Non-vacuity. Every assertion below reads REALM; a moved file would make
    them all pass over `{}` if the load were tolerant, and this makes the
    location itself an assertion rather than an assumption."""
    assert REALM_PATH.is_file(), REALM_PATH
    assert REALM["realm"] == "it-suite", REALM.get("realm")


def test_no_client_enables_the_password_grant() -> None:
    """§5.1. A public client with `directAccessGrantsEnabled` turns every
    leaked password into an API token with no browser and no MFA prompt --
    which also routes around the MFA this module exists to present."""
    offenders = [
        c["clientId"] for c in REALM["clients"] if c.get("directAccessGrantsEnabled")
    ]
    assert not offenders, offenders


def test_the_realm_asks_for_the_itms_theme() -> None:
    assert REALM.get("loginTheme") == "itms", REALM.get("loginTheme")


def test_the_theme_exists_at_the_path_the_realm_names() -> None:
    """A `loginTheme` naming a directory that is not mounted makes Keycloak
    fall back to its own theme SILENTLY -- the login page simply looks
    unbranded, with nothing in any log to say why."""
    assert (THEME / "theme.properties").is_file()
    assert (THEME / "login.ftl").is_file()
    assert (THEME / "resources/css/itms.css").is_file()


def test_the_theme_is_mounted_into_the_container() -> None:
    compose = (REPO / "deploy/docker-compose.yml").read_text()
    assert "./keycloak/themes:/opt/keycloak/themes:ro" in compose


def test_the_login_template_uses_the_field_loop_rather_than_hardcoded_inputs() -> None:
    """§5.2, as close as this can get without a running Keycloak.

    A theme that hardcodes `<input name="password">` renders a password box
    during a passkey-only flow, because the FLOW decides which credentials are
    asked for and the template only decides how they look. The achievable proxy
    is that the template asks Keycloak which fields to show.
    """
    ftl = (THEME / "login.ftl").read_text()
    assert "usernameHidden" in ftl or "auth.selectedCredential" in ftl, ftl[:400]
    assert "realm.password" in ftl, "the password block must be conditional on the flow"


def test_the_theme_carries_the_console_design_tokens() -> None:
    css = (THEME / "resources/css/itms.css").read_text()
    for token in ("#6f36d9", "16px", "Inter"):
        assert token in css, token
```

- [x] **Step 2: Run to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_console_auth_realm.py -q`
Expected: FAIL — `test_the_realm_asks_for_the_itms_theme` (no `loginTheme`), the three theme-file tests (no directory), the mount test, and both template tests. `test_no_client_enables_the_password_grant` should PASS already: the realm is correct today, and that is the point of pinning it.

- [x] **Step 3: Write the theme**

`deploy/keycloak/themes/itms/login/theme.properties`:

```properties
# ITMS login theme.
#
# parent=base, NOT keycloak. `base` supplies the templates and the messages and
# no styling at all, which is what a from-scratch brand wants; extending
# `keycloak` would inherit its stylesheet and leave us overriding it.
parent=base
import=common/keycloak

styles=css/itms.css

# Keycloak 26 renders these from the messages bundle; naming them here keeps
# the page's copy in one place rather than inside the template.
kcHtmlClass=itms
kcLoginClass=itms-login
```

`deploy/keycloak/themes/itms/login/login.ftl`:

```html
<#import "template.ftl" as layout>
<#--
  The credential form.

  THE FIELD LOOP IS THE POINT. `realm.password`, `usernameHidden` and
  `social.providers` are set by the AUTHENTICATION FLOW, not by this file. A
  template that hardcodes a username and a password input renders a password
  box during a passkey-only flow -- the spec's falsifier 2 -- so every block
  below is conditional on what the flow actually asked for.
-->
<@layout.registrationLayout displayMessage=!messagesPerField.existsError('username','password'); section>
  <#if section = "header">
    ITMS
  <#elseif section = "form">
    <#if realm.password>
      <form id="kc-form-login" action="${url.loginAction}" method="post">
        <#if !usernameHidden??>
          <div class="itms-field">
            <label for="username">${msg("email")}</label>
            <input id="username" name="username" type="text" autofocus
                   autocomplete="username webauthn"
                   value="${(login.username!'')}" />
          </div>
        </#if>
        <div class="itms-field">
          <label for="password">${msg("password")}</label>
          <input id="password" name="password" type="password"
                 autocomplete="current-password" />
        </div>
        <input type="hidden" id="id-hidden-input" name="credentialId"
               <#if auth.selectedCredential?has_content>value="${auth.selectedCredential}"</#if> />
        <button class="itms-primary" name="login" id="kc-login" type="submit">
          ${msg("doLogIn")}
        </button>
      </form>
    </#if>

    <#-- Identity providers: Microsoft arrives here once the IdP exists. The
         block renders nothing at all when `social.providers` is empty, so this
         file is correct both before and after Task 3. -->
    <#if realm.password && social.providers??>
      <div class="itms-divider"><span>or</span></div>
      <div class="itms-social">
        <#list social.providers as p>
          <a class="itms-social-link" href="${p.loginUrl}" id="social-${p.alias}">
            ${p.displayName!p.alias}
          </a>
        </#list>
      </div>
    </#if>
  </#if>
</@layout.registrationLayout>
```

`deploy/keycloak/themes/itms/login/resources/css/itms.css`:

```css
/* ITMS login.
 *
 * The four tokens are COPIED from frontend/src/index.css, not imported:
 * Keycloak serves this file from its own container and cannot reach the React
 * app's stylesheet. Two copies of four values is cheaper than a build step
 * that couples the console's bundle to a Java process's theme directory.
 */
:root {
  --purple: #6f36d9;
  --radius: 16px;
  --ink: #14121a;
  --muted: #6c6a76;
  --line: #e6e3ee;
  --font: Inter, "Inter var", system-ui, -apple-system, "Segoe UI", sans-serif;
}

body {
  margin: 0;
  min-height: 100vh;
  display: grid;
  place-items: center;
  font-family: var(--font);
  color: var(--ink);
  background: #f6f4fb;
}

#kc-header, .itms-card { width: min(400px, 92vw); }

.itms-card, .login-pf-page {
  background: #fff;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 32px;
}

.itms-field { display: grid; gap: 6px; margin-bottom: 16px; }
.itms-field label { font-size: 13px; color: var(--muted); }
.itms-field input {
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 10px 12px;
  font: inherit;
}

.itms-primary {
  width: 100%;
  border: 0;
  border-radius: 10px;
  padding: 11px 16px;
  background: var(--purple);
  color: #fff;
  font: inherit;
  font-weight: 600;
  cursor: pointer;
}

.itms-divider {
  display: flex; align-items: center; gap: 12px;
  margin: 20px 0; color: var(--muted); font-size: 13px;
}
.itms-divider::before, .itms-divider::after {
  content: ""; flex: 1; height: 1px; background: var(--line);
}

.itms-social-link {
  display: block; text-align: center; text-decoration: none;
  border: 1px solid var(--line); border-radius: 10px;
  padding: 10px 16px; color: var(--ink); font-weight: 500;
}
```

- [x] **Step 4: Mount it and name it**

`deploy/docker-compose.yml`, in the `keycloak` service's `volumes:` (immediately after the realm import line):

```yaml
      # The ITMS login theme, read-only. A BIND MOUNT rather than a baked
      # image: the Keycloak image is pinned and not built here, and the theme
      # is static assets an operator may legitimately re-brand.
      #
      # If this mount is missing, `loginTheme: itms` falls back to Keycloak's
      # own theme SILENTLY -- an unbranded page and nothing in any log.
      - ./keycloak/themes:/opt/keycloak/themes:ro
```

`deploy/keycloak/realm-it-suite.json`, alongside `"sslRequired"`:

```json
  "loginTheme": "itms",
```

- [x] **Step 5: Run the guards**

Run: `cd backend && .venv/bin/pytest tests/test_console_auth_realm.py -q`
Expected: PASS (8 tests)

- [x] **Step 6: Falsify two of them**

Guards over file text stop matching silently. Prove these two can fail:

```bash
cd /home/user/Toeshee-RMS
# 1. A hardcoded form: delete the `realm.password` conditional
python3 - <<'EOF'
import pathlib
p = pathlib.Path("deploy/keycloak/themes/itms/login/login.ftl")
p.write_text(p.read_text().replace("<#if realm.password>", "<#if true>"))
EOF
cd backend && .venv/bin/pytest tests/test_console_auth_realm.py -q -k field_loop
# Expected: FAIL
cd .. && git checkout deploy/keycloak/themes/itms/login/login.ftl
```

Then plant `"directAccessGrantsEnabled": true` on the client, confirm `test_no_client_enables_the_password_grant` goes red, and revert. **Confirm each plant is actually on disk before reading the result** — a plant that silently failed to apply produces a green run that proves nothing.

- [x] **Step 7: Commit**

```bash
git add deploy/keycloak/themes deploy/keycloak/realm-it-suite.json deploy/docker-compose.yml backend/tests/test_console_auth_realm.py
git commit -m "console-auth: the ITMS login theme, and guards over the realm it needs"
```

---

### Task 2: Passkeys in the committed realm

**Files:**
- Modify: `deploy/keycloak/realm-it-suite.json`
- Modify: `backend/tests/test_console_auth_realm.py`

**Interfaces:**
- Consumes: `REALM` from Task 1's test module.
- Produces: `webAuthnPolicyPasswordless*` keys and the `webauthn-register-passwordless` required action, which Task 4's runbook mirrors onto the live realm.

- [x] **Step 1: Write the failing tests**

Append to `backend/tests/test_console_auth_realm.py`:

```python
def test_passwordless_webauthn_is_configured_as_a_first_factor() -> None:
    """§3.2. `webAuthnPolicy*` (no suffix) is the SECOND-factor policy; the
    reference screen asks for "sign in with a passkey" as a first factor, which
    is the PASSWORDLESS policy. Setting the wrong one produces a realm where
    passkeys work only after a password -- the feature appears present and does
    the other thing."""
    assert REALM.get("webAuthnPolicyPasswordlessRpEntityName") == "ITMS"
    assert REALM.get("webAuthnPolicyPasswordlessSignatureAlgorithms") == ["ES256", "RS256"]
    assert REALM.get("webAuthnPolicyPasswordlessAttestationConveyancePreference") == "none"
    assert REALM.get("webAuthnPolicyPasswordlessRequireResidentKey") == "Yes"
    assert REALM.get("webAuthnPolicyPasswordlessUserVerificationRequirement") == "required"


def test_user_verification_is_required_because_it_is_the_second_factor() -> None:
    """The policy decision, pinned rather than left as prose.

    A passwordless passkey with user verification required IS two factors --
    the authenticator plus the biometric or PIN unlocking it -- which is why a
    passkey sign-in does not additionally prompt for TOTP. Relax this to
    `preferred` and the passkey silently becomes ONE factor while the flow that
    skips TOTP stays exactly as it was."""
    assert REALM["webAuthnPolicyPasswordlessUserVerificationRequirement"] == "required"


def test_an_operator_can_enrol_a_passkey_themselves() -> None:
    actions = {a["alias"]: a for a in REALM["requiredActions"]}
    assert "webauthn-register-passwordless" in actions, sorted(actions)
    action = actions["webauthn-register-passwordless"]
    assert action["enabled"] is True
    # OPTIONAL, not default: a required action with defaultAction=true fires for
    # every account at next login, and an operator with no authenticator to hand
    # is then locked out of the console by a feature meant to add a way IN.
    assert action["defaultAction"] is False


def test_totp_is_still_a_default_action() -> None:
    """Non-regression for the factor that exists today. Adding passkeys must
    not quietly remove the MFA every password account gets."""
    actions = {a["alias"]: a for a in REALM["requiredActions"]}
    assert actions["CONFIGURE_TOTP"]["defaultAction"] is True
```

- [x] **Step 2: Run to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_console_auth_realm.py -q -k "webauthn or passwordless or passkey or totp"`
Expected: FAIL — the realm has no `webAuthnPolicy*` keys at all (spec §2: "Not disabled — absent"). `test_totp_is_still_a_default_action` passes today.

- [x] **Step 3: Add the policy and the required action**

In `deploy/keycloak/realm-it-suite.json`, at the top level:

```json
  "webAuthnPolicyPasswordlessRpEntityName": "ITMS",
  "webAuthnPolicyPasswordlessSignatureAlgorithms": ["ES256", "RS256"],
  "webAuthnPolicyPasswordlessAttestationConveyancePreference": "none",
  "webAuthnPolicyPasswordlessRequireResidentKey": "Yes",
  "webAuthnPolicyPasswordlessUserVerificationRequirement": "required",
  "webAuthnPolicyPasswordlessCreateTimeout": 0,
  "webAuthnPolicyPasswordlessAvoidSameAuthenticatorRegister": false,
```

and in `requiredActions`, after the `CONFIGURE_TOTP` entry:

```json
    {
      "alias": "webauthn-register-passwordless",
      "name": "Webauthn Register Passwordless",
      "providerId": "webauthn-register-passwordless",
      "enabled": true,
      "defaultAction": false,
      "priority": 30
    }
```

- [x] **Step 4: Run the tests**

Run: `cd backend && .venv/bin/pytest tests/test_console_auth_realm.py -q`
Expected: PASS (12 tests)

- [x] **Step 5: Record what the realm file cannot do**

The **browser flow** with a Passkey alternative (spec §3.2) is not expressible as a handful of top-level keys — it is an `authenticationFlows` graph, and getting it wrong locks everyone out of the console. Add this to the spec's §7 open questions rather than guessing:

```markdown
4. **The passwordless browser flow is a runbook step, not a committed flow.**
   The policy keys and the required action are in `realm-it-suite.json`
   (2026-08-18), but the browser flow carrying a Passkey alternative alongside
   username/password is built in the admin console — `deploy/README.md`,
   "Passkeys". A hand-authored `authenticationFlows` graph that is wrong does
   not fail loudly; it locks every account out of a console whose only other
   way in is the same flow. Committing one is worth doing once a deployment has
   verified it by hand and can export it.
```

- [x] **Step 6: Commit**

```bash
git add deploy/keycloak/realm-it-suite.json backend/tests/test_console_auth_realm.py docs/modules/console-auth/spec.md
git commit -m "console-auth: passwordless WebAuthn in the committed realm"
```

---

### Task 3: The Microsoft identity provider, with no secret in git

**Files:**
- Modify: `deploy/keycloak/realm-it-suite.json`
- Modify: `backend/tests/test_console_auth_realm.py`
- Modify: `deploy/.env.example`

**Interfaces:**
- Consumes: `REALM`, `REALM_PATH` from Task 1's test module.
- Produces: an `identityProviders` entry with alias `microsoft` and an `org` mapper, which Task 4's runbook fills in with the tenant's secret.

- [x] **Step 1: Write the failing tests**

Append to `backend/tests/test_console_auth_realm.py`:

```python
def _microsoft() -> dict:
    providers = {p["alias"]: p for p in REALM.get("identityProviders", [])}
    assert "microsoft" in providers, sorted(providers)
    return providers["microsoft"]


def test_the_microsoft_provider_exists_and_carries_no_secret() -> None:
    """§5.4. This file is in git. A client secret committed here is a
    credential in every clone, every fork and every CI cache -- and rotating it
    means rewriting history, not editing a file."""
    provider = _microsoft()
    assert provider["providerId"] == "oidc"
    assert provider["config"].get("clientSecret", "") == "", provider["config"]


def test_no_plausible_secret_appears_anywhere_in_the_realm_file() -> None:
    """Broader than the field above, because the next person to add a provider
    will not add it to the same key. Anything that looks like an Entra client
    secret (Azure emits ~40 chars of base64-ish text) is refused."""
    import re

    text = REALM_PATH.read_text()
    suspects = re.findall(r'"(?:clientSecret|secret)"\s*:\s*"([^"]{8,})"', text)
    assert not suspects, suspects


def test_a_federated_login_is_forced_to_carry_an_org_claim() -> None:
    """§5.3, and the one that ITS_MULTI_TENANT=false actively HIDES.

    Without this mapper a federated user arrives with no `org` attribute, the
    API falls back to ITS_DEFAULT_ORG_SLUG, the login SUCCEEDS, and every
    Microsoft account silently lands in the default org. That is fine with one
    org and a tenancy hole the moment there are two -- so the mapper is
    required now, while the mistake is invisible.
    """
    mappers = {m["name"]: m for m in _microsoft().get("mappers", [])}
    assert "org" in mappers, sorted(mappers)
    config = mappers["org"]["config"]
    assert config["user.attribute"] == "org"
    assert config["attribute.value"], "the mapper must set a value, not just name one"


def test_account_linking_cannot_be_silent() -> None:
    """§5.7. Auto-linking a Microsoft identity to a local account whose email
    it merely claims is an account-takeover primitive: whoever controls that
    mailbox name at the IdP inherits the local account's roles."""
    provider = _microsoft()
    assert provider.get("trustEmail") is False
    assert provider.get("firstBrokerLoginFlowAlias", "first broker login") == "first broker login"
```

- [x] **Step 2: Run to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_console_auth_realm.py -q -k "microsoft or federated or linking or secret"`
Expected: FAIL — `identityProviders` is absent from the realm entirely.

- [x] **Step 3: Add the provider**

In `deploy/keycloak/realm-it-suite.json`, at the top level. **`clientSecret` is deliberately empty** — see the guard above:

```json
  "identityProviders": [
    {
      "alias": "microsoft",
      "displayName": "Microsoft",
      "providerId": "oidc",
      "enabled": true,
      "trustEmail": false,
      "storeToken": false,
      "linkOnly": false,
      "firstBrokerLoginFlowAlias": "first broker login",
      "config": {
        "clientId": "REPLACE_WITH_ENTRA_APPLICATION_ID",
        "clientSecret": "",
        "clientAuthMethod": "client_secret_post",
        "defaultScope": "openid profile email",
        "syncMode": "IMPORT",
        "pkceEnabled": "true",
        "pkceMethod": "S256"
      },
      "mappers": [
        {
          "name": "org",
          "identityProviderAlias": "microsoft",
          "identityProviderMapper": "oidc-hardcoded-attribute-idp-mapper",
          "config": {
            "syncMode": "INHERIT",
            "attribute": "org",
            "user.attribute": "org",
            "attribute.value": "REPLACE_WITH_ORG_SLUG"
          }
        }
      ]
    }
  ],
```

Three things this deliberately leaves for the operator, because they are per-tenant and cannot be guessed: the Entra application id, the tenant's discovery URL (`authorizationUrl`/`tokenUrl`, or `metadataDescriptorUrl` — the admin console derives them from the tenant), and the org slug in the mapper.

`deploy/.env.example`, in the Keycloak block:

```bash
# Microsoft / Entra sign-in (OPTIONAL). The realm ships the provider with an
# EMPTY secret on purpose: deploy/keycloak/realm-it-suite.json is in git. This
# value reaches the live realm through the admin API step in deploy/README.md,
# "Sign in with Microsoft", and is never committed.
ENTRA_CLIENT_ID=
ENTRA_CLIENT_SECRET=
ENTRA_TENANT_ID=
```

- [x] **Step 4: Run the tests**

Run: `cd backend && .venv/bin/pytest tests/test_console_auth_realm.py -q`
Expected: PASS (17 tests)

- [x] **Step 5: Falsify the secret guard**

```bash
cd /home/user/Toeshee-RMS
python3 - <<'EOF'
import json, pathlib
p = pathlib.Path("deploy/keycloak/realm-it-suite.json")
d = json.loads(p.read_text())
d["identityProviders"][0]["config"]["clientSecret"] = "Qq8~jK2sample_not_a_real_secret_abcdef1234"
p.write_text(json.dumps(d, indent=2) + "\n")
EOF
grep -c "not_a_real_secret" deploy/keycloak/realm-it-suite.json   # must print 1 before you read the result
cd backend && .venv/bin/pytest tests/test_console_auth_realm.py -q -k secret
# Expected: FAIL, both the field test and the regex sweep
cd .. && git checkout deploy/keycloak/realm-it-suite.json
```

- [x] **Step 6: Commit**

```bash
git add deploy/keycloak/realm-it-suite.json deploy/.env.example backend/tests/test_console_auth_realm.py
git commit -m "console-auth: the Microsoft provider, with the secret kept out of git"
```

---

### Task 4: The live-instance runbook

**Files:**
- Modify: `deploy/README.md`
- Modify: `deploy/GO-LIVE.md`

**Interfaces:**
- Consumes: the theme path, realm keys and provider alias from Tasks 1–3.
- Produces: the operator-facing procedure. Task 5 (removing the launcher) is ordered **after** this for the reason in spec §6.

- [x] **Step 1: Add the section to `deploy/README.md`**

After "Step 11 — Prove the platform actually works" and before "Publishing the agent to the console", add:

````markdown
## Branding and extending sign-in

**Realm import runs only against an empty database.** Everything in
`deploy/keycloak/realm-it-suite.json` applied at first boot; editing that file
on a running install changes nothing. These are the same changes, applied to a
live realm through the admin console.

Do them **in order**. Each is visible before the next begins, so a mistake
leaves a working way in.

### 1. The theme

Already mounted (`./keycloak/themes:/opt/keycloak/themes:ro`). On a deployment
that predates it, `docker compose up -d keycloak` picks the mount up.

Admin console → **Realm settings → Themes → Login theme → `itms` → Save**, then:

```sh
docker compose restart keycloak
```

Open `https://itms.toeshee.com/realms/it-suite/account` in a private window.
An unbranded page means the mount is missing — Keycloak falls back silently and
logs nothing.

### 2. Passkeys

Admin console → **Authentication → Policies → WebAuthn Passwordless Policy**:

| Field | Value |
|---|---|
| Relying Party Entity Name | `ITMS` |
| Signature Algorithms | `ES256`, `RS256` |
| Attestation Conveyance Preference | `none` |
| Require Resident Key | `Yes` |
| User Verification Requirement | `required` |

Then **Authentication → Flows → Browser → Duplicate** (name it `browser-passkey`),
add a **WebAuthn Passwordless Authenticator** execution as an *Alternative*
beside the Username/Password form, and bind it as the realm's Browser flow.

**Test it with a real key before binding.** Binding a broken browser flow locks
every account out of the console, including yours — recovery is `kcadm.sh`
inside the container.

`User verification: required` is deliberate: it makes the passkey two factors
on its own (the authenticator plus the PIN or biometric unlocking it), which is
why a passkey sign-in does not additionally prompt for TOTP. Password sign-in
still does.

### 3. Sign in with Microsoft

In Entra: register an application, add the redirect URI
`https://itms.toeshee.com/realms/it-suite/broker/microsoft/endpoint`, and mint a
client secret.

Admin console → **Identity providers → OpenID Connect v1.0**:

| Field | Value |
|---|---|
| Alias | `microsoft` (must match — the redirect URI contains it) |
| Discovery endpoint | `https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration` |
| Client ID | the Entra application id |
| Client Secret | from `.env` (`ENTRA_CLIENT_SECRET`) — **paste it here, never into the realm JSON** |
| Trust Email | **Off** |
| First login flow | `first broker login` |

Then **Mappers → Add mapper → Hardcoded attribute**: attribute `org`, value your
org slug (`ITS_DEFAULT_ORG_SLUG` in `.env`).

**The mapper is not optional, and its absence is masked.** With
`ITS_MULTI_TENANT=false` a federated user with no `org` attribute falls back to
the default org, so sign-in *works* and every Microsoft account silently lands
in one tenant. Verify the claim, not the login:

```sh
# after signing in through Microsoft once, in the browser devtools console
JSON.parse(atob(sessionStorage.getItem(Object.keys(sessionStorage).find(k => k.startsWith('oidc.user'))))
  .access_token.split('.')[1] |> atob).org
```

or decode the access token at jwt.io and confirm an `org` claim is present.

**Trust Email off is deliberate.** With it on, a Microsoft identity claiming an
existing user's email address is linked to that account automatically — whoever
controls that name at the IdP inherits the local account's roles.
````

- [x] **Step 2: Add the ordering warning to `deploy/GO-LIVE.md`**

In the section that covers Keycloak hardening, add:

```markdown
- **Do not remove the console's own sign-in launcher until the Keycloak page is
  known good.** The console redirects unauthenticated visitors straight to
  Keycloak; if the themed page, the browser flow or the identity provider is
  broken, there is no other way in, and the failure looks like a redirect you
  cannot stop. Verify sign-in in a private window first.
```

- [x] **Step 3: Check the runbook still passes its own guards**

Run: `cd backend && .venv/bin/pytest tests/test_deploy_runbook_secrets.py -q`
Expected: PASS — no `sh`-fenced block above assigns a secret on a command line. **If this goes red, the runbook you just wrote leaks a credential into argv and shell history; fix the runbook, not the test.**

- [x] **Step 4: Commit**

```bash
git add deploy/README.md deploy/GO-LIVE.md
git commit -m "console-auth: the live-realm runbook for theme, passkeys and Microsoft"
```

---

### Task 5: Remove the launcher, and make a failed sign-in visible

**Files:**
- Modify: `frontend/src/App.tsx:100-112` (`LoginPage`), `:154-157` (`Protected`), `:170` (the `/login` route)
- Modify: `frontend/src/lib/auth.ts`
- Create: `frontend/src/App.auth.test.tsx`

**Interfaces:**
- Consumes: nothing from Tasks 1–4 at the code level; **ordered last for the operational reason in spec §6.**
- Produces: `signIn()` returning `Promise<void>` unchanged; a new exported `SignInUnavailable` is *not* required — keep it module-private in `App.tsx`.

- [x] **Step 1: Write the failing tests**

`frontend/src/App.auth.test.tsx`:

```tsx
/**
 * What an unauthenticated visitor sees.
 *
 * THE LAUNCHER WAS ACCIDENTALLY COVERING TWO FAILURES. A page with a button
 * renders whether or not Keycloak is reachable; an automatic redirect has
 * nothing to render if discovery fails. That is not hypothetical -- it is the
 * defect that cost a live afternoon on this deployment (`lib/auth.ts`'s
 * header): a login button that did nothing, 0-byte discovery fetches, and no
 * error anywhere.
 *
 * So both of these are required tests, not niceties (spec §5.5, §5.6).
 */

import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const signIn = vi.fn();
vi.mock("./lib/auth", async () => {
  const actual = await vi.importActual<typeof import("./lib/auth")>("./lib/auth");
  return {
    ...actual,
    signIn: () => signIn(),
    isAuthenticated: () => false,
    initAuth: () => Promise.resolve(null),
    completeSignIn: () => Promise.reject(new Error("state mismatch")),
  };
});

// Imported after the mock so the component picks it up.
const { RequireAuth, CallbackPage } = await import("./App");

beforeEach(() => {
  signIn.mockReset();
  signIn.mockResolvedValue(undefined);
});

describe("an unauthenticated visitor", () => {
  it("is sent to Keycloak rather than shown a launcher", async () => {
    render(
      <MemoryRouter>
        <RequireAuth>
          <p>secret</p>
        </RequireAuth>
      </MemoryRouter>,
    );
    await waitFor(() => expect(signIn).toHaveBeenCalledTimes(1));
    expect(screen.queryByText("secret")).not.toBeInTheDocument();
  });

  it("is TOLD when sign-in is unavailable, and which authority failed", async () => {
    signIn.mockRejectedValue(new Error("Failed to fetch openid-configuration"));
    render(
      <MemoryRouter>
        <RequireAuth>
          <p>secret</p>
        </RequireAuth>
      </MemoryRouter>,
    );
    expect(await screen.findByText(/sign-in is unavailable/i)).toBeInTheDocument();
    // The authority is the single most useful fact for diagnosing this, and the
    // one nothing else on a blank page reveals.
    expect(screen.getByText(/realms\/it-suite/)).toBeInTheDocument();
  });

  it("redirects exactly once, not on every render", async () => {
    const { rerender } = render(
      <MemoryRouter>
        <RequireAuth>
          <p>secret</p>
        </RequireAuth>
      </MemoryRouter>,
    );
    await waitFor(() => expect(signIn).toHaveBeenCalledTimes(1));
    rerender(
      <MemoryRouter>
        <RequireAuth>
          <p>secret</p>
        </RequireAuth>
      </MemoryRouter>,
    );
    expect(signIn).toHaveBeenCalledTimes(1);
  });
});

describe("a failed callback", () => {
  it("shows the error instead of re-entering signIn (a loop is indistinguishable from a hang)", async () => {
    render(
      <MemoryRouter initialEntries={["/auth/callback"]}>
        <CallbackPage />
      </MemoryRouter>,
    );
    expect(await screen.findByText(/sign-in failed/i)).toBeInTheDocument();
    expect(signIn).not.toHaveBeenCalled();
  });
});
```

- [x] **Step 2: Run to verify they fail**

Run: `cd frontend && npx vitest run src/App.auth.test.tsx`
Expected: FAIL — `App.tsx` exports neither `RequireAuth` nor `CallbackPage`.

- [x] **Step 3: Implement**

In `frontend/src/lib/auth.ts`, export the resolved authority so the error state can name it:

```ts
/** The authority actually in use, exported so a failure can NAME it. */
export const oidcAuthority = authority;
```

In `frontend/src/App.tsx`: delete `LoginPage` and the `/login` route, export `CallbackPage`, and replace `Protected` with:

```tsx
/**
 * The guard. An unauthenticated visitor goes to Keycloak immediately — there is
 * no ITMS launcher any more (docs/modules/console-auth/spec.md §3.4).
 *
 * `redirecting` is a ref, not state: `signinRedirect()` is a side effect that
 * navigates the whole page, and firing it from a render that runs twice (React
 * strict mode does exactly that) starts two authorization requests, the second
 * of which overwrites the first's PKCE state in sessionStorage. The symptom is
 * a callback that fails with "state mismatch" on a login that looked fine.
 */
export function RequireAuth({ children }: { children: React.ReactNode }) {
  const [error, setError] = useState<string | null>(null);
  const redirecting = useRef(false);
  const authed = isAuthenticated();

  useEffect(() => {
    if (authed || redirecting.current) return;
    redirecting.current = true;
    signIn().catch((err: unknown) => setError(String(err)));
  }, [authed]);

  if (authed) return <Shell>{children}</Shell>;
  if (error) {
    return (
      <div className="flex min-h-screen items-center justify-center p-8">
        <div className="max-w-md rounded-lg border border-[var(--line)] p-8">
          <h1 className="mb-2 text-lg font-semibold">Sign-in is unavailable</h1>
          <p className="mb-4 text-sm text-[var(--muted)]">
            The console could not reach its identity provider at{" "}
            <span className="font-mono">{oidcAuthority}</span>.
          </p>
          <p className="text-xs text-[var(--muted)]">{error}</p>
        </div>
      </div>
    );
  }
  return <p className="p-8 text-[var(--muted)]">Redirecting to sign-in…</p>;
}
```

Replace every `<Protected>` usage with `<RequireAuth>` (a rename — the routes are otherwise untouched), and leave `/auth/callback` outside the guard exactly as it is today.

- [x] **Step 4: Run the tests**

Run: `cd frontend && npx vitest run src/App.auth.test.tsx`
Expected: PASS (4 tests)

- [x] **Step 5: Falsify the error state**

The regression this exists for is a blank page. Prove the test sees it:

```bash
cd frontend
# Drop the .catch, so a failed signIn() produces an unhandled rejection and a
# spinner forever -- precisely the shipped defect.
# Edit App.tsx: `signIn().catch(...)` -> `void signIn()`
npx vitest run src/App.auth.test.tsx -t "unavailable"
# Expected: FAIL. Then restore the .catch.
```

- [x] **Step 6: Whole frontend suite and types**

Run: `cd frontend && npx vitest run && npx tsc --noEmit`
Expected: green. `App.routing.test.tsx` reads `App.tsx` as text and asserts every module folder is imported — a rename that drops an import turns it red, which is the point.

- [x] **Step 7: Commit**

```bash
git add frontend/src/App.tsx frontend/src/lib/auth.ts frontend/src/App.auth.test.tsx
git commit -m "console-auth: redirect straight to Keycloak, and say so when it is unreachable"
```

---

### Task 6: Close the spec

**Files:**
- Modify: `docs/modules/console-auth/spec.md`

- [x] **Step 1: Update the status block**

Replace "Not built" with what is true, in the shape the agent-distribution spec uses: which falsifiers have tests, which do not, and what has never run.

The honest accounting to write:

- **Falsifiers 1, 2 (proxy), 4, 7** — `backend/tests/test_console_auth_realm.py`.
- **Falsifiers 5, 6** — `frontend/src/App.auth.test.tsx`.
- **Falsifier 3** (a federated token carries an `org` claim) — **not tested.** It needs a real Entra tenant and a real login; the committed mapper is asserted, the claim is not. The runbook tells the operator to decode the token, which is a procedure, not a test.
- **Nothing here has rendered in a running Keycloak.** The theme is FreeMarker executed by a Java process this suite does not run; the browser flow is built by hand in the admin console. §5.2's real form of the test needs a live instance.

- [x] **Step 2: Commit**

```bash
git add docs/modules/console-auth/spec.md
git commit -m "console-auth: spec status, with what is not proven named"
```

---

## Verification before claiming completion

- `cd backend && .venv/bin/pytest tests/test_console_auth_realm.py tests/test_deploy_runbook_secrets.py -q` — green
- `cd frontend && npx vitest run && npx tsc --noEmit` — green
- `python3 -c "import json;json.load(open('deploy/keycloak/realm-it-suite.json'))"` — exit 0 (a realm file that does not parse fails at first boot of a *new* deployment, where nobody is watching this suite)
- `git grep -n "clientSecret" deploy/` shows only the empty value and the comments explaining it
- Every falsifier in the spec's §5 either has a test named above **or** is listed in the status block as untested, with the reason
