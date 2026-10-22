# Microsoft OAuth for mail — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator authenticate this platform's outgoing relay and its inbound support mailbox to Microsoft 365 with OAuth instead of a password, without regressing a single password-configured deployment.

**Architecture:** Two independent cycles. **Cycle A+B** upgrades Keycloak 26.0 → 26.2 and teaches the existing realm SMTP surface a second authentication mode — Keycloak does the client-credentials token exchange itself, so this platform stores no new secret. **Cycle C** adds delegated (authorization-code + PKCE) sign-in for IMAP intake: the refresh token becomes a new credential type in the existing envelope vault, the OAuth callback lands on a **console route** (not an API route) so the privileged completion call carries the operator's own bearer token, and the poller picks an authentication path per mailbox.

**Tech Stack:** Python 3.12 · FastAPI · SQLAlchemy 2.x (typed) · Pydantic v2 · Alembic · `httpx` · stdlib `imaplib` · React + TypeScript (strict) · TanStack Query · Vite · Keycloak 26.2 · OpenBao transit.

**Spec:** `docs/superpowers/specs/2026-09-11-mail-oauth-design.md`. Read §3 before Cycle A+B and §4 before Cycle C.

## Global Constraints

- **Keycloak image pin:** `quay.io/keycloak/keycloak:26.2` (26.2 is the floor — XOAUTH2 does not exist below it). Current pin is `quay.io/keycloak/keycloak:26.0` at `deploy/docker-compose.yml:491`.
- **The realm's own SMTP key names, exactly:** `authType`, `authTokenUrl`, `authTokenScope`, `authTokenClientId`, `authTokenClientSecret`. `authType` takes `password` or `token`.
- **Microsoft host, pinned in code, never configurable:** `https://login.microsoftonline.com`.
- **Delegated scope string, exactly:** `https://outlook.office.com/IMAP.AccessAsUser.All offline_access`.
- **Redirect URI shape:** `{ITS_PUBLIC_CONSOLE_URL}/settings/email-intake/oauth/callback`. Built from config. **Never** from `Host` or `X-Forwarded-Host`.
- **`state` length:** `secrets.token_urlsafe(32)`. **Authorization TTL:** 10 minutes.
- **PKCE:** S256 only. `code_challenge = base64url(sha256(verifier)).rstrip("=")`.
- **Permissions, all existing — no new permission codes and no permission migration:** outgoing = `orgs.manage`; incoming = `email_intake.read` for reads, `email_intake.admin` for every write.
- **Audit actions, exact strings:** `email.settings_updated` (existing), `email_intake.oauth_app_configured`, `email_intake.oauth_started`, `email_intake.oauth_connected`, `email_intake.oauth_abandoned`, `email_intake.oauth_disconnected`, `email_intake.oauth_revoked`, `email_intake.credential_used` (existing).
- **Credential types:** `email_mailbox` (existing, password), `email_mailbox_oauth` (refresh token), `email_oauth_client` (Azure client secret).
- **Invariant 3:** no secret is ever returned by a response model, written to a log, put in audit `metadata`, or stored outside `credentials.ciphertext`. Tests scan **raw response bytes** for the literal, not for the absence of a key.
- **Invariant 5:** every new table is `ENABLE`d + `FORCE`d RLS with a `tenant_isolation` policy using `current_setting('app.current_org', true)::uuid` in both `USING` and `WITH CHECK`.
- **Migrations are forward-only.** New revision `0083`, `down_revision = "0082"`.
- **Backend commands:** `.venv/bin/pytest`, `.venv/bin/mypy app`, `.venv/bin/ruff check app tests` from `backend/`. **Frontend:** `npx vitest run`, `npm run typecheck` from `frontend/`.
- **Four other agents are editing `frontend/src/modules/settings/` right now.** Re-read any settings file immediately before editing it; anchor edits on the `FieldGroup title="Authentication"` block rather than on line numbers.

---

# CYCLE A+B — Keycloak upgrade + outgoing OAuth

Ships and deploys on its own. Cycle C does not start until this is on `main`.

---

### Task 1: Pin Keycloak 26.2, and make the floor a test

**Files:**
- Modify: `deploy/docker-compose.yml` (the `keycloak` service `image:` line)
- Create: `backend/tests/test_keycloak_image_pin.py`

**Interfaces:**
- Produces: nothing importable. The compose pin is read by Task 5's CI smoke and by the deploy sequence in Appendix A.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_keycloak_image_pin.py`:

```python
"""The pinned Keycloak minor is a FEATURE FLOOR, not a preference.

SMTP XOAUTH2 (`authType: token`) arrived in Keycloak 26.2. Below it the five
token keys this platform now writes into `smtpServer` are accepted by the admin
API and then ignored by the mail sender -- the realm saves, the console reports
success, and every invitation silently fails to send. There is no error to read.

So the floor is asserted here rather than left in a comment: a well-meaning
"pin back to the version we know" is exactly the change that would reintroduce
it, and this test is what stops that being a silent regression.
"""

import re
from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "docker-compose.yml"

#: SMTP XOAUTH2 landed in this minor. See
#: docs/superpowers/specs/2026-09-10-microsoft-oauth-findings.md.
MINIMUM = (26, 2)


def test_the_pinned_keycloak_supports_smtp_xoauth2() -> None:
    text = COMPOSE.read_text()
    match = re.search(r"image:\s*quay\.io/keycloak/keycloak:(\d+)\.(\d+)", text)
    assert match is not None, "the keycloak service must pin an explicit x.y tag"
    pinned = (int(match.group(1)), int(match.group(2)))
    assert pinned >= MINIMUM, (
        f"keycloak is pinned at {pinned[0]}.{pinned[1]}; SMTP XOAUTH2 needs "
        f"{MINIMUM[0]}.{MINIMUM[1]} or later and fails SILENTLY below it"
    )


def test_the_pin_is_not_a_moving_tag() -> None:
    """`latest`, `26`, or no tag would let an unattended `docker compose pull`
    change the identity provider under a running fleet."""
    text = COMPOSE.read_text()
    assert "quay.io/keycloak/keycloak:latest" not in text
    assert re.search(r"image:\s*quay\.io/keycloak/keycloak:\d+\.\d+", text) is not None
```

- [ ] **Step 2: Run it, watch it fail**

Run: `cd backend && .venv/bin/pytest tests/test_keycloak_image_pin.py -v`
Expected: `test_the_pinned_keycloak_supports_smtp_xoauth2` FAILS with "keycloak is pinned at 26.0".

- [ ] **Step 3: Move the pin**

In `deploy/docker-compose.yml`, in the `keycloak` service, replace the image line and add the note above it:

```yaml
  keycloak:
    # PINNED AT 26.2 AS A FEATURE FLOOR, not a preference. SMTP XOAUTH2
    # (`authType: token` in the realm's smtpServer block) arrived in 26.2;
    # below it the five token keys are stored and then ignored, so every
    # invitation silently fails to send with nothing in any log.
    # `backend/tests/test_keycloak_image_pin.py` holds this to a test.
    #
    # UPGRADING THIS IS ONE-WAY. Keycloak migrates its own schema in the
    # `keycloak` database on first start and does not migrate back, so
    # re-pinning to an older tag produces a container that refuses to start.
    # Rollback is restore-from-backup -- see
    # docs/superpowers/specs/2026-09-11-mail-oauth-design.md §7.2.
    image: quay.io/keycloak/keycloak:26.2
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && .venv/bin/pytest tests/test_keycloak_image_pin.py -v`
Expected: 2 passed.

- [ ] **Step 5: Falsify**

Temporarily set the tag to `26.1`. Expected: RED on the first test. Restore `26.2`.

- [ ] **Step 6: Commit**

```bash
git add deploy/docker-compose.yml backend/tests/test_keycloak_image_pin.py
git commit -m "deploy: pin Keycloak 26.2, the floor for SMTP XOAUTH2"
```

---

### Task 2: The realm's five token keys reach the realm, and no further

**Files:**
- Modify: `backend/app/modules/user_admin/realm_config.py`
- Test: `backend/tests/test_realm_config.py`

**Interfaces:**
- Produces: `SMTP_FIELDS` grows to sixteen entries including `authType`, `authTokenUrl`, `authTokenScope`, `authTokenClientId`, `authTokenClientSecret`; and a new `CARRIED_SECRETS: tuple[str, ...] = ("password", "authTokenClientSecret")`. `set_smtp(config, values)` keeps its signature. Task 3 consumes both names.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_realm_config.py`. Use the module's **existing** fake-realm fixture, install helper and config constant — do not add a second set; the names below (`_FakeRealm`, `_install`, `_CONFIG`) are placeholders for whatever that file already calls them.

```python
from app.modules.user_admin.realm_config import SMTP_FIELDS, get_smtp, set_smtp

TOKEN_KEYS = (
    "authType",
    "authTokenUrl",
    "authTokenScope",
    "authTokenClientId",
    "authTokenClientSecret",
)


def test_the_five_token_keys_are_allowlisted() -> None:
    for key in TOKEN_KEYS:
        assert key in SMTP_FIELDS


@pytest.mark.asyncio
async def test_token_mode_writes_all_five_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    realm = _FakeRealm()
    _install(monkeypatch, realm)
    await set_smtp(
        _CONFIG,
        {
            "host": "smtp.office365.com",
            "port": "587",
            "from": "itms@toeshee.com",
            "starttls": "true",
            "auth": "true",
            "user": "itms@toeshee.com",
            "authType": "token",
            "authTokenUrl": "https://login.microsoftonline.com/t/oauth2/v2.0/token",
            "authTokenScope": "https://outlook.office365.com/.default",
            "authTokenClientId": "client-abc",
            "authTokenClientSecret": "s3cr3t",
        },
    )
    block = realm.realm["smtpServer"]
    assert block["authType"] == "token"
    assert block["authTokenClientId"] == "client-abc"
    assert block["authTokenClientSecret"] == "s3cr3t"


@pytest.mark.asyncio
async def test_an_absent_token_secret_keeps_the_stored_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same rule as `password`, for the same reason: the console cannot send
    back a value it was never given, so an absent field must not clear a working
    credential when the operator corrects the scope."""
    realm = _FakeRealm()
    realm.realm["smtpServer"] = {
        "host": "smtp.office365.com",
        "authType": "token",
        "authTokenClientSecret": "**********",
    }
    _install(monkeypatch, realm)
    await set_smtp(_CONFIG, {"host": "smtp.office365.com", "authType": "token"})
    assert realm.realm["smtpServer"]["authTokenClientSecret"] == "**********"


@pytest.mark.asyncio
async def test_get_smtp_never_returns_the_token_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    realm = _FakeRealm()
    realm.realm["smtpServer"] = {"host": "h", "authTokenClientSecret": "**********"}
    _install(monkeypatch, realm)
    values = await get_smtp(_CONFIG)
    assert "authTokenClientSecret" not in values


@pytest.mark.asyncio
async def test_a_key_outside_the_allowlist_cannot_reach_the_realm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    realm = _FakeRealm()
    _install(monkeypatch, realm)
    await set_smtp(_CONFIG, {"host": "h", "authTokenUrlEvil": "https://attacker.example"})
    assert "authTokenUrlEvil" not in realm.realm["smtpServer"]
```

- [ ] **Step 2: Run it, watch it fail**

Run: `cd backend && .venv/bin/pytest tests/test_realm_config.py -v`
Expected: FAIL — the five keys are not in `SMTP_FIELDS`, so `set_smtp` drops them.

- [ ] **Step 3: Implement**

In `backend/app/modules/user_admin/realm_config.py`, replace `SMTP_FIELDS` with:

```python
#: The realm's own key names for the SMTP block, in the order the form shows
#: them. Named here so the API layer maps ITMS field names onto vendor ones in
#: exactly one place, and so a key that is not one of these cannot be smuggled
#: into the realm by a request body.
#:
#: THE LAST FIVE ARE KEYCLOAK 26.2's TOKEN-BASED SMTP AUTH (XOAUTH2). Keycloak
#: itself performs the client-credentials exchange against `authTokenUrl` and
#: sends the resulting bearer token on the SMTP connection -- this platform
#: neither obtains nor holds that token, because this platform does not open
#: that connection. See the design's §3.1 for why there was no other choice.
SMTP_FIELDS: tuple[str, ...] = (
    "host",
    "port",
    "from",
    "fromDisplayName",
    "replyTo",
    "envelopeFrom",
    "ssl",
    "starttls",
    "auth",
    "user",
    "password",
    "authType",
    "authTokenUrl",
    "authTokenScope",
    "authTokenClientId",
    "authTokenClientSecret",
)

#: Every key whose value the realm MASKS on read, and for which an absent key in
#: a write therefore means "leave it alone". Both are credentials; both travel
#: once, inbound; neither is ever stored, logged or returned by this platform.
CARRIED_SECRETS: tuple[str, ...] = ("password", "authTokenClientSecret")
```

Add `"CARRIED_SECRETS",` to `__all__`.

In `get_smtp`, replace the single `values.pop("password", None)` with:

```python
    values = _stored_smtp(realm)
    for key in CARRIED_SECRETS:
        # Stripped whatever came back, mask or not. This platform does not get
        # to rely on the vendor stripping it (invariant 3).
        values.pop(key, None)
    return values
```

In `set_smtp`, replace the single-key carry-over block with:

```python
        block = {key: value for key, value in values.items() if key in SMTP_FIELDS}
        stored = _stored_smtp(realm)
        for key in CARRIED_SECRETS:
            if key not in block:
                held = stored.get(key)
                if held is not None:
                    # The value the GET gave us, sent back unchanged. On a live
                    # realm that is the mask, which the identity provider reads
                    # as "unchanged"; there is no other value here, because the
                    # real one was never disclosed to this process.
                    block[key] = held
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && .venv/bin/pytest tests/test_realm_config.py -v && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: PASS, clean.

- [ ] **Step 5: Falsify**

Remove `authTokenClientSecret` from `CARRIED_SECRETS`. Expected: `test_an_absent_token_secret_keeps_the_stored_one` RED. Restore.

- [ ] **Step 6: Commit**

```bash
git add backend/app/modules/user_admin/realm_config.py backend/tests/test_realm_config.py
git commit -m "realm: carry the five XOAUTH2 keys, and mask the token secret like the password"
```

---

### Task 3: `PUT /settings/email` takes an authentication mode

**Files:**
- Modify: `backend/app/api/v1/realm_settings.py`
- Test: `backend/tests/test_realm_settings_api.py`

**Interfaces:**
- Consumes: `SMTP_FIELDS`, `CARRIED_SECRETS` from Task 2.
- Produces: `EmailSettingsIn` gains `auth_type: Literal["password","token"] = "password"`, `token_url: str = ""`, `token_scope: str = ""`, `token_client_id: str = ""`, `token_client_secret: str | None = None`. `EmailSettingsOut` gains `auth_type: str` and `token_client_id: str`. Task 4's TypeScript mirrors these names exactly.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_realm_settings_api.py`, using its existing `email_env`, `VALID_BODY`, `TYPED` and `admin_engine` names:

```python
TOKEN_BODY: dict[str, Any] = {
    "host": "smtp.office365.com",
    "port": 587,
    "from_address": "itms@toeshee.com",
    "from_display_name": "ITMS",
    "reply_to": "",
    "encryption": "starttls",
    "username": "itms@toeshee.com",
    "auth_type": "token",
    "token_url": "https://login.microsoftonline.com/tid/oauth2/v2.0/token",
    "token_scope": "https://outlook.office365.com/.default",
    "token_client_id": "client-abc",
    "token_client_secret": "s3cr3t-value",
}


@pytest.mark.asyncio
async def test_password_mode_writes_the_block_a_password_deployment_needs(
    email_env: Any,
) -> None:
    """THE COMPATIBILITY GUARANTEE, asserted as a whole-dict comparison rather
    than an inspection.

    The guarantee is that a password deployment's BEHAVIOUR is unchanged, not
    that the dict is textually unchanged. `authType: "password"` is Keycloak's
    own default, and the four empty token keys are what makes switching BACK
    from OAuth actually clear the old application -- a key merely omitted would
    be carried by `set_smtp`'s merge and leave a stale client id behind.
    """
    response = await email_env.client.put("/api/v1/settings/email", json=VALID_BODY)
    assert response.status_code == 200
    assert email_env.realm.realm["smtpServer"] == {
        "host": "smtp.new.internal",
        "port": "465",
        "from": "itms@toeshee.com",
        "fromDisplayName": "ITMS",
        "replyTo": "support@toeshee.com",
        "ssl": "true",
        "auth": "true",
        "user": "itms",
        "password": TYPED,
        "authType": "password",
        "authTokenUrl": "",
        "authTokenScope": "",
        "authTokenClientId": "",
        "authTokenClientSecret": "",
    }


@pytest.mark.asyncio
async def test_token_mode_writes_the_token_keys_and_clears_the_password(
    email_env: Any,
) -> None:
    response = await email_env.client.put("/api/v1/settings/email", json=TOKEN_BODY)
    assert response.status_code == 200
    block = email_env.realm.realm["smtpServer"]
    assert block["authType"] == "token"
    assert block["authTokenClientId"] == "client-abc"
    # A realm carrying BOTH leaves the relay to decide what was meant.
    assert block["password"] == ""


@pytest.mark.asyncio
async def test_switching_back_to_password_clears_the_token_keys(
    email_env: Any,
) -> None:
    await email_env.client.put("/api/v1/settings/email", json=TOKEN_BODY)
    await email_env.client.put("/api/v1/settings/email", json=VALID_BODY)
    block = email_env.realm.realm["smtpServer"]
    for key in ("authTokenUrl", "authTokenScope", "authTokenClientId"):
        assert block[key] == ""
    assert block["authType"] == "password"


@pytest.mark.asyncio
async def test_a_token_secret_without_a_client_id_is_refused(email_env: Any) -> None:
    body = {**TOKEN_BODY, "token_client_id": ""}
    response = await email_env.client.put("/api/v1/settings/email", json=body)
    assert response.status_code == 422
    assert "client id" in response.json()["detail"].lower()
    # The refusal names the rule and never repeats the value.
    assert b"s3cr3t-value" not in response.content


@pytest.mark.asyncio
async def test_no_route_ever_returns_the_token_secret(email_env: Any) -> None:
    """RAW BYTES, not a key check: a renamed field satisfies the weaker
    assertion while still shipping the value."""
    put = await email_env.client.put("/api/v1/settings/email", json=TOKEN_BODY)
    get = await email_env.client.get("/api/v1/settings/email")
    for response in (put, get):
        assert b"s3cr3t-value" not in response.content


@pytest.mark.asyncio
async def test_the_audit_row_records_field_names_and_no_values(
    email_env: Any, admin_engine: AsyncEngine
) -> None:
    await email_env.client.put("/api/v1/settings/email", json=TOKEN_BODY)
    async with AsyncSession(admin_engine) as session:
        rows = (await session.execute(select(AuditEvent))).scalars().all()
    event = next(r for r in rows if r.action == "email.settings_updated")
    assert "token_client_secret" in event.event_metadata["fields"]
    assert "s3cr3t-value" not in json.dumps(event.event_metadata)
```

(Use the file's own spelling for the audit metadata attribute if it is not `event_metadata`.)

- [ ] **Step 2: Run it, watch it fail**

Run: `cd backend && .venv/bin/pytest tests/test_realm_settings_api.py -v`
Expected: FAIL — `extra="forbid"` rejects `auth_type` with 422.

- [ ] **Step 3: Implement**

In `backend/app/api/v1/realm_settings.py`, add beside `MAX_PASSWORD`:

```python
#: Same bound and same reasoning as MAX_PASSWORD: long enough for any Azure
#: client secret, short enough that the body is bounded.
MAX_CLIENT_SECRET = 512
MAX_TOKEN_URL = 2048
```

Add to `EmailSettingsOut`, after `authenticated`:

```python
    auth_type: str = Field(
        description=(
            "`password` or `token`. `token` means the identity provider signs "
            "in to the relay with OAuth (Microsoft), obtaining the token itself "
            "-- this platform neither holds nor sees it."
        )
    )
    token_client_id: str = Field(
        description=(
            "The OAuth application the relay sign-in uses. The client SECRET is "
            "never returned by this API and has no field here that could carry it."
        )
    )
```

Add to `EmailSettingsIn`, after `password`:

```python
    auth_type: Literal["password", "token"] = Field(
        default="password",
        description=(
            "DEFAULTS TO `password`, which is what every existing deployment "
            "sends and what keeps their realm write unchanged in behaviour."
        ),
    )
    token_url: str = Field(default="", max_length=MAX_TOKEN_URL)
    token_scope: str = Field(default="", max_length=500)
    token_client_id: str = Field(default="", max_length=200)
    # NO `max_length` HERE, for the same reason `password` has none: a
    # field-level constraint failure is reported by FastAPI with the offending
    # INPUT echoed into the 422 body. Bounded in `_refuse_unusable` instead.
    token_client_secret: str | None = Field(
        default=None,
        description=(
            "Sent straight to the identity provider and never stored, logged or "
            "returned by this platform. OMIT IT to keep the one already saved."
        ),
    )
```

Extend `_refuse_unusable` with the mirror rules:

```python
    if body.auth_type == "token":
        if not body.token_client_id:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "a Microsoft sign-in needs an application (client id); choose "
                "Password instead for a relay that signs in with one",
            )
        if not body.token_url:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "a Microsoft sign-in needs a token URL",
            )
    if body.token_client_secret is not None:
        if not body.token_client_id:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "a client secret needs an application (client id)",
            )
        if len(body.token_client_secret) > MAX_CLIENT_SECRET:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"the client secret may be at most {MAX_CLIENT_SECRET} characters",
            )
```

Extend `_as_settings`'s return with two more keyword arguments:

```python
        username=values.get("user", ""),
        authenticated=values.get("auth") == "true",
        auth_type="token" if values.get("authType") == "token" else "password",
        token_client_id=values.get("authTokenClientId", ""),
    )
```

Replace the authentication branch of `_as_realm_block` with three explicit branches:

```python
    if not body.username:
        # NO USERNAME MEANS NO SIGN-IN, so every stored credential is CLEARED
        # rather than carried -- a credential nothing uses is a credential
        # nobody rotates.
        block["password"] = ""
        block["authType"] = "password"
        block["authTokenUrl"] = ""
        block["authTokenScope"] = ""
        block["authTokenClientId"] = ""
        block["authTokenClientSecret"] = ""
        return block

    block["auth"] = "true"
    block["user"] = body.username

    if body.auth_type == "token":
        block["authType"] = "token"
        block["authTokenUrl"] = body.token_url
        block["authTokenScope"] = body.token_scope
        block["authTokenClientId"] = body.token_client_id
        if body.token_client_secret is not None:
            block["authTokenClientSecret"] = body.token_client_secret
        # A realm carrying BOTH a password and a token client leaves the relay
        # to decide what was meant -- the same rule as ssl/starttls.
        block["password"] = ""
        return block

    block["authType"] = "password"
    block["authTokenUrl"] = ""
    block["authTokenScope"] = ""
    block["authTokenClientId"] = ""
    block["authTokenClientSecret"] = ""
    if body.password is not None:
        block["password"] = body.password
    return block
```

The route handlers themselves are unchanged: the permission, the `org_session`, the audit record of `sorted(body.model_fields_set)`, and the fresh re-read all still do exactly what they did.

- [ ] **Step 4: Run the tests**

Run: `cd backend && .venv/bin/pytest tests/test_realm_settings_api.py -v && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: PASS, clean.

- [ ] **Step 5: Falsify**

Delete `block["password"] = ""` from the token branch. Expected: `test_token_mode_writes_the_token_keys_and_clears_the_password` RED. Restore.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/realm_settings.py backend/tests/test_realm_settings_api.py
git commit -m "settings/email: an authentication mode, with the token secret held to the password's rule"
```

---

### Task 4: The Email tab offers Password or Microsoft

**Files:**
- Modify: `frontend/src/modules/settings/types.ts` (`EmailSettingsOut`, `EmailSettingsUpdate`)
- Modify: `frontend/src/modules/settings/EmailTab.tsx` (the `EmailForm` interface, `seedFrom`, `submit`, the completeness guard, and the `FieldGroup title="Authentication"` block)
- Modify: `frontend/src/modules/settings/fixtures.ts` (the email settings fixture)
- Test: `frontend/src/modules/settings/__tests__/email.test.tsx` — **it already exists and another agent is editing it**; append a new `describe` block rather than rewriting the file

**Interfaces:**
- Consumes: the wire names from Task 3 exactly — `auth_type`, `token_url`, `token_scope`, `token_client_id`, `token_client_secret`.
- Produces: nothing later tasks consume.

> **Re-read every file in this task immediately before editing it.** Four agents are editing this folder concurrently. Anchor on `FieldGroup title="Authentication"`, never on line numbers.

- [ ] **Step 1: Write the failing test**

```tsx
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { renderSettings } from "./harness";

describe("Email tab — authentication mode", () => {
  it("offers Microsoft and hides the password box when it is chosen", async () => {
    renderSettings({
      email: { configured: true, host: "smtp.office365.com", auth_type: "password" },
    });
    await userEvent.click(await screen.findByRole("button", { name: /Microsoft/i }));
    expect(screen.queryByLabelText(/^Password$/i)).not.toBeInTheDocument();
    expect(screen.getByLabelText(/Application \(client id\)/i)).toBeInTheDocument();
  });

  it("says the password will not be kept, before the save", async () => {
    renderSettings({
      email: { configured: true, host: "h", authenticated: true, auth_type: "password" },
    });
    await userEvent.click(await screen.findByRole("button", { name: /Microsoft/i }));
    expect(
      screen.getByText(/replace the stored relay password with a Microsoft sign-in/i),
    ).toBeInTheDocument();
  });

  it("sends auth_type and omits an untouched client secret", async () => {
    const { requests } = renderSettings({
      email: {
        configured: true,
        host: "h",
        from_address: "a@b.c",
        port: "587",
        username: "u",
        auth_type: "token",
        token_client_id: "abc",
      },
    });
    await userEvent.type(await screen.findByLabelText(/Token URL/i), "https://x/token");
    await userEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() => expect(requests.at(-1)?.body).toBeDefined());
    const body = requests.at(-1)!.body as Record<string, unknown>;
    expect(body.auth_type).toBe("token");
    // THE OMISSION IS THE FEATURE, exactly as it is for the password.
    expect("token_client_secret" in body).toBe(false);
  });
});
```

Use the harness's own render signature and request-capture shape; do not add a second harness.

- [ ] **Step 2: Run it, watch it fail**

Run: `cd frontend && npx vitest run src/modules/settings`
Expected: FAIL — there is no Microsoft control.

- [ ] **Step 3: Implement the types**

In `frontend/src/modules/settings/types.ts`, add to `EmailSettingsOut`:

```ts
  /** `password` or `token`. `token` means the identity provider signs in with OAuth. */
  auth_type: string;
  /**
   * The OAuth application the relay sign-in uses. The client SECRET is never
   * returned — there is no field here that could carry it, which is structural
   * rather than careful.
   */
  token_client_id: string;
```

and to `EmailSettingsUpdate`:

```ts
  auth_type?: "password" | "token";
  token_url?: string;
  token_scope?: string;
  token_client_id?: string;
  /** Only ever present when the operator typed one. Absent means "keep the stored one". */
  token_client_secret?: string;
```

- [ ] **Step 4: Implement the tab**

In `EmailTab.tsx`, extend the form type and add the two constants near `ENCRYPTION_CHOICES`:

```ts
type EmailAuthType = "password" | "token";

interface EmailForm {
  host: string;
  port: string;
  fromAddress: string;
  fromDisplayName: string;
  replyTo: string;
  encryption: EmailEncryption;
  username: string;
  /** Always starts empty. What the operator types here, and only that, is sent. */
  password: string;
  authType: EmailAuthType;
  tokenUrl: string;
  tokenScope: string;
  tokenClientId: string;
  /** Always starts empty, for the same reason `password` does. */
  tokenClientSecret: string;
}

const AUTH_CHOICES: readonly { value: EmailAuthType; label: string }[] = [
  { value: "password", label: "Password" },
  { value: "token", label: "Microsoft (OAuth)" },
];

/**
 * Microsoft's own value for a client-credentials SMTP grant. Offered as a
 * DEFAULT rather than fixed: the realm field is free-form, this platform does
 * not dial it, and a tenant with a different cloud instance would need to
 * change it.
 */
const DEFAULT_TOKEN_SCOPE = "https://outlook.office365.com/.default";
```

In `seedFrom`, add the five new keys:

```ts
    authType: data?.auth_type === "token" ? "token" : "password",
    tokenUrl: "",
    tokenScope: DEFAULT_TOKEN_SCOPE,
    tokenClientId: data?.token_client_id ?? "",
    tokenClientSecret: "",
```

Replace the whole `FieldGroup title="Authentication"` block with:

```tsx
          <FieldGroup title="Authentication" icon="key">
            {editable ? (
              // A CHOICE OF TWO, drawn the way this page draws every short
              // fixed enumeration. A plain `.field` div, NOT `Field`'s
              // <label>: a chip button nested in a <label> inherits that
              // label's text as its own accessible name.
              <div className="field">
                <span>Sign-in</span>
                <Chips
                  label="Sign-in"
                  items={AUTH_CHOICES.map((choice) => choice.label)}
                  active={AUTH_CHOICES.findIndex((c) => c.value === form.authType)}
                  onSelect={(at) => {
                    const choice = AUTH_CHOICES[at];
                    if (choice) set("authType", choice.value);
                  }}
                />
              </div>
            ) : null}
            <div className="form-grid" style={FOLDING_FORM_GRID}>
              <Field label="Username">
                <input
                  className={inputClass}
                  value={form.username}
                  disabled={!editable}
                  onChange={(event) => set("username", event.target.value)}
                  placeholder="Leave blank if the relay needs no sign-in"
                />
              </Field>
              {form.authType === "password" ? (
                <Field
                  label="Password"
                  help={
                    settings.data?.authenticated
                      ? "Password: set. Leave the box blank to keep it. This platform never stores it and cannot show it to you."
                      : "Password: not set. It is passed straight to the identity provider and never stored by this platform."
                  }
                >
                  <input
                    className={inputClass}
                    type="password"
                    value={form.password}
                    disabled={!editable}
                    autoComplete="new-password"
                    onChange={(event) => set("password", event.target.value)}
                  />
                </Field>
              ) : (
                <>
                  <Field
                    label="Token URL"
                    help="Microsoft: https://login.microsoftonline.com/<directory id>/oauth2/v2.0/token"
                  >
                    <input
                      className={inputClass}
                      value={form.tokenUrl}
                      disabled={!editable}
                      onChange={(event) => set("tokenUrl", event.target.value)}
                    />
                  </Field>
                  <Field label="Scope">
                    <input
                      className={inputClass}
                      value={form.tokenScope}
                      disabled={!editable}
                      onChange={(event) => set("tokenScope", event.target.value)}
                    />
                  </Field>
                  <Field label="Application (client id)">
                    <input
                      className={inputClass}
                      value={form.tokenClientId}
                      disabled={!editable}
                      onChange={(event) => set("tokenClientId", event.target.value)}
                    />
                  </Field>
                  <Field
                    label="Client secret"
                    help={
                      settings.data?.auth_type === "token"
                        ? "Client secret: set. Leave the box blank to keep it. This platform never stores it and cannot show it to you."
                        : "Passed straight to the identity provider and never stored by this platform."
                    }
                  >
                    <input
                      className={inputClass}
                      type="password"
                      value={form.tokenClientSecret}
                      disabled={!editable}
                      autoComplete="new-password"
                      onChange={(event) => set("tokenClientSecret", event.target.value)}
                    />
                  </Field>
                </>
              )}
            </div>
            {form.authType === "token" && settings.data?.auth_type !== "token" ? (
              // SAID BEFORE THE SAVE, not reported after it. The realm holds
              // ONE credential for this relay; choosing Microsoft replaces it.
              <p role="status" className="notice gold" style={{ marginTop: 10 }}>
                <Icon name={KIND_ICON.warn} size={16} />
                <span>
                  Saving will replace the stored relay password with a Microsoft sign-in.
                  The password is not kept.
                </span>
              </p>
            ) : null}
          </FieldGroup>
```

Replace the body construction inside `submit`:

```ts
    const body: EmailSettingsUpdate = {
      host: form.host.trim(),
      port: port as number,
      from_address: form.fromAddress.trim(),
      from_display_name: form.fromDisplayName.trim(),
      reply_to: form.replyTo.trim(),
      encryption: form.encryption,
      username: form.username.trim(),
      auth_type: form.authType,
    };
    if (form.authType === "token") {
      body.token_url = form.tokenUrl.trim();
      body.token_scope = form.tokenScope.trim();
      body.token_client_id = form.tokenClientId.trim();
      // THE OMISSION IS THE FEATURE. Only a secret the operator actually typed
      // is sent; anything else would overwrite the stored one.
      if (form.tokenClientSecret !== "") body.token_client_secret = form.tokenClientSecret;
    } else if (form.password !== "") {
      body.password = form.password;
    }
    update.mutate(body, {
      onSuccess: () =>
        setForm((current) => ({ ...current, password: "", tokenClientSecret: "" })),
    });
```

Extend the completeness guard so a half-filled Microsoft form cannot be saved:

```ts
  const tokenIncomplete =
    form.authType === "token" &&
    (form.tokenClientId.trim() === "" || form.tokenUrl.trim() === "");
  const complete =
    form.host.trim() !== "" &&
    form.fromAddress.trim() !== "" &&
    portValid &&
    !passwordWithoutUsername &&
    !tokenIncomplete;
```

Finally add `auth_type: "password"` and `token_client_id: ""` to the email fixture in `fixtures.ts`, so the existing tests keep type-checking.

- [ ] **Step 5: Run the tests**

Run: `cd frontend && npx vitest run src/modules/settings && npm run typecheck`
Expected: PASS, no type errors.

- [ ] **Step 6: Falsify**

Send `token_client_secret: form.tokenClientSecret` unconditionally. Expected: the third test RED. Restore.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/modules/settings
git commit -m "settings/email: choose Password or Microsoft, and say what the switch costs"
```

---

### Task 5: The upgrade smoke, the acceptance checklist, and the merge

**Files:**
- Modify: `.github/workflows/ci.yml` (the `deploy` job)
- Modify: `deploy/README.md` (append the acceptance checklist)

**Interfaces:**
- Consumes: the 26.2 pin from Task 1 and the realm keys from Task 2.
- Produces: nothing importable.

- [ ] **Step 1: Add the CI step**

In `.github/workflows/ci.yml`, inside the `deploy` job and after the step that boots the compose stack, add:

```yaml
      - name: keycloak 26.2 — the realm still parses, and takes the token fields
        working-directory: deploy
        run: |
          set -euo pipefail
          # WHAT THIS IS FOR. The XOAUTH2 fields were read about in Keycloak's
          # release notes; nothing in this repository had ever seen the running
          # server accept them. This is the only check in the tree that proves
          # 26.2 stores what `realm_config.py` sends -- see
          # docs/superpowers/specs/2026-09-11-mail-oauth-design.md §6.1 item 10.
          for _ in $(seq 1 60); do
            docker compose exec -T keycloak /opt/keycloak/bin/kcadm.sh config credentials \
              --server http://localhost:8080 --realm master \
              --user "$KEYCLOAK_ADMIN_USER" --password "$KEYCLOAK_ADMIN_PASSWORD" && break
            sleep 5
          done
          # The login theme survived the upgrade. It is a bind mount, and a
          # silent fallback to Keycloak's own theme has no log line at all.
          docker compose exec -T keycloak /opt/keycloak/bin/kcadm.sh get realms/it-suite \
            --fields loginTheme | grep -q '"itms"'
          # The five token keys round-trip.
          docker compose exec -T keycloak /opt/keycloak/bin/kcadm.sh update realms/it-suite \
            -s 'smtpServer.host=smtp.office365.com' \
            -s 'smtpServer.port=587' \
            -s 'smtpServer.from=ci@example.invalid' \
            -s 'smtpServer.auth=true' \
            -s 'smtpServer.user=ci@example.invalid' \
            -s 'smtpServer.authType=token' \
            -s 'smtpServer.authTokenUrl=https://login.microsoftonline.com/t/oauth2/v2.0/token' \
            -s 'smtpServer.authTokenScope=https://outlook.office365.com/.default' \
            -s 'smtpServer.authTokenClientId=ci-client' \
            -s 'smtpServer.authTokenClientSecret=ci-secret'
          docker compose exec -T keycloak /opt/keycloak/bin/kcadm.sh get realms/it-suite \
            --fields smtpServer > /tmp/smtp.json
          grep -q '"authType"' /tmp/smtp.json
          grep -q '"authTokenClientId"' /tmp/smtp.json
          # And the secret is MASKED on read, which is the assumption
          # `set_smtp`'s carry-over rests on.
          grep -q '"authTokenClientSecret" *: *"\*' /tmp/smtp.json
```

- [ ] **Step 2: Run it**

Push the branch and read the `deploy` job. Expected: the step passes.

**If the mask assertion fails, that is a finding, not a flake.** `set_smtp`'s carry-over for `authTokenClientSecret` depends on the realm masking that field on read. Record what it actually returns, add it to the spec's §8 as a new open question, and do **not** delete the assertion — change it to pin the real behaviour and revisit the carry-over rule.

- [ ] **Step 3: Write the acceptance checklist**

Append to `deploy/README.md`:

```markdown
## Microsoft OAuth for the outgoing relay — acceptance (run once, against the real tenant)

⚠ **UNVERIFIED against a live Microsoft tenant.** Everything in this repository
about the Microsoft side is tested against a fake. These four steps are what
turn it into a verified claim; until they have been run, the feature is
"shipped, not verified" — the same standard `realm_config.py` holds itself to.

1. In Azure, register (or reuse) an application, grant it the application
   permission for SMTP send, and have a tenant admin consent. Create a client
   secret.
2. Settings → Email → Sign-in: **Microsoft (OAuth)**. Token URL
   `https://login.microsoftonline.com/<directory id>/oauth2/v2.0/token`, scope
   `https://outlook.office365.com/.default`, the application id, the secret.
   Host `smtp.office365.com`, port `587`, StartTLS, username = the sending
   mailbox. Save.
3. Press **Send a test message** and confirm it arrives. *Accepted is not
   delivered* — look in the inbox, not at the button.
4. Invite a test user and confirm the invitation arrives.

If step 3 fails with an identity-provider 5xx, check first that the sending
mailbox has SMTP AUTH enabled and that the application access policy, if the
tenant uses one, includes that mailbox.
```

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml deploy/README.md
git commit -m "ci+docs: prove 26.2 takes the token fields, and say what only a real tenant can show"
```

- [ ] **Step 5: Merge, then deploy**

Open the PR, get it reviewed and merged to `main`, then hand the operator the sequence in **Appendix A**. Do not give them a sequence that starts on this branch — they deploy from `main`.

---

# CYCLE C — delegated IMAP OAuth

**Do not start until Cycle A+B is deployed and its acceptance checklist has been run.** The Azure application registered for that checklist is what Cycle C's Microsoft connection screen configures.

---

### Task 6: The schema — an authentication kind, a credential state, and two new tables

**Files:**
- Create: `backend/migrations/versions/0083_email_intake_oauth.py`
- Modify: `backend/app/modules/email_intake/models.py`
- Modify: `backend/app/models/__init__.py` (export the two new classes, following that file's existing pattern)
- Modify: `docs/modules/email-intake/spec.md` (§2.1, amendment A5)
- Test: `backend/tests/test_email_intake_oauth_schema.py`

**Interfaces:**
- Produces: `EmailMailbox.auth_kind: Mapped[str]`, `EmailMailbox.credential_state: Mapped[str]`, `EmailMailbox.credential_id: Mapped[UUID | None]`; ORM classes `EmailOAuthApp` and `EmailOAuthAuthorization`. Tasks 8–14 consume all of these by these exact names.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_email_intake_oauth_schema.py`. Reuse the org/category/user seeding fixture that `backend/tests/test_email_intake_mailboxes.py` already has rather than writing a second one; `seeded_org_and_category` below stands for it.

```python
"""The schema half of delegated IMAP OAuth.

THE `credential_id` NULLABILITY IS THE INTERESTING PART. `docs/modules/
email-intake/spec.md` §2.1 made it NOT NULL because "there is no
unauthenticated IMAP". That property is preserved in a STRONGER form by the
check constraint below -- a mailbox with no credential can never be POLLED --
and the nullable window exists only between "an OAuth mailbox was created" and
"somebody consented", which the old schema could not express at all.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


@pytest.mark.asyncio
async def test_existing_mailboxes_default_to_the_password_path(
    admin_engine: AsyncEngine,
) -> None:
    async with AsyncSession(admin_engine) as session:
        default = (
            await session.execute(
                text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name='email_mailbox' AND column_name='auth_kind'"
                )
            )
        ).scalar_one()
    assert "password" in default


@pytest.mark.asyncio
async def test_a_credential_less_mailbox_cannot_be_active(
    admin_engine: AsyncEngine, seeded_org_and_category: tuple[str, str, str]
) -> None:
    org_id, category_id, user_id = seeded_org_and_category
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO email_mailbox (id, org_id, address, host, port, "
                    "username, credential_id, default_category_id, active, "
                    "auth_kind, credential_state, created_by, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), :org, 'a@b.c', 'h', 993, 'u', NULL, "
                    ":cat, true, 'microsoft_oauth', 'ok', :usr, now(), now())"
                ),
                {"org": org_id, "cat": category_id, "usr": user_id},
            )


@pytest.mark.asyncio
async def test_an_unconnected_oauth_mailbox_is_legal_when_inactive(
    admin_engine: AsyncEngine, seeded_org_and_category: tuple[str, str, str]
) -> None:
    org_id, category_id, user_id = seeded_org_and_category
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text(
                "INSERT INTO email_mailbox (id, org_id, address, host, port, "
                "username, credential_id, default_category_id, active, "
                "auth_kind, credential_state, created_by, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :org, 'a@b.c', 'h', 993, 'u', NULL, "
                ":cat, false, 'microsoft_oauth', 'ok', :usr, now(), now())"
            ),
            {"org": org_id, "cat": category_id, "usr": user_id},
        )
        await session.commit()


@pytest.mark.asyncio
async def test_an_unknown_auth_kind_is_refused(
    admin_engine: AsyncEngine, seeded_org_and_category: tuple[str, str, str]
) -> None:
    """The check constraint is the vocabulary's authority. A typo in a future
    migration must fail loudly rather than create a mailbox nothing polls."""
    org_id, category_id, user_id = seeded_org_and_category
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO email_mailbox (id, org_id, address, host, port, "
                    "username, credential_id, default_category_id, active, "
                    "auth_kind, credential_state, created_by, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), :org, 'x@b.c', 'h', 993, 'u', NULL, "
                    ":cat, false, 'google_oauth', 'ok', :usr, now(), now())"
                ),
                {"org": org_id, "cat": category_id, "usr": user_id},
            )


@pytest.mark.asyncio
async def test_both_new_tables_are_rls_protected(admin_engine: AsyncEngine) -> None:
    async with AsyncSession(admin_engine) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname IN ('email_oauth_app','email_oauth_authorization')"
                )
            )
        ).all()
    assert len(rows) == 2
    for _, enabled, forced in rows:
        assert enabled and forced


@pytest.mark.asyncio
async def test_one_state_per_org_is_unique(admin_engine: AsyncEngine) -> None:
    async with AsyncSession(admin_engine) as session:
        definitions = (
            await session.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE tablename='email_oauth_authorization'"
                )
            )
        ).scalars().all()
    assert any("UNIQUE" in d and "org_id" in d and "state" in d for d in definitions)
```

- [ ] **Step 2: Run it, watch it fail**

Run: `cd backend && .venv/bin/pytest tests/test_email_intake_oauth_schema.py -v`
Expected: FAIL — `column "auth_kind" does not exist`.

- [ ] **Step 3: Write the migration**

Create `backend/migrations/versions/0083_email_intake_oauth.py`:

```python
"""email intake: delegated Microsoft OAuth

Design: docs/superpowers/specs/2026-09-11-mail-oauth-design.md §4.7.

THE ONE RELAXATION, AND WHY IT IS NOT A WEAKENING. `email_mailbox.credential_id`
was NOT NULL because there is no unauthenticated IMAP. It becomes nullable, and
the property is preserved in a stronger form by `ck_email_mailbox_credential`:
a mailbox with no credential can never be ACTIVE, and `poller._claim` only ever
claims active mailboxes. The nullable window is exactly "created, not yet
consented" -- a state the old schema could not express, and whose alternative
was a side table holding a copy of every mailbox field until consent landed.

FORWARD-ONLY. The downgrade drops what this added and restores the NOT NULL,
which can only succeed where no unconnected OAuth mailbox exists -- correct, and
the CI round-trip (`upgrade head -> downgrade base -> upgrade head`) runs it on
an empty database where it always does.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0083"
down_revision: str | None = "0082"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"


def _rls(table: str) -> None:
    """Standard tenant_isolation pattern (0002_rls.py, 0043, 0050, 0077, 0081,
    0082), WITH CHECK stated explicitly."""
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
    )


def upgrade() -> None:
    op.add_column(
        "email_mailbox",
        sa.Column("auth_kind", sa.String(20), nullable=False, server_default="password"),
    )
    op.add_column(
        "email_mailbox",
        sa.Column(
            "credential_state", sa.String(20), nullable=False, server_default="ok"
        ),
    )
    op.create_check_constraint(
        "ck_email_mailbox_auth_kind",
        "email_mailbox",
        "auth_kind IN ('password', 'microsoft_oauth')",
    )
    op.create_check_constraint(
        "ck_email_mailbox_credential_state",
        "email_mailbox",
        "credential_state IN ('ok', 'needs_reconsent')",
    )
    op.alter_column("email_mailbox", "credential_id", nullable=True)
    op.create_check_constraint(
        "ck_email_mailbox_credential",
        "email_mailbox",
        "credential_id IS NOT NULL OR active IS FALSE",
    )

    op.create_table(
        "email_oauth_app",
        sa.Column(
            "org_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            primary_key=True,
        ),
        sa.Column("tenant_id", sa.String(200), nullable=False),
        sa.Column("client_id", sa.String(200), nullable=False),
        # The client SECRET is not here. It is a vault credential.
        sa.Column(
            "credential_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("credentials.id"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    _rls("email_oauth_app")

    op.create_table(
        "email_oauth_authorization",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "mailbox_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("email_mailbox.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("state", sa.String(64), nullable=False),
        # SEALED, not plaintext. A PKCE verifier is worthless without the
        # authorization code, so this is not an invariant 3 requirement -- it
        # costs one vault call and removes the argument entirely.
        sa.Column("sealed_verifier", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("org_id", "state", name="uq_email_oauth_auth_org_state"),
    )
    _rls("email_oauth_authorization")


def downgrade() -> None:
    # The policies go with the tables; DROP TABLE drops them. Named here rather
    # than dropped separately so the asymmetry reads as deliberate.
    op.drop_table("email_oauth_authorization")
    op.drop_table("email_oauth_app")
    op.drop_constraint("ck_email_mailbox_credential", "email_mailbox", type_="check")
    op.alter_column("email_mailbox", "credential_id", nullable=False)
    op.drop_constraint("ck_email_mailbox_credential_state", "email_mailbox", type_="check")
    op.drop_constraint("ck_email_mailbox_auth_kind", "email_mailbox", type_="check")
    op.drop_column("email_mailbox", "credential_state")
    op.drop_column("email_mailbox", "auth_kind")
```

- [ ] **Step 4: Update the ORM**

In `backend/app/modules/email_intake/models.py`, replace `EmailMailbox.credential_id` and add the two columns beside it:

```python
    # NULLABLE as of migration 0083, and guarded by
    # `ck_email_mailbox_credential`: a mailbox with no credential can never be
    # ACTIVE, and `poller._claim` only claims active mailboxes. The window is
    # "an OAuth mailbox was created, nobody has consented yet".
    credential_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("credentials.id"), default=None
    )
    #: `password` or `microsoft_oauth`. The check constraint is the authority.
    auth_kind: Mapped[str] = mapped_column(
        String(20), server_default="password", default="password"
    )
    #: `ok` or `needs_reconsent`. A REVOKED GRANT IS A STATE, not an error to
    #: retry: `poller._claim` refuses a mailbox that is not `ok`, so "stops
    #: trying" is a property of the claim rather than a branch every caller has
    #: to remember.
    credential_state: Mapped[str] = mapped_column(
        String(20), server_default="ok", default="ok"
    )
```

Add both new models at the end of the file (`test_schema_drift.py` compares `Base.metadata` to the live schema, so every column must match the migration exactly in type, length and nullability):

```python
class EmailOAuthApp(Base):
    """The Azure application registration this org's mailboxes consent against.

    ONE PER ORG. Each on-prem install registers its own application, because
    each has its own hostname and this platform ships no client secret. The
    SECRET is NOT here -- it is a vault credential of type `email_oauth_client`
    reached through `credential_id` (invariant #3).
    """

    __tablename__ = "email_oauth_app"

    org_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id"), primary_key=True
    )
    tenant_id: Mapped[str] = mapped_column(String(200))
    client_id: Mapped[str] = mapped_column(String(200))
    credential_id: Mapped[UUID] = mapped_column(ForeignKey("credentials.id"))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class EmailOAuthAuthorization(Base):
    """One in-flight consent. SINGLE USE, ten minutes, bound to a person.

    `state` is NOT what binds a callback to a tenant -- the completing call
    carries the operator's own bearer token and runs inside `org_session`, so
    RLS does that. This row is the third, independent check (design §4.3).
    """

    __tablename__ = "email_oauth_authorization"
    __table_args__ = (
        UniqueConstraint("org_id", "state", name="uq_email_oauth_auth_org_state"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    mailbox_id: Mapped[UUID] = mapped_column(
        ForeignKey("email_mailbox.id", ondelete="CASCADE")
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    state: Mapped[str] = mapped_column(String(64))
    sealed_verifier: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column()
    consumed_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
```

Export both from `backend/app/models/__init__.py` next to the existing email-intake models.

- [ ] **Step 5: Amend the module spec**

In `docs/modules/email-intake/spec.md` §2.1, immediately after amendment A4b, add:

```markdown
**Amendment A5 (2026-09-11, migration 0083) — `credential_id` is now NULLABLE,
and the property it protected is stronger, not weaker.** This list said NOT NULL
because "there is no unauthenticated IMAP". Delegated Microsoft OAuth introduces
a state the old schema could not express: a mailbox that exists and has not been
consented to yet. `credential_id` becomes nullable and
`CHECK (credential_id IS NOT NULL OR active IS FALSE)` takes over — *a mailbox
with no credential can never be polled*, which `poller._claim`'s
`active.is_(True)` filter already assumed. Two columns are added: `auth_kind`
(`password` | `microsoft_oauth`, default `password`) and `credential_state`
(`ok` | `needs_reconsent`, default `ok`). Every existing row takes the defaults
and its behaviour is unchanged.
See `docs/superpowers/specs/2026-09-11-mail-oauth-design.md` §4.7.
```

- [ ] **Step 6: Run everything**

Run:
```bash
cd backend && .venv/bin/alembic upgrade head \
  && .venv/bin/pytest tests/test_email_intake_oauth_schema.py tests/test_schema_drift.py -v \
  && .venv/bin/mypy app && .venv/bin/ruff check app tests
```
Expected: PASS, no drift.

- [ ] **Step 7: Falsify**

Delete the `ck_email_mailbox_credential` constraint from the migration and re-run. Expected: `test_a_credential_less_mailbox_cannot_be_active` RED. Restore.

- [ ] **Step 8: Commit**

```bash
git add backend/migrations/versions/0083_email_intake_oauth.py \
        backend/app/modules/email_intake/models.py backend/app/models/__init__.py \
        backend/tests/test_email_intake_oauth_schema.py docs/modules/email-intake/spec.md
git commit -m "email-intake: schema for delegated Microsoft OAuth, with a stronger credential invariant"
```

---

### Task 7: The Microsoft endpoints, pinned; and the console's own URL

**Files:**
- Create: `backend/app/modules/email_intake/microsoft.py`
- Modify: `backend/app/core/config.py`
- Modify: `deploy/.env.example`
- Test: `backend/tests/test_email_intake_microsoft.py`

**Interfaces:**
- Produces, all consumed by Tasks 9 and 11 at these exact names:
  - `MICROSOFT_HOST: str` = `"https://login.microsoftonline.com"`
  - `IMAP_SCOPE: str` = `"https://outlook.office.com/IMAP.AccessAsUser.All offline_access"`
  - `CALLBACK_PATH: str` = `"/settings/email-intake/oauth/callback"`
  - `authorize_url(*, tenant_id: str, client_id: str, redirect_uri: str, state: str, code_challenge: str) -> str`
  - `token_url(tenant_id: str) -> str`
  - `make_verifier() -> str`, `challenge_of(verifier: str) -> str`
  - `redirect_uri(settings: Settings) -> str`
  - `class MicrosoftNotConfigured(Exception)`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_email_intake_microsoft.py`. `settings_factory` stands for whatever settings-construction helper `backend/tests/` already provides.

```python
"""THE HOST PIN IS THE WHOLE CONTROL ON THIS PATH.

`app/core/egress.py` guards webhook delivery and nothing else, so the token
exchange is an UNGUARDED outbound POST carrying a refresh token and a client
secret. A configurable token URL would therefore be a credential-exfiltration
primitive available to anyone holding `email_intake.admin`: point it at a host
you control and the platform delivers both, per poll, forever. The operator
supplies a TENANT ID; the host is a module constant.
"""

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest

from app.modules.email_intake.microsoft import (
    IMAP_SCOPE,
    MICROSOFT_HOST,
    MicrosoftNotConfigured,
    authorize_url,
    challenge_of,
    make_verifier,
    redirect_uri,
    token_url,
)


def test_the_token_host_is_microsofts_and_cannot_be_moved() -> None:
    assert MICROSOFT_HOST == "https://login.microsoftonline.com"
    assert token_url("tid").startswith("https://login.microsoftonline.com/tid/")


def test_a_tenant_id_cannot_smuggle_a_different_host() -> None:
    for hostile in ("../../evil.example", "evil.example/x", "a?b", "a#b", "a/b", "", "@"):
        with pytest.raises(ValueError):
            token_url(hostile)


def test_the_authorize_url_carries_pkce_and_the_offline_scope() -> None:
    url = authorize_url(
        tenant_id="tid",
        client_id="cid",
        redirect_uri="https://itms.example/settings/email-intake/oauth/callback",
        state="st",
        code_challenge="ch",
    )
    query = parse_qs(urlparse(url).query)
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == ["ch"]
    assert query["state"] == ["st"]
    assert query["scope"] == [IMAP_SCOPE]
    # offline_access is what yields a refresh token at all. Without it the
    # mailbox works for one hour and then dies with no way back.
    assert "offline_access" in IMAP_SCOPE
    # A second mailbox must not be silently connected as the signed-in account.
    assert query["prompt"] == ["select_account"]


def test_the_challenge_is_s256_of_the_verifier() -> None:
    verifier = make_verifier()
    assert 43 <= len(verifier) <= 128
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert challenge_of(verifier) == expected


def test_the_redirect_uri_comes_from_config_and_never_from_a_request() -> None:
    configured = settings_factory(public_console_url="https://itms.example")
    assert (
        redirect_uri(configured)
        == "https://itms.example/settings/email-intake/oauth/callback"
    )
    assert (
        redirect_uri(settings_factory(public_console_url="https://itms.example/"))
        == "https://itms.example/settings/email-intake/oauth/callback"
    )
    with pytest.raises(MicrosoftNotConfigured):
        redirect_uri(settings_factory(public_console_url=None))
```

- [ ] **Step 2: Run it, watch it fail**

Run: `cd backend && .venv/bin/pytest tests/test_email_intake_microsoft.py -v`
Expected: FAIL — the module does not exist.

- [ ] **Step 3: Implement**

Create `backend/app/modules/email_intake/microsoft.py`:

```python
"""Microsoft's OAuth endpoints, and the PKCE arithmetic.

THE HOST IS A CONSTANT AND THE OPERATOR SUPPLIES ONLY A TENANT ID. See
`tests/test_email_intake_microsoft.py`'s docstring for the attack this closes;
in one line: `app/core/egress.py` does not guard this call, so the pin is the
only thing standing between `email_intake.admin` and a refresh token posted to
a host of the holder's choosing.

THE OUTGOING HALF IS DELIBERATELY ASYMMETRIC. `Settings -> Email` accepts a
free-form `authTokenUrl` because Keycloak's own field is free-form and the
value is STORED IN THE REALM rather than dialled by this process. Nothing here
is stored anywhere: it is dialled, by this process, holding two secrets.

NO TOKEN IS OBTAINED HERE. This module builds URLs and does arithmetic; the
exchanges live in `oauth.py` (start/complete) and `tokens.py` (refresh), where
there is a session to audit into.
"""

import base64
import hashlib
import re
import secrets
from urllib.parse import urlencode

from app.core.config import Settings

__all__ = [
    "CALLBACK_PATH",
    "IMAP_SCOPE",
    "MICROSOFT_HOST",
    "MicrosoftNotConfigured",
    "authorize_url",
    "challenge_of",
    "make_verifier",
    "redirect_uri",
    "token_url",
]

#: NOT CONFIGURABLE. See the module docstring.
MICROSOFT_HOST = "https://login.microsoftonline.com"

#: `offline_access` is what yields a refresh token; without it the connection
#: works for exactly one hour and then dies with no way back.
IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"

#: The CONSOLE path Microsoft redirects a browser to. It is an SPA route, NOT
#: an API route -- the completing call is made afterwards by the console,
#: carrying the operator's own bearer token (design §4.3).
CALLBACK_PATH = "/settings/email-intake/oauth/callback"

#: A directory id (GUID) or a verified domain. Anything that could introduce a
#: path segment, a query or a fragment is refused, because the tenant id is
#: interpolated directly into a URL.
_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,199}$")


class MicrosoftNotConfigured(Exception):
    """This deployment has not set `ITS_PUBLIC_CONSOLE_URL`.

    A SUPPORTED STATE, not a fault: an install that never uses Microsoft never
    needs it. The routes answer 503 naming the setting -- the same three-way
    contract `KeycloakAdminConfig.from_settings` already uses for the
    provisioning client.
    """


def _tenant(tenant_id: str) -> str:
    if not _TENANT_RE.match(tenant_id):
        raise ValueError("the directory (tenant) id is not a valid id or domain")
    return tenant_id


def token_url(tenant_id: str) -> str:
    return f"{MICROSOFT_HOST}/{_tenant(tenant_id)}/oauth2/v2.0/token"


def authorize_url(
    *,
    tenant_id: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "response_mode": "query",
            "scope": IMAP_SCOPE,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            # Without this, a second mailbox is silently connected as whoever
            # is already signed in -- and the operator finds out when the wrong
            # inbox starts becoming tickets.
            "prompt": "select_account",
        }
    )
    return f"{MICROSOFT_HOST}/{_tenant(tenant_id)}/oauth2/v2.0/authorize?{query}"


def make_verifier() -> str:
    """43-128 characters of unreserved ASCII, per RFC 7636."""
    return secrets.token_urlsafe(64)


def challenge_of(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def redirect_uri(settings: Settings) -> str:
    """From CONFIG, never from the request.

    `Host` and `X-Forwarded-Host` are attacker-controllable; deriving a redirect
    URI from either is the host-header injection that hands somebody else's
    authorization code away. Microsoft would refuse the mismatch anyway -- this
    platform must not be relying on the provider to catch its own mistake.
    """
    base = getattr(settings, "public_console_url", None)
    if not base:
        raise MicrosoftNotConfigured(
            "ITS_PUBLIC_CONSOLE_URL is not set, so this platform does not know "
            "the address a browser reaches its console at"
        )
    return f"{str(base).rstrip('/')}{CALLBACK_PATH}"
```

In `backend/app/core/config.py`, beside the other optional URLs:

```python
    # The browser-facing origin of the console, e.g. https://itms.toeshee.com.
    # OPTIONAL: only the Microsoft OAuth routes need it, and they answer 503
    # naming it when it is absent. It is a SETTING rather than something derived
    # from the request because a redirect URI taken from `Host` is host-header
    # injection (docs/superpowers/specs/2026-09-11-mail-oauth-design.md §5.2).
    public_console_url: str | None = None
```

Append to `deploy/.env.example`:

```
# The address a browser reaches this console at, with scheme and no trailing
# slash -- e.g. https://itms.toeshee.com.
#
# REQUIRED ONLY FOR MICROSOFT OAUTH (Email Intake). Everything else derives its
# URLs from the request. This one cannot: it is sent to Microsoft as the OAuth
# redirect URI and must be a value an attacker cannot influence, so it is
# configuration rather than a header. The SAME string, with
# /settings/email-intake/oauth/callback appended, must be registered as a
# redirect URI on the Azure application.
#
# Left blank, the OAuth routes answer 503 naming this setting and nothing else
# changes.
ITS_PUBLIC_CONSOLE_URL=
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && .venv/bin/pytest tests/test_email_intake_microsoft.py -v && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: PASS, clean.

- [ ] **Step 5: Falsify**

Make `token_url` interpolate the tenant id unvalidated. Expected: `test_a_tenant_id_cannot_smuggle_a_different_host` RED. Restore.

- [ ] **Step 6: Commit**

```bash
git add backend/app/modules/email_intake/microsoft.py backend/app/core/config.py \
        deploy/.env.example backend/tests/test_email_intake_microsoft.py
git commit -m "email-intake: Microsoft's endpoints, pinned, with the redirect URI in config"
```

---

### Task 8: The Azure application registration, and its vaulted client secret

**Files:**
- Create: `backend/app/modules/email_intake/oauth_app.py`
- Modify: `backend/app/modules/email_intake/mailboxes.py` (`_store_secret` gains two keyword arguments **with defaults**, so every existing caller is unchanged)
- Modify: `backend/app/modules/email_intake/schema.py`
- Modify: `backend/app/modules/email_intake/api.py`
- Test: `backend/tests/test_email_intake_oauth_app.py`

**Interfaces:**
- Consumes: `EmailOAuthApp` (Task 6).
- Produces, consumed by Tasks 9 and 11:
  - `async def get_app(session, *, org_id: UUID) -> EmailOAuthApp | None`
  - `async def set_app(session, *, org_id: UUID, actor_id: UUID, body: OAuthAppUpdate, vault: VaultService) -> EmailOAuthApp`
  - `async def app_client_secret(session, *, org_id: UUID, vault: VaultService) -> str`
  - `_store_secret(..., name: str = "email-mailbox-secret", credential_type: str = "email_mailbox")`
  - Pydantic `OAuthAppUpdate` / `OAuthAppOut`
  - `GET`/`PUT /api/v1/email-intake/oauth-app`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_email_intake_oauth_app.py`, using the API fixtures `backend/tests/test_email_intake_api.py` already provides (`api_env` for an `email_intake.admin` caller; `api_env_reader` stands for a read-only one — add it there if it does not exist yet).

```python
"""The Azure registration.

THE SECRET IS A STORED CREDENTIAL. Unlike the outgoing half, where it is written
through to the realm and never retained, this one is held by this platform and
used on every token exchange -- so invariant #3 applies in full: vault only,
never a response, never a log, never audit metadata.
"""

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

SECRET = "azure-client-secret-value"

BODY: dict[str, Any] = {
    "tenant_id": "contoso.onmicrosoft.com",
    "client_id": "11111111-2222-3333-4444-555555555555",
    "client_secret": SECRET,
}


@pytest.mark.asyncio
async def test_saving_the_registration_never_echoes_the_secret(api_env: Any) -> None:
    put = await api_env.client.put("/api/v1/email-intake/oauth-app", json=BODY)
    assert put.status_code == 200
    get = await api_env.client.get("/api/v1/email-intake/oauth-app")
    for response in (put, get):
        assert SECRET.encode() not in response.content
    assert get.json()["has_secret"] is True
    assert get.json()["client_id"] == BODY["client_id"]
    assert get.json()["redirect_uri"].endswith("/settings/email-intake/oauth/callback")


@pytest.mark.asyncio
async def test_the_secret_is_not_in_the_database_in_plaintext(
    api_env: Any, admin_engine: AsyncEngine
) -> None:
    await api_env.client.put("/api/v1/email-intake/oauth-app", json=BODY)
    async with AsyncSession(admin_engine) as session:
        rows = (
            await session.execute(text("SELECT ciphertext FROM credentials"))
        ).scalars().all()
    assert rows
    for row in rows:
        assert SECRET not in (row or "")


@pytest.mark.asyncio
async def test_an_omitted_secret_keeps_the_stored_one(api_env: Any) -> None:
    await api_env.client.put("/api/v1/email-intake/oauth-app", json=BODY)
    response = await api_env.client.put(
        "/api/v1/email-intake/oauth-app",
        json={"tenant_id": "contoso.onmicrosoft.com", "client_id": "other-id"},
    )
    assert response.status_code == 200
    assert response.json()["has_secret"] is True
    assert response.json()["client_id"] == "other-id"


@pytest.mark.asyncio
async def test_the_first_save_requires_a_secret(api_env: Any) -> None:
    response = await api_env.client.put(
        "/api/v1/email-intake/oauth-app",
        json={"tenant_id": "t", "client_id": "c"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_reading_needs_read_and_writing_needs_admin(api_env_reader: Any) -> None:
    assert (
        await api_env_reader.client.get("/api/v1/email-intake/oauth-app")
    ).status_code == 200
    assert (
        await api_env_reader.client.put("/api/v1/email-intake/oauth-app", json=BODY)
    ).status_code == 403


@pytest.mark.asyncio
async def test_the_audit_row_names_fields_and_no_values(
    api_env: Any, admin_engine: AsyncEngine
) -> None:
    await api_env.client.put("/api/v1/email-intake/oauth-app", json=BODY)
    async with AsyncSession(admin_engine) as session:
        rows = (
            await session.execute(text("SELECT action, metadata FROM audit_events"))
        ).all()
    assert "email_intake.oauth_app_configured" in [r[0] for r in rows]
    assert all(SECRET not in str(r[1]) for r in rows)
```

- [ ] **Step 2: Run it, watch it fail**

Run: `cd backend && .venv/bin/pytest tests/test_email_intake_oauth_app.py -v`
Expected: FAIL — 404, the route does not exist.

- [ ] **Step 3: Widen `_store_secret`**

In `backend/app/modules/email_intake/mailboxes.py`, change the signature and the one construction inside it. **The defaults are what keep every existing caller byte-identical.**

```python
async def _store_secret(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    secret: str,
    vault: VaultService,
    existing_credential_id: UUID | None = None,
    # NAMED, WITH THE OLD VALUES AS DEFAULTS. Three kinds of secret now live on
    # `credentials` for this module -- the IMAP password, the OAuth refresh
    # token, and the Azure client secret -- and `credentials.type` is how a
    # future audit tells them apart. Existing call sites pass neither and are
    # unchanged.
    name: str = "email-mailbox-secret",
    credential_type: str = "email_mailbox",
) -> UUID:
```

```python
    if credential is None:
        credential = Credential(
            org_id=org_id,
            name=name,
            type=credential_type,
            vault_ref="",
            created_by=actor_id,
        )
```

- [ ] **Step 4: Implement the service**

Create `backend/app/modules/email_intake/oauth_app.py`:

```python
"""The org's Azure application registration.

WHY THE SECRET IS VAULTED HERE AND NOT WRITTEN THROUGH. The outgoing half
(`user_admin/realm_config.py`) hands its client secret to Keycloak and forgets
it, because Keycloak does the exchange. This half's exchange is made BY THIS
PROCESS, on every poll, so the secret has to be retrievable -- which makes it a
stored credential and puts it squarely inside invariant #3. It lives in the same
envelope vault as every other, reached through a `credentials` row of type
`email_oauth_client`.

ONE ROW PER ORG, overwritten in place, with the secret rotated ON the existing
credential row rather than a fresh one: an orphaned credential per rotation is
sprawl on a table whose whole purpose is bounded lifetime.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.vault import VaultService
from app.models import EmailOAuthApp
from app.modules.email_intake.errors import MailboxValidationError
from app.modules.email_intake.mailboxes import _store_secret
from app.modules.email_intake.schema import OAuthAppUpdate

__all__ = ["app_client_secret", "get_app", "set_app"]


async def get_app(session: AsyncSession, *, org_id: UUID) -> EmailOAuthApp | None:
    return (
        await session.execute(
            select(EmailOAuthApp).where(EmailOAuthApp.org_id == org_id)
        )
    ).scalar_one_or_none()


async def set_app(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    body: OAuthAppUpdate,
    vault: VaultService,
) -> EmailOAuthApp:
    """Create or update the registration.

    An ABSENT `client_secret` keeps the stored one -- the console cannot send
    back a value it was never given, and a blank field must not clear a working
    secret when somebody corrects the tenant id. On the FIRST save there is
    nothing to keep, so it is required.
    """
    existing = await get_app(session, org_id=org_id)
    if existing is None and body.client_secret is None:
        raise MailboxValidationError(
            "a client secret is required the first time this application is saved"
        )

    if body.client_secret is not None:
        credential_id = await _store_secret(
            session,
            org_id=org_id,
            actor_id=actor_id,
            secret=body.client_secret,
            vault=vault,
            existing_credential_id=existing.credential_id if existing else None,
            name="email-oauth-client-secret",
            credential_type="email_oauth_client",
        )
    else:
        assert existing is not None  # guarded above
        credential_id = existing.credential_id

    if existing is None:
        existing = EmailOAuthApp(
            org_id=org_id,
            tenant_id=body.tenant_id,
            client_id=body.client_id,
            credential_id=credential_id,
            created_by=actor_id,
        )
    else:
        existing.tenant_id = body.tenant_id
        existing.client_id = body.client_id
        existing.credential_id = credential_id
    session.add(existing)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="email_intake.oauth_app_configured",
        target_type="email_oauth_app",
        target_id=str(org_id),
        # FIELD NAMES ONLY. `client_secret` appearing here records that the
        # secret was rotated, which is what an audit log is for; its value is
        # not, and never can be, because this log cannot be redacted afterwards.
        metadata={"fields": sorted(body.model_fields_set)},
    )
    return existing


async def app_client_secret(
    session: AsyncSession, *, org_id: UUID, vault: VaultService
) -> str:
    """Decrypt at point of use. Callers hold the result for the duration of one
    token exchange and drop it."""
    app = await get_app(session, org_id=org_id)
    if app is None:
        raise MailboxValidationError(
            "this organisation has no Microsoft application configured"
        )
    return await vault.checkout(
        session, org_id=org_id, credential_id=app.credential_id
    )
```

- [ ] **Step 5: Implement the schema and the routes**

Add to `backend/app/modules/email_intake/schema.py`:

```python
class OAuthAppUpdate(BaseModel):
    """`extra="forbid"`: a stray key is a 422, not a silent no-op."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=200)
    client_id: str = Field(min_length=1, max_length=200)
    # NO `max_length`: a Pydantic field failure echoes the offending INPUT into
    # the 422 body, and for this field that input is the client secret. The
    # bound is enforced in the route, where the refusal can name the rule
    # without repeating the value.
    client_secret: str | None = Field(default=None)


class OAuthAppOut(BaseModel):
    """NO FIELD HERE CAN CARRY THE SECRET, and that is structural rather than
    careful -- there is no key to forget to strip."""

    tenant_id: str
    client_id: str
    has_secret: bool
    redirect_uri: str = Field(
        description=(
            "Register this exact string as a redirect URI on the Azure "
            "application. It comes from server configuration, never from the "
            "request."
        )
    )
```

Add to `backend/app/modules/email_intake/api.py` (mirroring the file's existing route shape — `org_session`, `require_permission`, `_http_error`, and its own spelling for reaching the vault and settings off `request.app.state`):

```python
MAX_CLIENT_SECRET = 512


def _redirect_uri(request: Request) -> str:
    """503 rather than a guess when the console's own address is unconfigured --
    the same three-way contract the identity-provisioning routes answer with."""
    try:
        return microsoft.redirect_uri(request.app.state.settings)
    except microsoft.MicrosoftNotConfigured as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


@router.get("/email-intake/oauth-app", response_model=OAuthAppOut)
async def read_oauth_app_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("email_intake.read")),
) -> OAuthAppOut:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        app = await oauth_app.get_app(session, org_id=ctx.org_id)
        result = OAuthAppOut(
            tenant_id=app.tenant_id if app else "",
            client_id=app.client_id if app else "",
            has_secret=app is not None,
            redirect_uri=_redirect_uri(request),
        )
    return result


@router.put("/email-intake/oauth-app", response_model=OAuthAppOut)
async def write_oauth_app_route(
    body: OAuthAppUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("email_intake.admin")),
) -> OAuthAppOut:
    if body.client_secret is not None and len(body.client_secret) > MAX_CLIENT_SECRET:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"the client secret may be at most {MAX_CLIENT_SECRET} characters",
        )
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        try:
            app = await oauth_app.set_app(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                body=body,
                vault=request.app.state.vault,
            )
        except MailboxValidationError as exc:
            raise _http_error(exc) from exc
        result = OAuthAppOut(
            tenant_id=app.tenant_id,
            client_id=app.client_id,
            has_secret=True,
            redirect_uri=_redirect_uri(request),
        )
    return result
```

- [ ] **Step 6: Run the tests**

Run: `cd backend && .venv/bin/pytest tests/test_email_intake_oauth_app.py tests/test_email_intake_mailboxes.py tests/test_email_intake_api.py -v && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: PASS — including the existing mailbox tests, which is what proves the `_store_secret` defaults left every caller identical.

- [ ] **Step 7: Falsify**

Add a `client_secret: str` field to `OAuthAppOut` and populate it. Expected: `test_saving_the_registration_never_echoes_the_secret` RED. Restore.

- [ ] **Step 8: Commit**

```bash
git add backend/app/modules/email_intake backend/tests/test_email_intake_oauth_app.py
git commit -m "email-intake: the Azure registration, with its client secret in the vault"
```

---

### Task 9: Start, complete, abandon — the consent handshake, and its three routes

**Files:**
- Create: `backend/app/modules/email_intake/oauth.py`
- Modify: `backend/app/modules/email_intake/schema.py` (three request/response models; `MailboxOut` gains `auth_kind` and `credential_state`)
- Modify: `backend/app/modules/email_intake/api.py` (three routes, and `_mailbox_out`)
- Test: `backend/tests/test_email_intake_oauth_flow.py`

**Tasks 9 and 10 in the original decomposition are merged into this one task**, deliberately: the tests below are route-level, and splitting the service from its routes would end a task with a red suite for reasons the implementer cannot fix.

**Interfaces:**
- Consumes: `microsoft.*` (Task 7), `oauth_app.get_app` / `app_client_secret` (Task 8), `mailboxes._store_secret` with `name=`/`credential_type=` (Task 8), `EmailOAuthAuthorization` (Task 6).
- Produces:
  - `async def start(session, *, org_id, user_id, mailbox_id, settings, vault) -> str`
  - `async def complete(session, *, org_id, user_id, state, code, settings, vault, client=None) -> EmailMailbox`
  - `async def abandon(session, *, org_id, user_id, state) -> None`
  - `class OAuthFlowError(Exception)` carrying `.status: int`
  - `POST /api/v1/email-intake/mailboxes/{mailbox_id}/oauth/start` → `OAuthStartOut`
  - `POST /api/v1/email-intake/oauth/complete` → `MailboxOut`
  - `POST /api/v1/email-intake/oauth/abandon` → 204
  - Task 14 calls all three routes.

- [ ] **Step 1: Build the fixture**

`oauth_env` is new to this file. It must provide:

- an org with a saved `email_oauth_app` (tenant `t`, client `c`, a vaulted secret);
- `mailbox_id` — an inactive mailbox with `auth_kind="microsoft_oauth"` and `credential_id=None`;
- `client` — an authenticated test client for an `email_intake.admin` user;
- `settings` carrying `public_console_url="https://itms.example"`;
- `install_token_handler(fn)` — installs `httpx.MockTransport(fn)` over `httpx.AsyncClient`, using the same `monkeypatch.setattr(httpx, "AsyncClient", factory)` shape `test_realm_settings_api.py` already uses;
- `expire_authorizations()` — sets every `email_oauth_authorization.expires_at` into the past;
- `audit_actions()` / `audit_dump()` — the actions written, and the whole metadata dump as a string.

Plus `other_org_client` (an `email_intake.admin` user in a *different* org) and `same_org_other_admin_client` (a *different* `email_intake.admin` user in the *same* org).

- [ ] **Step 2: Write the failing test**

Create `backend/tests/test_email_intake_oauth_flow.py`:

```python
"""The consent handshake, and the ways it is prevented from being abused.

THE CALLBACK IS NOT AN API ROUTE. Microsoft redirects a browser to a CONSOLE
route; the console then makes an ORDINARY AUTHENTICATED CALL with the operator's
own bearer token. So `state` is not carrying authentication, is not carrying
tenancy, and is free to do only the job it is designed for. These tests pin all
three separately (design §4.3).
"""

from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

REFRESH = "refresh-token-value-do-not-leak"


def _token_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": "access-token",
            "refresh_token": REFRESH,
            "expires_in": 3599,
            "token_type": "Bearer",
        },
    )


async def _state(env: Any) -> str:
    start = await env.client.post(
        f"/api/v1/email-intake/mailboxes/{env.mailbox_id}/oauth/start"
    )
    assert start.status_code == 200
    return parse_qs(urlparse(start.json()["authorize_url"]).query)["state"][0]


@pytest.mark.asyncio
async def test_start_returns_an_authorize_url_with_pkce(oauth_env: Any) -> None:
    response = await oauth_env.client.post(
        f"/api/v1/email-intake/mailboxes/{oauth_env.mailbox_id}/oauth/start"
    )
    assert response.status_code == 200
    query = parse_qs(urlparse(response.json()["authorize_url"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["state"][0]) >= 32
    assert query["redirect_uri"] == [
        "https://itms.example/settings/email-intake/oauth/callback"
    ]


@pytest.mark.asyncio
async def test_start_refuses_a_password_mailbox(oauth_env: Any) -> None:
    response = await oauth_env.client.post(
        f"/api/v1/email-intake/mailboxes/{oauth_env.password_mailbox_id}/oauth/start"
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_complete_stores_the_refresh_token_and_never_returns_it(
    oauth_env: Any,
) -> None:
    oauth_env.install_token_handler(_token_response)
    state = await _state(oauth_env)
    done = await oauth_env.client.post(
        "/api/v1/email-intake/oauth/complete", json={"state": state, "code": "c"}
    )
    assert done.status_code == 200
    assert REFRESH.encode() not in done.content
    assert done.json()["has_credential"] is True
    assert done.json()["credential_state"] == "ok"


@pytest.mark.asyncio
async def test_the_verifier_sent_is_the_preimage_of_the_challenge_shown(
    oauth_env: Any,
) -> None:
    """PKCE is worth nothing unless these two are actually related."""
    import base64
    import hashlib

    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        seen["verifier"] = form["code_verifier"]
        return _token_response(request)

    oauth_env.install_token_handler(handler)
    start = await oauth_env.client.post(
        f"/api/v1/email-intake/mailboxes/{oauth_env.mailbox_id}/oauth/start"
    )
    query = parse_qs(urlparse(start.json()["authorize_url"]).query)
    state, challenge = query["state"][0], query["code_challenge"][0]
    await oauth_env.client.post(
        "/api/v1/email-intake/oauth/complete", json={"state": state, "code": "c"}
    )
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(seen["verifier"].encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert expected == challenge


@pytest.mark.asyncio
async def test_a_state_is_single_use(oauth_env: Any) -> None:
    oauth_env.install_token_handler(_token_response)
    state = await _state(oauth_env)
    first = await oauth_env.client.post(
        "/api/v1/email-intake/oauth/complete", json={"state": state, "code": "c"}
    )
    second = await oauth_env.client.post(
        "/api/v1/email-intake/oauth/complete", json={"state": state, "code": "c"}
    )
    assert first.status_code == 200
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_a_state_from_another_organization_is_invisible(
    oauth_env: Any, other_org_client: Any
) -> None:
    """RLS, not a string comparison. The completing call runs inside
    `org_session(caller's org)`, so the row simply is not there."""
    state = await _state(oauth_env)
    response = await other_org_client.post(
        "/api/v1/email-intake/oauth/complete", json={"state": state, "code": "c"}
    )
    assert response.status_code in (403, 404, 409)


@pytest.mark.asyncio
async def test_a_state_belonging_to_another_user_is_refused(
    oauth_env: Any, same_org_other_admin_client: Any
) -> None:
    state = await _state(oauth_env)
    response = await same_org_other_admin_client.post(
        "/api/v1/email-intake/oauth/complete", json={"state": state, "code": "c"}
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_an_expired_authorization_is_refused(oauth_env: Any) -> None:
    state = await _state(oauth_env)
    await oauth_env.expire_authorizations()
    response = await oauth_env.client.post(
        "/api/v1/email-intake/oauth/complete", json={"state": state, "code": "c"}
    )
    assert response.status_code == 410


@pytest.mark.asyncio
async def test_a_response_with_no_refresh_token_is_refused_with_an_explanation(
    oauth_env: Any,
) -> None:
    """Without `offline_access` the connection works for an hour and then dies.
    Refusing at connect time is the only moment anyone will understand why."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "at", "expires_in": 3599})

    oauth_env.install_token_handler(handler)
    state = await _state(oauth_env)
    response = await oauth_env.client.post(
        "/api/v1/email-intake/oauth/complete", json={"state": state, "code": "c"}
    )
    assert response.status_code == 400
    assert "offline_access" in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_refused_exchange_never_echoes_the_providers_body(
    oauth_env: Any,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Provider error bodies echo the request, and this request carried a
        # client secret.
        return httpx.Response(
            400,
            json={
                "error": "invalid_client",
                "error_description": "secret azure-client-secret-value was wrong",
            },
        )

    oauth_env.install_token_handler(handler)
    state = await _state(oauth_env)
    response = await oauth_env.client.post(
        "/api/v1/email-intake/oauth/complete", json={"state": state, "code": "c"}
    )
    assert response.status_code == 400
    assert b"azure-client-secret-value" not in response.content
    # And it points at the right screen: reconnecting the mailbox will NOT fix
    # a bad application registration.
    assert "Microsoft connection" in response.json()["detail"]


@pytest.mark.asyncio
async def test_abandon_consumes_the_row_and_leaves_the_mailbox_alone(
    oauth_env: Any,
) -> None:
    state = await _state(oauth_env)
    assert (
        await oauth_env.client.post(
            "/api/v1/email-intake/oauth/abandon", json={"state": state}
        )
    ).status_code == 204
    assert (
        await oauth_env.client.post(
            "/api/v1/email-intake/oauth/complete", json={"state": state, "code": "c"}
        )
    ).status_code == 409
    mailbox = await oauth_env.client.get(
        f"/api/v1/email-intake/mailboxes/{oauth_env.mailbox_id}"
    )
    assert mailbox.json()["has_credential"] is False


@pytest.mark.asyncio
async def test_the_audit_chain_records_start_and_connect_and_no_token(
    oauth_env: Any,
) -> None:
    oauth_env.install_token_handler(_token_response)
    state = await _state(oauth_env)
    await oauth_env.client.post(
        "/api/v1/email-intake/oauth/complete", json={"state": state, "code": "c"}
    )
    actions = await oauth_env.audit_actions()
    assert "email_intake.oauth_started" in actions
    assert "email_intake.oauth_connected" in actions
    assert REFRESH not in await oauth_env.audit_dump()


@pytest.mark.asyncio
async def test_every_oauth_route_needs_admin(api_env_reader: Any, oauth_env: Any) -> None:
    paths = [
        (f"/api/v1/email-intake/mailboxes/{oauth_env.mailbox_id}/oauth/start", {}),
        ("/api/v1/email-intake/oauth/complete", {"state": "s", "code": "c"}),
        ("/api/v1/email-intake/oauth/abandon", {"state": "s"}),
    ]
    for path, body in paths:
        assert (await api_env_reader.client.post(path, json=body)).status_code == 403
```

- [ ] **Step 3: Run it, watch it fail**

Run: `cd backend && .venv/bin/pytest tests/test_email_intake_oauth_flow.py -v`
Expected: FAIL — 404 on every route.

- [ ] **Step 4: Implement the service**

Create `backend/app/modules/email_intake/oauth.py`:

```python
"""The delegated-consent handshake: start, complete, abandon.

WHERE THE CALLBACK LANDS, AND WHY IT MATTERS MORE THAN ANYTHING ELSE HERE.
Microsoft redirects the browser to a CONSOLE route (`microsoft.CALLBACK_PATH`),
served by the SPA. The console reads `code` and `state`, strips them from the
address bar, and calls `complete` as an ORDINARY AUTHENTICATED REQUEST carrying
the operator's own bearer token.

An API route Microsoft redirected to would arrive with no `Authorization`
header, and authorising it would mean inventing a second authentication scheme
whose only credential is `state` -- making one string simultaneously the CSRF
defence, the tenant binding and the authentication. Instead:

  * RBAC (invariant #6) is the router's ordinary `email_intake.admin` gate.
  * TENANCY (invariant #5) is `org_session` + RLS: a row belonging to another
    organisation is NOT VISIBLE, so a stolen `state` replayed elsewhere finds
    nothing. `state` is not what binds a callback to a tenant.
  * `state` does only CSRF and single-use replay defence, which is its job.

SINGLE USE IS AN `UPDATE ... RETURNING`, not a read-then-write. Two concurrent
completions must produce one connection and one 409, not two token exchanges
against one authorization code.

THE PROVIDER'S STATUS AND `error` CODE, NEVER ITS BODY. Token-endpoint error
bodies echo the request, and this request carried a client secret.
"""

import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.config import Settings
from app.core.vault import VaultService
from app.models import EmailOAuthAuthorization
from app.modules.email_intake import microsoft, oauth_app
from app.modules.email_intake.mailboxes import _store_secret
from app.modules.email_intake.models import EmailMailbox

__all__ = ["OAuthFlowError", "TTL", "abandon", "complete", "start"]

#: Long enough for a sign-in with MFA, short enough that an intercepted `state`
#: is worthless by the time anyone could use it.
TTL = timedelta(minutes=10)

_TIMEOUT = 15.0


class OAuthFlowError(Exception):
    """Carries the HTTP status the route should answer, so the route does not
    re-derive it from the message text."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


async def _mailbox(
    session: AsyncSession, org_id: UUID, mailbox_id: UUID
) -> EmailMailbox:
    row = (
        await session.execute(
            select(EmailMailbox).where(
                EmailMailbox.org_id == org_id, EmailMailbox.id == mailbox_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise OAuthFlowError("no such mailbox", status=404)
    if row.auth_kind != "microsoft_oauth":
        raise OAuthFlowError(
            "this mailbox signs in with a password; change its sign-in method first",
            status=409,
        )
    return row


async def start(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    mailbox_id: UUID,
    settings: Settings,
    vault: VaultService,
) -> str:
    mailbox = await _mailbox(session, org_id, mailbox_id)
    app = await oauth_app.get_app(session, org_id=org_id)
    if app is None:
        raise OAuthFlowError(
            "this organisation has no Microsoft application configured yet",
            status=409,
        )
    redirect = microsoft.redirect_uri(settings)

    verifier = microsoft.make_verifier()
    challenge = microsoft.challenge_of(verifier)
    state = secrets.token_urlsafe(32)

    row = EmailOAuthAuthorization(
        org_id=org_id,
        mailbox_id=mailbox.id,
        user_id=user_id,
        state=state,
        sealed_verifier="",
        expires_at=datetime.now(UTC) + TTL,
    )
    session.add(row)
    # Flushed first because `seal_bytes` binds the ciphertext to this row's id.
    await session.flush()
    row.sealed_verifier = await vault.seal_bytes(
        session, org_id=org_id, key_id=row.id, plaintext=verifier.encode()
    )
    session.add(row)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=user_id,
        action="email_intake.oauth_started",
        target_type="email_mailbox",
        target_id=str(mailbox.id),
        metadata={"address": mailbox.address},
    )
    return microsoft.authorize_url(
        tenant_id=app.tenant_id,
        client_id=app.client_id,
        redirect_uri=redirect,
        state=state,
        code_challenge=challenge,
    )


async def _consume(
    session: AsyncSession, *, org_id: UUID, user_id: UUID, state: str
) -> EmailOAuthAuthorization:
    """Single-use, atomically. A read-then-write here would let two concurrent
    completions both redeem the same authorization code."""
    now = datetime.now(UTC)
    row = (
        await session.execute(
            update(EmailOAuthAuthorization)
            .where(
                EmailOAuthAuthorization.org_id == org_id,
                EmailOAuthAuthorization.state == state,
                EmailOAuthAuthorization.consumed_at.is_(None),
            )
            .values(consumed_at=now)
            .returning(EmailOAuthAuthorization)
        )
    ).scalar_one_or_none()
    if row is None:
        # Also the answer for a state belonging to another ORG: RLS made the
        # row invisible, so there was nothing to update. One message for both,
        # deliberately -- it should not be possible to probe another tenant's
        # states by reading the difference.
        raise OAuthFlowError(
            "that sign-in has already been used, or does not exist", status=409
        )
    if row.expires_at < now:
        raise OAuthFlowError("that sign-in took too long; start it again", status=410)
    if row.user_id != user_id:
        raise OAuthFlowError("that sign-in was started by somebody else", status=403)
    return row


def _refusal(response: httpx.Response) -> str:
    """A sentence for the console, built from the provider's `error` CODE only.

    The `error_description` is DISCARDED: it routinely quotes what it refused,
    and what it refused here is a client secret or an authorization code.
    """
    try:
        code = str(response.json().get("error", ""))
    except ValueError:
        code = ""
    if code in ("invalid_client", "unauthorized_client"):
        return (
            "The Microsoft application this organisation is registered with was "
            "refused. Check Settings → Email Intake → Microsoft connection."
        )
    if code == "invalid_grant":
        return "Microsoft refused that sign-in. Start it again."
    return f"Microsoft refused the sign-in (HTTP {response.status_code})."


async def complete(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    state: str,
    code: str,
    settings: Settings,
    vault: VaultService,
    client: httpx.AsyncClient | None = None,
) -> EmailMailbox:
    row = await _consume(session, org_id=org_id, user_id=user_id, state=state)
    mailbox = await _mailbox(session, org_id, row.mailbox_id)
    app = await oauth_app.get_app(session, org_id=org_id)
    if app is None:
        raise OAuthFlowError("the Microsoft application has been removed", status=409)

    verifier = (
        await vault.open_bytes(
            session, org_id=org_id, key_id=row.id, sealed=row.sealed_verifier
        )
    ).decode()
    secret = await oauth_app.app_client_secret(session, org_id=org_id, vault=vault)

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        response = await client.post(
            microsoft.token_url(app.tenant_id),
            data={
                "client_id": app.client_id,
                "client_secret": secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": microsoft.redirect_uri(settings),
                "code_verifier": verifier,
                "scope": microsoft.IMAP_SCOPE,
            },
        )
    except httpx.HTTPError as exc:
        # The CLASS NAME, never `str(exc)`: httpx embeds the request in it.
        raise OAuthFlowError(
            f"could not reach Microsoft ({type(exc).__name__})", status=502
        ) from None
    finally:
        if owns_client:
            await client.aclose()

    if response.status_code != 200:
        raise OAuthFlowError(
            _refusal(response), status=502 if response.status_code >= 500 else 400
        )

    refresh = response.json().get("refresh_token")
    if not refresh:
        raise OAuthFlowError(
            "Microsoft returned no refresh token, so this mailbox could only be "
            "read for one hour. Check that the application requests "
            "`offline_access`.",
            status=400,
        )

    credential_id = await _store_secret(
        session,
        org_id=org_id,
        actor_id=user_id,
        secret=refresh,
        vault=vault,
        existing_credential_id=mailbox.credential_id,
        name="email-mailbox-oauth-refresh",
        credential_type="email_mailbox_oauth",
    )
    mailbox.credential_id = credential_id
    mailbox.credential_state = "ok"
    # A stale "sign-in expired" under a freshly connected mailbox is the kind of
    # contradiction an operator stops trusting the console for.
    mailbox.last_error = None
    session.add(mailbox)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=user_id,
        action="email_intake.oauth_connected",
        target_type="email_mailbox",
        target_id=str(mailbox.id),
        # The MAILBOX, never the token.
        metadata={"address": mailbox.address},
    )
    return mailbox


async def abandon(
    session: AsyncSession, *, org_id: UUID, user_id: UUID, state: str
) -> None:
    """The operator cancelled at Microsoft.

    Consume the row so the `state` cannot be completed later, and leave the
    mailbox EXACTLY as it was -- a half-connected mailbox is worse than an
    unconnected one, because it looks finished.
    """
    row = await _consume(session, org_id=org_id, user_id=user_id, state=state)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=user_id,
        action="email_intake.oauth_abandoned",
        target_type="email_mailbox",
        target_id=str(row.mailbox_id),
        metadata={},
    )
```

- [ ] **Step 5: Implement the schema and the routes**

Add to `backend/app/modules/email_intake/schema.py`:

```python
class OAuthStartOut(BaseModel):
    authorize_url: str = Field(
        description="Send the browser here. Carries `state` and the PKCE challenge."
    )


class OAuthCompleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=4096)


class OAuthAbandonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str = Field(min_length=1, max_length=64)
```

and to `MailboxOut`, after `has_credential`:

```python
    #: `password` or `microsoft_oauth`.
    auth_kind: str
    #: `ok` or `needs_reconsent`. NOT an error field: `needs_reconsent` is a
    #: state with an action ("Reconnect"), and the console renders it as one.
    credential_state: str
```

Update `api._mailbox_out` to pass both — it enumerates fields by hand on purpose, so this is one line each.

Add the three routes to `backend/app/modules/email_intake/api.py`:

```python
def _oauth_error(exc: OAuthFlowError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


@router.post(
    "/email-intake/mailboxes/{mailbox_id}/oauth/start", response_model=OAuthStartOut
)
async def start_oauth_route(
    mailbox_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("email_intake.admin")),
) -> OAuthStartOut:
    """Mint a single-use authorization and return where to send the browser.

    A POST, not a GET: it writes a row and an audit event, and a GET that
    changes state is prefetchable by a browser.
    """
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        try:
            url = await oauth.start(
                session,
                org_id=ctx.org_id,
                user_id=ctx.user_id,
                mailbox_id=mailbox_id,
                settings=request.app.state.settings,
                vault=request.app.state.vault,
            )
        except microsoft.MicrosoftNotConfigured as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        except OAuthFlowError as exc:
            raise _oauth_error(exc) from exc
    return OAuthStartOut(authorize_url=url)


@router.post("/email-intake/oauth/complete", response_model=MailboxOut)
async def complete_oauth_route(
    body: OAuthCompleteIn,
    request: Request,
    ctx: AuthContext = Depends(require_permission("email_intake.admin")),
) -> MailboxOut:
    """Redeem the code the console received from Microsoft.

    THE CALLER'S OWN TOKEN IS WHAT AUTHORISES THIS. Microsoft redirected a
    browser to a CONSOLE route; this is the console's own authenticated call
    afterwards. `state` is a third check, not the tenant binding -- `org_session`
    and RLS are (design §4.3).
    """
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        try:
            mailbox = await oauth.complete(
                session,
                org_id=ctx.org_id,
                user_id=ctx.user_id,
                state=body.state,
                code=body.code,
                settings=request.app.state.settings,
                vault=request.app.state.vault,
            )
        except microsoft.MicrosoftNotConfigured as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        except OAuthFlowError as exc:
            raise _oauth_error(exc) from exc
        result = _mailbox_out(mailbox)
    return result


@router.post("/email-intake/oauth/abandon", status_code=status.HTTP_204_NO_CONTENT)
async def abandon_oauth_route(
    body: OAuthAbandonIn,
    request: Request,
    ctx: AuthContext = Depends(require_permission("email_intake.admin")),
) -> None:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        try:
            await oauth.abandon(
                session, org_id=ctx.org_id, user_id=ctx.user_id, state=body.state
            )
        except OAuthFlowError as exc:
            raise _oauth_error(exc) from exc
```

- [ ] **Step 6: Run the tests**

Run: `cd backend && .venv/bin/pytest tests/test_email_intake_oauth_flow.py -v && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: all PASS, clean.

- [ ] **Step 7: Falsify**

Change `_consume`'s `UPDATE ... RETURNING` to a `SELECT` followed by a separate `UPDATE`. Expected: `test_a_state_is_single_use` still passes serially — so **also** delete the `consumed_at.is_(None)` predicate, and confirm it goes RED. Restore both.

- [ ] **Step 8: Commit**

```bash
git add backend/app/modules/email_intake/oauth.py backend/app/modules/email_intake/schema.py \
        backend/app/modules/email_intake/api.py backend/tests/test_email_intake_oauth_flow.py
git commit -m "email-intake: the consent handshake, with state doing only CSRF"
```

---

### Task 10: Refreshing the token, and treating revocation as a state

**Files:**
- Create: `backend/app/modules/email_intake/tokens.py`
- Test: `backend/tests/test_email_intake_tokens.py`

**Interfaces:**
- Consumes: `microsoft.token_url`, `microsoft.IMAP_SCOPE`, `oauth_app.get_app`, `oauth_app.app_client_secret`, `mailboxes._store_secret`.
- Produces, consumed by Task 12:
  - `async def access_token_for(session, *, org_id: UUID, mailbox: EmailMailbox, vault: VaultService, client: httpx.AsyncClient | None = None) -> str`
  - `class TokenRevoked(Exception)` — permanent
  - `class TokenTransportError(Exception)` — transient

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_email_intake_tokens.py`. `token_env` provides a session, an org with a saved app, a connected `microsoft_oauth` mailbox whose stored refresh token is `OLD`, the vault, and `stored_refresh()` which decrypts and returns the current value.

```python
"""Refreshing an access token, and the one ordering that matters.

MICROSOFT ROTATES REFRESH TOKENS. If the new one is not persisted BEFORE the
access token is used, a crash in between leaves the mailbox holding a token the
provider has retired -- and the symptom is a mailbox that silently stops polling
weeks later, with a login failure nobody connects to a deploy.
"""

from typing import Any

import httpx
import pytest

from app.modules.email_intake.tokens import (
    TokenRevoked,
    TokenTransportError,
    access_token_for,
)

OLD = "refresh-old"
NEW = "refresh-new"


def _mock(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_a_rotated_refresh_token_is_stored_before_the_access_token_returns(
    token_env: Any,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"access_token": "at", "refresh_token": NEW, "expires_in": 3599}
        )

    token = await access_token_for(
        token_env.session,
        org_id=token_env.org_id,
        mailbox=token_env.mailbox,
        vault=token_env.vault,
        client=_mock(handler),
    )
    assert token == "at"
    assert await token_env.stored_refresh() == NEW


@pytest.mark.asyncio
async def test_a_response_without_a_new_refresh_token_keeps_the_old_one(
    token_env: Any,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "at", "expires_in": 3599})

    await access_token_for(
        token_env.session,
        org_id=token_env.org_id,
        mailbox=token_env.mailbox,
        vault=token_env.vault,
        client=_mock(handler),
    )
    assert await token_env.stored_refresh() == OLD


@pytest.mark.asyncio
async def test_invalid_grant_is_revoked_and_not_a_transport_error(
    token_env: Any,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(TokenRevoked):
        await access_token_for(
            token_env.session,
            org_id=token_env.org_id,
            mailbox=token_env.mailbox,
            vault=token_env.vault,
            client=_mock(handler),
        )


@pytest.mark.asyncio
async def test_a_network_failure_is_transient_and_not_a_revocation(
    token_env: Any,
) -> None:
    """A blip must NOT present as 'your consent was withdrawn' -- that sends an
    operator into Azure to fix a router."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(TokenTransportError):
        await access_token_for(
            token_env.session,
            org_id=token_env.org_id,
            mailbox=token_env.mailbox,
            vault=token_env.vault,
            client=_mock(handler),
        )


@pytest.mark.asyncio
async def test_a_5xx_is_transient_too(token_env: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "temporarily_unavailable"})

    with pytest.raises(TokenTransportError):
        await access_token_for(
            token_env.session,
            org_id=token_env.org_id,
            mailbox=token_env.mailbox,
            vault=token_env.vault,
            client=_mock(handler),
        )


@pytest.mark.asyncio
async def test_no_exception_message_ever_carries_token_material(
    token_env: Any,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": "invalid_grant", "error_description": f"token {OLD}"}
        )

    with pytest.raises(TokenRevoked) as caught:
        await access_token_for(
            token_env.session,
            org_id=token_env.org_id,
            mailbox=token_env.mailbox,
            vault=token_env.vault,
            client=_mock(handler),
        )
    assert OLD not in str(caught.value)
```

- [ ] **Step 2: Run it, watch it fail**

Run: `cd backend && .venv/bin/pytest tests/test_email_intake_tokens.py -v`
Expected: FAIL — the module does not exist.

- [ ] **Step 3: Implement**

Create `backend/app/modules/email_intake/tokens.py`:

```python
"""Turning a stored refresh token into an access token, at point of use.

THE ACCESS TOKEN IS NEVER STORED. It is fetched per poll, handed to the IMAP
connection, and dropped -- the same lifetime the password has today.

THE REFRESH TOKEN IS STORED BEFORE IT IS USED. Microsoft may return a NEW
refresh token with every exchange; the new one is written to the vault before
this function returns, so a crash between the exchange and the IMAP connection
costs one poll rather than the mailbox.

NOTHING HERE EVER PUTS TOKEN MATERIAL IN AN EXCEPTION. The provider's
`error_description` routinely quotes the token it refused, so only the machine
`error` CODE is read and the description is discarded (invariant #3).

TWO EXCEPTION CLASSES, AND THE DIFFERENCE IS THE WHOLE POINT. `TokenRevoked`
means a person must reconnect the mailbox; `TokenTransportError` means try
again next interval. Collapsing them would either send an operator into Azure
to fix a router, or hide a dead grant behind a retry loop.
"""

from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.vault import VaultService
from app.modules.email_intake import microsoft, oauth_app
from app.modules.email_intake.mailboxes import _store_secret
from app.modules.email_intake.models import EmailMailbox

__all__ = ["TokenRevoked", "TokenTransportError", "access_token_for"]

_TIMEOUT = 15.0

#: Provider error codes that mean "this will never work again without a human".
_TERMINAL = frozenset(
    {"invalid_grant", "invalid_client", "unauthorized_client", "consent_required"}
)


class TokenRevoked(Exception):
    """Permanent. The mailbox needs a person to reconnect it; retrying is noise."""


class TokenTransportError(Exception):
    """Transient. The next poll tries again and `credential_state` is untouched."""


async def access_token_for(
    session: AsyncSession,
    *,
    org_id: UUID,
    mailbox: EmailMailbox,
    vault: VaultService,
    client: httpx.AsyncClient | None = None,
) -> str:
    app = await oauth_app.get_app(session, org_id=org_id)
    if app is None:
        raise TokenRevoked(
            "the Microsoft application for this organisation has been removed"
        )
    if mailbox.credential_id is None:
        raise TokenRevoked("this mailbox has never been connected to Microsoft")

    refresh = await vault.checkout(
        session, org_id=org_id, credential_id=mailbox.credential_id
    )
    secret = await oauth_app.app_client_secret(session, org_id=org_id, vault=vault)

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        response = await client.post(
            microsoft.token_url(app.tenant_id),
            data={
                "client_id": app.client_id,
                "client_secret": secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "scope": microsoft.IMAP_SCOPE,
            },
        )
    except httpx.HTTPError as exc:
        # The CLASS NAME, never `str(exc)` -- httpx embeds the request in it.
        raise TokenTransportError(type(exc).__name__) from None
    finally:
        if owns_client:
            await client.aclose()

    if response.status_code != 200:
        try:
            code = str(response.json().get("error", ""))
        except ValueError:
            code = ""
        if code in _TERMINAL:
            raise TokenRevoked(f"Microsoft refused the stored sign-in ({code})")
        raise TokenTransportError(f"the token endpoint answered {response.status_code}")

    body = response.json()
    rotated = body.get("refresh_token")
    if rotated and rotated != refresh:
        # BEFORE the access token leaves this function. See the module docstring.
        await _store_secret(
            session,
            org_id=org_id,
            actor_id=mailbox.created_by,
            secret=rotated,
            vault=vault,
            existing_credential_id=mailbox.credential_id,
            name="email-mailbox-oauth-refresh",
            credential_type="email_mailbox_oauth",
        )
        await session.flush()

    token = body.get("access_token")
    if not token:
        raise TokenTransportError("the token endpoint returned no access token")
    return str(token)
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && .venv/bin/pytest tests/test_email_intake_tokens.py -v && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: PASS, clean.

- [ ] **Step 5: Falsify**

Move the rotation write to after the `return`. Expected: the first test RED. Restore.

- [ ] **Step 6: Commit**

```bash
git add backend/app/modules/email_intake/tokens.py backend/tests/test_email_intake_tokens.py
git commit -m "email-intake: refresh tokens, rotated and persisted before use"
```

---

### Task 11: XOAUTH2 on the IMAP wire

**Files:**
- Modify: `backend/app/modules/email_intake/imap.py`
- Test: `backend/tests/test_email_intake_imap.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `imap_client_factory(..., access_token: str | None = None)` and the matching widening of the `ImapClientFactory` Protocol's `__call__`. When `access_token` is not `None`, `password` is ignored and `AUTHENTICATE XOAUTH2` replaces `LOGIN`. Task 12 passes it.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_email_intake_imap.py`, extending that file's existing socket-server harness with `expect_authenticate_ok()`, `expect_authenticate_challenge_then_no()` and a `_sasl_argument(wire)` helper that returns the base64 blob following `AUTHENTICATE XOAUTH2`.

```python
import base64


def test_xoauth2_sends_the_exact_sasl_string_and_never_the_refresh_token(
    fake_imap_server: Any,
) -> None:
    """THE PROTOCOL, not a mock. This is what a passive listener would have
    seen, which is the only level at which 'the refresh token is not on this
    wire' can actually be asserted."""
    fake_imap_server.expect_authenticate_ok()
    client = imap_client_factory(
        host=fake_imap_server.host,
        port=fake_imap_server.port,
        use_ssl=True,
        folder="INBOX",
        username="support@toeshee.com",
        password="",
        access_token="ACCESS-TOKEN",
    )
    client.close()

    wire = fake_imap_server.captured()
    assert b"AUTHENTICATE XOAUTH2" in wire
    assert b"LOGIN" not in wire
    assert base64.b64decode(_sasl_argument(wire)) == (
        b"user=support@toeshee.com\x01auth=Bearer ACCESS-TOKEN\x01\x01"
    )
    assert b"refresh" not in wire.lower()


def test_a_refused_xoauth2_sends_the_empty_continuation_and_does_not_hang(
    fake_imap_server: Any,
) -> None:
    """Microsoft answers a failed AUTHENTICATE with a base64 JSON challenge and
    then WAITS. A client that does not send the empty response line hangs until
    the conversation deadline -- once per poll interval, forever."""
    fake_imap_server.expect_authenticate_challenge_then_no()
    with pytest.raises(ImapError) as caught:
        imap_client_factory(
            host=fake_imap_server.host,
            port=fake_imap_server.port,
            use_ssl=True,
            folder="INBOX",
            username="support@toeshee.com",
            password="",
            access_token="ACCESS-TOKEN",
            deadline_s=5.0,
        )
    assert caught.value.stage == "login"
    assert caught.value.reason == "rejected"
    # The server received the empty continuation and was not left waiting.
    assert fake_imap_server.saw_empty_continuation()
    # The provider's JSON blob is not copied into the message.
    assert "{" not in str(caught.value)


def test_a_token_is_never_followed_by_a_password_attempt(
    fake_imap_server: Any,
) -> None:
    """No fallback. Falling back would send a second credential to a server that
    just rejected the first."""
    fake_imap_server.expect_authenticate_challenge_then_no()
    with pytest.raises(ImapError):
        imap_client_factory(
            host=fake_imap_server.host,
            port=fake_imap_server.port,
            use_ssl=True,
            folder="INBOX",
            username="u",
            password="a-real-password",
            access_token="ACCESS-TOKEN",
            deadline_s=5.0,
        )
    assert b"a-real-password" not in fake_imap_server.captured()


def test_the_password_path_is_untouched(fake_imap_server: Any) -> None:
    fake_imap_server.expect_login_ok()
    client = imap_client_factory(
        host=fake_imap_server.host,
        port=fake_imap_server.port,
        use_ssl=True,
        folder="INBOX",
        username="u",
        password="p",
    )
    client.close()
    wire = fake_imap_server.captured()
    assert b"LOGIN" in wire
    assert b"XOAUTH2" not in wire
```

- [ ] **Step 2: Run it, watch it fail**

Run: `cd backend && .venv/bin/pytest tests/test_email_intake_imap.py -v`
Expected: FAIL — `imap_client_factory` has no `access_token` parameter.

- [ ] **Step 3: Implement**

In `backend/app/modules/email_intake/imap.py`, add beside `_login`:

```python
def _xoauth2(conn: imaplib.IMAP4, username: str, access_token: str) -> None:
    """SASL XOAUTH2 -- the wire format Microsoft and Google both use.

    `from None` throughout, for the same reason `_login` does it: the server's
    refusal line quotes what it refused, and here that is a bearer token.

    THE EMPTY CONTINUATION IS NOT OPTIONAL. On failure the server sends a
    base64 JSON error blob as a CHALLENGE and waits for a response before it
    will send its `NO`. `imaplib.authenticate` drives that exchange for us as
    long as the callback answers the second challenge with `b""` -- without it
    the connection sits until `_ConversationDeadline` closes it, once per poll
    interval.
    """
    sasl = f"user={username}\x01auth=Bearer {access_token}\x01\x01".encode()
    sent = False

    def respond(challenge: bytes) -> bytes:
        nonlocal sent
        if not sent:
            sent = True
            return sasl
        # The challenge here is the provider's base64 JSON error. DISCARDED
        # rather than surfaced: it adds nothing an operator can act on, and it
        # is remote-controlled text headed for `last_error`.
        return b""

    try:
        conn.authenticate("XOAUTH2", respond)
    except imaplib.IMAP4.error:
        raise ImapError(
            "the Microsoft sign-in was refused", stage="login", reason="rejected"
        ) from None
    except OSError:
        raise ImapError(
            "the connection failed during sign-in", stage="login", reason="network"
        ) from None
```

Widen the factory's signature:

```python
def imap_client_factory(
    *,
    host: str,
    port: int,
    use_ssl: bool,
    folder: str,
    username: str,
    password: str,
    deadline_s: float | None = None,
    access_token: str | None = None,
) -> ImapClient:
```

and replace the `_login(conn, username, password)` call inside it with:

```python
        if access_token is not None:
            # THE TOKEN WINS AND THE PASSWORD IS NOT CONSULTED. There is no
            # path here that tries a token and falls back to a password: a
            # fallback would send a second credential to a server that just
            # rejected the first.
            _xoauth2(conn, username, access_token)
        else:
            _login(conn, username, password)
```

Add the same optional keyword to the `ImapClientFactory` Protocol's `__call__`, so the typed seam matches and `mypy` accepts the poller's call in Task 12.

Extend the factory docstring with one paragraph:

```
    `access_token`, when given, selects SASL XOAUTH2 and `password` is ignored.
    Both paths verify the certificate and both refuse to send a credential if
    they cannot -- `access_token` chooses WHICH credential, never whether the
    channel is authenticated.
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && .venv/bin/pytest tests/test_email_intake_imap.py -v && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: PASS, clean.

- [ ] **Step 5: Falsify**

Make `respond` return `sasl` on every call. Expected: the continuation test RED — either on `saw_empty_continuation()` or by hanging to the 5-second deadline, which is the same finding. Restore.

- [ ] **Step 6: Commit**

```bash
git add backend/app/modules/email_intake/imap.py backend/tests/test_email_intake_imap.py
git commit -m "email-intake/imap: XOAUTH2, with the failure continuation the protocol requires"
```

---

### Task 12: The poller picks a path, and records a revocation

**Files:**
- Modify: `backend/app/modules/email_intake/poller.py`
- Test: `backend/tests/test_email_intake_poller.py`

**Interfaces:**
- Consumes: `tokens.access_token_for`, `TokenRevoked`, `TokenTransportError` (Task 10); `imap_client_factory(..., access_token=)` (Task 11).
- Produces: `_MailboxPlan` gains `auth_kind`, `credential_state` and `access_token` slots; `_mark_needs_reconsent`. Task 13's console reads the `credential_state` this writes.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_email_intake_poller.py`. `poller_env` extends the file's existing poller fixture with: `oauth_mailbox_id` (connected, `auth_kind="microsoft_oauth"`), `password_mailbox_id`, `token_revoked()` / `token_unreachable()` (install the corresponding mock token endpoint), `unreachable_factory` (a client factory that raises if called), `connections_opened`, `token_exchanges`, `reset_counters()`, `reload(id)` and `audit_actions()`.

```python
@pytest.mark.asyncio
async def test_an_oauth_mailbox_is_authenticated_with_a_token_not_a_password(
    poller_env: Any,
) -> None:
    seen: dict[str, Any] = {}

    def factory(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return poller_env.fake_client()

    await poll_mailbox(
        poller_env.org_id,
        poller_env.oauth_mailbox_id,
        engine=poller_env.engine,
        vault=poller_env.vault,
        client_factory=factory,
    )
    assert seen["access_token"] == "access-token"
    assert seen["password"] == ""


@pytest.mark.asyncio
async def test_a_revoked_consent_sets_needs_reconsent_and_says_so(
    poller_env: Any,
) -> None:
    poller_env.token_revoked()
    await poll_mailbox(
        poller_env.org_id,
        poller_env.oauth_mailbox_id,
        engine=poller_env.engine,
        vault=poller_env.vault,
        client_factory=poller_env.unreachable_factory,
    )
    row = await poller_env.reload(poller_env.oauth_mailbox_id)
    assert row.credential_state == "needs_reconsent"
    assert "reconnect" in (row.last_error or "").lower()
    assert "email_intake.oauth_revoked" in await poller_env.audit_actions()
    # It never opened a connection: there was no credential worth presenting.
    assert poller_env.connections_opened == 0
    # And the claim was released, not left to time out.
    assert row.claimed_at is None


@pytest.mark.asyncio
async def test_a_mailbox_needing_reconsent_is_not_retried(poller_env: Any) -> None:
    poller_env.token_revoked()
    await poll_mailbox(
        poller_env.org_id,
        poller_env.oauth_mailbox_id,
        engine=poller_env.engine,
        vault=poller_env.vault,
        client_factory=poller_env.unreachable_factory,
    )
    poller_env.reset_counters()
    await poll_mailbox(
        poller_env.org_id,
        poller_env.oauth_mailbox_id,
        engine=poller_env.engine,
        vault=poller_env.vault,
        client_factory=poller_env.unreachable_factory,
    )
    # Retrying a dead refresh token is noise, not resilience -- and the claim
    # query is where that is enforced, not a branch in every caller.
    assert poller_env.token_exchanges == 0


@pytest.mark.asyncio
async def test_a_network_blip_does_not_look_like_a_revocation(
    poller_env: Any,
) -> None:
    poller_env.token_unreachable()
    await poll_mailbox(
        poller_env.org_id,
        poller_env.oauth_mailbox_id,
        engine=poller_env.engine,
        vault=poller_env.vault,
        client_factory=poller_env.unreachable_factory,
    )
    row = await poller_env.reload(poller_env.oauth_mailbox_id)
    assert row.credential_state == "ok"
    assert "reconnect" not in (row.last_error or "").lower()


@pytest.mark.asyncio
async def test_the_password_path_is_unchanged(poller_env: Any) -> None:
    seen: dict[str, Any] = {}

    def factory(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return poller_env.fake_client()

    await poll_mailbox(
        poller_env.org_id,
        poller_env.password_mailbox_id,
        engine=poller_env.engine,
        vault=poller_env.vault,
        client_factory=factory,
    )
    assert seen.get("access_token") is None
    assert seen["password"] != ""


@pytest.mark.asyncio
async def test_an_oauth_poll_writes_a_credential_used_event(poller_env: Any) -> None:
    """Invariant #4 does not get weaker because the secret changed shape:
    'which process decrypted this mailbox's refresh token, and when' must stay
    answerable."""
    await poll_mailbox(
        poller_env.org_id,
        poller_env.oauth_mailbox_id,
        engine=poller_env.engine,
        vault=poller_env.vault,
        client_factory=lambda **kw: poller_env.fake_client(),
    )
    assert "email_intake.credential_used" in await poller_env.audit_actions()
```

- [ ] **Step 2: Run it, watch it fail**

Run: `cd backend && .venv/bin/pytest tests/test_email_intake_poller.py -v`
Expected: FAIL — the poller has no token path.

- [ ] **Step 3: Implement**

In `poller.py`, extend `_MailboxPlan.__slots__` and `__init__`:

```python
    __slots__ = (
        "address",
        "host",
        "port",
        "use_ssl",
        "folder",
        "username",
        "credential_id",
        "password",
        "auth_kind",
        "credential_state",
        # Fetched per poll, never stored, cleared in the same `finally` the
        # password is. Its lifetime is one IMAP handshake.
        "access_token",
    )
```

```python
        self.credential_id = mailbox.credential_id
        self.password = ""
        self.auth_kind = mailbox.auth_kind
        self.credential_state = mailbox.credential_state
        self.access_token = ""
```

Add to `_claim`'s SELECT predicate list:

```python
                    # A mailbox waiting for a person to reconnect it is not
                    # eligible for a scheduled poll. THIS is what makes "stops
                    # trying" a property of the claim rather than a branch every
                    # caller has to remember to write.
                    EmailMailbox.credential_state == "ok",
```

Add a state writer beside `_release`:

```python
async def _mark_needs_reconsent(
    org_id: UUID, mailbox_id: UUID, message: str, *, engine: AsyncEngine
) -> None:
    """A dead refresh token is a STATE, not an error to retry.

    The console shows Reconnect; the poller stops trying. Retrying a revoked
    grant every interval produces a log full of failures nobody can act on and
    buries the one thing an operator needs to see.

    The claim is RELEASED here (`claimed_at=None`) rather than left to the
    stuck-claim sweep: this poll ended, it just ended without a connection.
    """
    async with org_session(org_id, engine=engine) as session:
        await session.execute(
            update(EmailMailbox)
            .where(EmailMailbox.org_id == org_id, EmailMailbox.id == mailbox_id)
            .values(
                credential_state="needs_reconsent",
                last_error=_sanitize_excerpt(message),
                claimed_at=None,
            )
        )
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="system",
            actor_id=None,
            action="email_intake.oauth_revoked",
            target_type="email_mailbox",
            target_id=str(mailbox_id),
            metadata={},
        )
```

In `poll_mailbox`, replace the single `plan.password = await _checkout_credential(...)` line with the branch:

```python
            if plan.auth_kind == "microsoft_oauth":
                revoked = ""
                async with org_session(org_id, engine=engine) as session:
                    mailbox = (
                        await session.execute(
                            select(EmailMailbox).where(
                                EmailMailbox.org_id == org_id,
                                EmailMailbox.id == mailbox_id,
                            )
                        )
                    ).scalar_one()
                    try:
                        plan.access_token = await tokens.access_token_for(
                            session, org_id=org_id, mailbox=mailbox, vault=vault
                        )
                    except TokenRevoked as exc:
                        revoked = str(exc)
                    # THE AUDIT EVENT IS WRITTEN EITHER WAY. A checkout that was
                    # attempted and refused is still a credential access, and
                    # invariant #4's question is "what touched this secret",
                    # not "what succeeded".
                    await audit_service.record(
                        session,
                        org_id=org_id,
                        actor_type="system",
                        actor_id=None,
                        action="email_intake.credential_used",
                        target_type="credential",
                        target_id=str(plan.credential_id),
                        metadata={"mailbox_id": str(mailbox_id)},
                    )
                if revoked:
                    await _mark_needs_reconsent(
                        org_id,
                        mailbox_id,
                        "Microsoft sign-in is no longer valid — reconnect this mailbox",
                        engine=engine,
                    )
                    return
            else:
                plan.password = await _checkout_credential(
                    org_id, mailbox_id, plan.credential_id, engine=engine, vault=vault
                )
```

Pass the token to the factory:

```python
                password=plan.password,
                access_token=plan.access_token or None,
```

and clear it wherever the password is cleared:

```python
                plan.password = ""
                plan.access_token = ""
```

Do the same in `test_mailbox`, which reuses this module's checkout and factory —
including the `actor_id` it already passes, so a manual test of an OAuth mailbox
still records *which admin* caused the decrypt.

`TokenTransportError` is deliberately **not** caught here: it falls through to
the module's existing mailbox-level failure handling, which writes a sanitized
`last_error` and leaves `credential_state` alone. That is the design's §4.5
"transient" row, and it needs no new code.

- [ ] **Step 4: Run the tests**

Run: `cd backend && .venv/bin/pytest tests/test_email_intake_poller.py tests/test_email_intake_tokens.py -v && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: PASS, clean.

- [ ] **Step 5: Falsify**

Remove the `credential_state == "ok"` predicate from `_claim`. Expected: `test_a_mailbox_needing_reconsent_is_not_retried` RED. Restore.

- [ ] **Step 6: Commit**

```bash
git add backend/app/modules/email_intake/poller.py backend/tests/test_email_intake_poller.py
git commit -m "email-intake: poll with a token where one is configured, and stop when consent dies"
```

---

### Task 13: Connect with Microsoft, in the console

**Files:**
- Modify: `frontend/src/modules/email-intake/types.ts`
- Modify: `frontend/src/modules/email-intake/api.ts`
- Modify: `frontend/src/modules/email-intake/MailboxForm.tsx`
- Modify: `frontend/src/modules/email-intake/MailboxList.tsx`
- Modify: `frontend/src/modules/email-intake/EmailIntakeAdminPage.tsx` (render the new panel)
- Modify: `frontend/src/modules/email-intake/fixtures.ts`
- Create: `frontend/src/modules/email-intake/OAuthCallback.tsx`
- Create: `frontend/src/modules/email-intake/MicrosoftConnection.tsx`
- Modify: `frontend/src/App.tsx` (one route)
- Test: `frontend/src/modules/email-intake/__tests__/oauth.test.tsx`

**Interfaces:** the wire shapes from Tasks 8–9 exactly —
`GET/PUT /email-intake/oauth-app` ↔ `{ tenant_id, client_id, has_secret, redirect_uri }` / `{ tenant_id, client_id, client_secret? }`;
`POST /email-intake/mailboxes/{id}/oauth/start` → `{ authorize_url }`;
`POST /email-intake/oauth/complete` `{ state, code }` → `MailboxOut`;
`POST /email-intake/oauth/abandon` `{ state }` → 204.
`MailboxOut` now carries `auth_kind: string` and `credential_state: string`.

- [ ] **Step 1: Write the failing test**

```tsx
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { renderCallbackAt, renderIntake } from "./harness";

describe("Connect with Microsoft", () => {
  it("sends the browser to the authorize URL the server returned", async () => {
    const assign = vi.fn();
    renderIntake({
      mailboxes: [
        { id: "m1", auth_kind: "microsoft_oauth", has_credential: false, active: false },
      ],
      oauthApp: {
        tenant_id: "t",
        client_id: "c",
        has_secret: true,
        redirect_uri: "https://x/settings/email-intake/oauth/callback",
      },
      locationAssign: assign,
    });
    await userEvent.click(
      await screen.findByRole("button", { name: /Connect with Microsoft/i }),
    );
    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith(expect.stringContaining("authorize")),
    );
  });

  it("completes from the callback and strips the code from the address bar", async () => {
    const replace = vi.fn();
    renderCallbackAt("?code=abc&state=xyz", { historyReplace: replace });
    await waitFor(() => expect(replace).toHaveBeenCalled());
    expect(await screen.findByText(/Connected/i)).toBeInTheDocument();
    // The authorization code must not survive in history, in a bookmark, or in
    // a screenshot of the operator's screen.
    expect(String(replace.mock.calls[0]?.[2])).not.toContain("abc");
  });

  it("abandons rather than completing when the user cancelled", async () => {
    const { requests } = renderCallbackAt("?error=access_denied&state=xyz");
    await waitFor(() =>
      expect(requests.some((r) => r.url.includes("abandon"))).toBe(true),
    );
    expect(requests.some((r) => r.url.includes("complete"))).toBe(false);
    expect(await screen.findByText(/was not completed/i)).toBeInTheDocument();
  });

  it("posts the completion exactly once even under a double mount", async () => {
    const { requests } = renderCallbackAt("?code=abc&state=xyz", { strictMode: true });
    await waitFor(() => expect(screen.getByText(/Connected/i)).toBeInTheDocument());
    expect(requests.filter((r) => r.url.includes("complete"))).toHaveLength(1);
  });

  it("offers Reconnect, not an error, when consent is gone", async () => {
    renderIntake({
      mailboxes: [
        {
          id: "m1",
          auth_kind: "microsoft_oauth",
          has_credential: true,
          credential_state: "needs_reconsent",
        },
      ],
    });
    expect(await screen.findByText(/Microsoft sign-in expired/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Reconnect/i })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("does not offer a password box for an OAuth mailbox", async () => {
    renderIntake({});
    await userEvent.click(await screen.findByRole("button", { name: /Add mailbox/i }));
    await userEvent.click(screen.getByRole("button", { name: /^Microsoft$/i }));
    expect(screen.queryByLabelText(/^Password$/i)).not.toBeInTheDocument();
  });

  it("shows the redirect URI to register, and never echoes the client secret", async () => {
    renderIntake({
      oauthApp: {
        tenant_id: "t",
        client_id: "c",
        has_secret: true,
        redirect_uri: "https://x/settings/email-intake/oauth/callback",
      },
    });
    expect(
      await screen.findByText(/https:\/\/x\/settings\/email-intake\/oauth\/callback/),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/Client secret/i)).toHaveValue("");
  });
});
```

- [ ] **Step 2: Run it, watch it fail**

Run: `cd frontend && npx vitest run src/modules/email-intake`
Expected: FAIL — none of these controls exist.

- [ ] **Step 3: Implement the types**

In `types.ts`, add to `MailboxOut` and add three interfaces:

```ts
  /** `password` or `microsoft_oauth`. */
  auth_kind: string;
  /**
   * `ok` or `needs_reconsent`. `needs_reconsent` is a STATE with an action,
   * not an error — the row shows "Reconnect", not a red alert.
   */
  credential_state: string;
```

```ts
export interface OAuthAppOut {
  tenant_id: string;
  client_id: string;
  has_secret: boolean;
  /** Register this exact string as a redirect URI on the Azure application. */
  redirect_uri: string;
}

export interface OAuthAppUpdate {
  tenant_id: string;
  client_id: string;
  /** Only when the operator typed one. Absent means "keep the stored one". */
  client_secret?: string;
}

export interface OAuthStartOut {
  authorize_url: string;
}
```

- [ ] **Step 4: Implement the api layer**

In `api.ts`, following that file's existing `useQuery`/`useMutation` shape and its `MAILBOXES_KEY`:

```ts
export const OAUTH_APP_KEY = "email-intake-oauth-app";

export function useOAuthApp(enabled: boolean) {
  return useQuery<OAuthAppOut, ApiError>({
    queryKey: [OAUTH_APP_KEY],
    queryFn: () => api.getOAuthApp(),
    enabled,
  });
}

export function useSaveOAuthAppMutation() {
  const qc = useQueryClient();
  return useMutation<OAuthAppOut, ApiError, OAuthAppUpdate>({
    mutationFn: (body) => api.saveOAuthApp(body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: [OAUTH_APP_KEY] }),
  });
}

export function useStartOAuthMutation() {
  return useMutation<OAuthStartOut, ApiError, string>({
    mutationFn: (mailboxId) => api.startMailboxOAuth(mailboxId),
  });
}

export function useCompleteOAuthMutation() {
  const qc = useQueryClient();
  return useMutation<MailboxOut, ApiError, { state: string; code: string }>({
    mutationFn: (body) => api.completeMailboxOAuth(body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: [MAILBOXES_KEY] }),
  });
}

export function useAbandonOAuthMutation() {
  return useMutation<void, ApiError, { state: string }>({
    mutationFn: (body) => api.abandonMailboxOAuth(body),
  });
}
```

with the five matching methods on the api client in `frontend/src/lib/api.ts`, spelled the way its neighbours are:

```ts
  getOAuthApp = () => this.get<OAuthAppOut>("/email-intake/oauth-app");
  saveOAuthApp = (body: OAuthAppUpdate) =>
    this.put<OAuthAppOut>("/email-intake/oauth-app", body);
  startMailboxOAuth = (id: string) =>
    this.post<OAuthStartOut>(`/email-intake/mailboxes/${id}/oauth/start`, {});
  completeMailboxOAuth = (body: { state: string; code: string }) =>
    this.post<MailboxOut>("/email-intake/oauth/complete", body);
  abandonMailboxOAuth = (body: { state: string }) =>
    this.post<void>("/email-intake/oauth/abandon", body);
```

- [ ] **Step 5: Implement the callback route**

Create `frontend/src/modules/email-intake/OAuthCallback.tsx`:

```tsx
// Where Microsoft sends the browser back.
//
// THIS IS A CONSOLE ROUTE, NOT AN API ROUTE, AND THAT IS THE WHOLE SECURITY
// ARGUMENT. A redirect arrives with no Authorization header; an API route
// receiving it would have to authorise itself from `state` alone, making one
// string the CSRF token, the tenant binding and the authentication at once.
// Instead the browser lands here and the console makes an ORDINARY
// AUTHENTICATED CALL — same bearer token, same RBAC gate, same RLS-scoped
// session as every other write on this page. See
// docs/superpowers/specs/2026-09-11-mail-oauth-design.md §4.3.
//
// THE CODE IS STRIPPED FROM THE ADDRESS BAR IMMEDIATELY. It is single-use and
// short-lived, but it has no business surviving in history, in a bookmark, or
// in a screenshot of somebody's screen.

import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { useAbandonOAuthMutation, useCompleteOAuthMutation } from "./api";

type Phase = "working" | "done" | "cancelled" | "failed";

export function OAuthCallback() {
  const complete = useCompleteOAuthMutation();
  const abandon = useAbandonOAuthMutation();
  const navigate = useNavigate();
  const [phase, setPhase] = useState<Phase>("working");
  const [reason, setReason] = useState("");
  // StrictMode mounts effects twice in development. The state is SINGLE USE
  // server-side, so a second POST is a 409 — harmless to the data, but it
  // would show the operator a failure for a connection that worked.
  const fired = useRef(false);

  useEffect(() => {
    if (fired.current) return;
    fired.current = true;

    const query = new URLSearchParams(window.location.search);
    const state = query.get("state") ?? "";
    const code = query.get("code");
    const error = query.get("error");
    window.history.replaceState({}, "", window.location.pathname);

    if (!state) {
      setPhase("failed");
      setReason("That link did not come from a sign-in this console started.");
      return;
    }
    if (error || !code) {
      abandon.mutate({ state }, { onSettled: () => setPhase("cancelled") });
      return;
    }
    complete.mutate(
      { state, code },
      {
        onSuccess: () => setPhase("done"),
        onError: (err) => {
          setPhase("failed");
          setReason(err.message);
        },
      },
    );
    // Deliberately once, on mount: the query string is read from the address
    // bar and erased in the same tick, so a re-run would find nothing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="panel" style={{ maxWidth: 520, margin: "80px auto" }}>
      {phase === "working" ? (
        <p className="note">Finishing the Microsoft sign-in…</p>
      ) : null}
      {phase === "done" ? (
        <p role="status">Connected. This mailbox can now be polled.</p>
      ) : null}
      {phase === "cancelled" ? (
        <p role="status">
          The Microsoft sign-in was not completed, so nothing changed. You can start
          it again whenever you are ready.
        </p>
      ) : null}
      {phase === "failed" ? (
        <p role="alert" className="form-error">
          {reason}
        </p>
      ) : null}
      {phase !== "working" ? (
        <button className="btn" onClick={() => navigate("/email-intake")}>
          Back to mailboxes
        </button>
      ) : null}
    </div>
  );
}
```

In `frontend/src/App.tsx`, add the route **inside the authenticated `<Routes>`** — the completing call needs the bearer token, so this must not be a public route:

```tsx
          <Route path="/settings/email-intake/oauth/callback" element={<OAuthCallback />} />
```

- [ ] **Step 6: Implement the panel and the list**

`MicrosoftConnection.tsx` — a `Panel` titled **Microsoft connection**, rendered by `EmailIntakeAdminPage` above the mailbox list, holding:

- `Directory (tenant) id` and `Application (client id)` text inputs, seeded from `useOAuthApp`;
- a `Client secret` password input that **always starts empty**, is sent only when non-empty, and whose help text reads `Client secret: set. Leave the box blank to keep it.` when `has_secret`, and `Passed to Microsoft when a mailbox connects, and stored encrypted. This console can never show it to you.` otherwise;
- the `redirect_uri` rendered as read-only copyable text with the help line **"Register this exact address as a redirect URI on the Azure application."**;
- a Save button wired to `useSaveOAuthAppMutation`, using the page's existing `SaveBar`-equivalent.

`MailboxForm.tsx`:

- on **create**, a two-chip `Sign-in` choice (`Password` / `Microsoft`) that sets `auth_kind` in the body; when `Microsoft` is chosen the password field is **not rendered** and `password` is not sent;
- on **edit**, the choice is **not offered**. Changing a connected mailbox's sign-in method is a disconnect followed by a reconnect; pretending it is one field invites a mailbox that is in neither state. Say so in a one-line hint rather than silently hiding the control.

`MailboxList.tsx`, per row where `auth_kind === "microsoft_oauth"`:

| condition | chip | button |
| --- | --- | --- |
| `has_credential === false` | **Not connected** | **Connect with Microsoft** |
| `credential_state === "needs_reconsent"` | **Microsoft sign-in expired** | **Reconnect** |
| otherwise | **Connected** | **Disconnect** |

**The middle row is not styled as an error** — no `role="alert"`, no red. It is a
state with an action, and rendering it as a failure teaches the operator to
ignore the row rather than click the button.

Connect and Reconnect both do:

```tsx
start.mutate(mailbox.id, {
  onSuccess: (data) => window.location.assign(data.authorize_url),
});
```

Disconnect uses the existing mailbox update path to clear the credential and
deactivate the mailbox; it does **not** fall back to a password, because there
is none, and a mailbox that appeared to keep working after a disconnect would be
worse than one that stopped.

Add `auth_kind: "password"` and `credential_state: "ok"` to every mailbox
fixture in `fixtures.ts`.

- [ ] **Step 7: Run the tests**

Run: `cd frontend && npx vitest run src/modules/email-intake && npm run typecheck && npm run build`
Expected: PASS, clean.

- [ ] **Step 8: Falsify**

Remove the `history.replaceState` call. Expected: the second test RED. Restore.

- [ ] **Step 9: Commit**

```bash
git add frontend/src/modules/email-intake frontend/src/lib/api.ts frontend/src/App.tsx
git commit -m "email-intake: Connect with Microsoft, and Reconnect as a state rather than an error"
```

---

### Task 14: The leak sweep, the acceptance checklist, and the merge

**Files:**
- Create: `backend/tests/test_email_intake_oauth_secrecy.py`
- Modify: `deploy/README.md`

**Interfaces:**
- Consumes: every route and service from Tasks 6–13.

- [ ] **Step 1: Write the sweep**

Create `backend/tests/test_email_intake_oauth_secrecy.py`. `connected_env` builds an org with a saved Azure app whose secret is `CLIENT_SECRET`, and a connected OAuth mailbox whose refresh token is `REFRESH`.

```python
"""ONE FILE THAT LOOKS FOR THE LITERALS, everywhere they could surface.

Each module has its own assertion; this is the backstop that catches the surface
nobody thought of. It scans RAW BYTES rather than checking for the absence of a
key, because a renamed field satisfies the weaker assertion while still shipping
the value (the form `test_realm_settings_api.py` established).

A FAILURE HERE IS A FINDING, NOT A TEST BUG. Fix the leak, not the assertion.
"""

import json
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

REFRESH = "REFRESH-TOKEN-SENTINEL"
CLIENT_SECRET = "CLIENT-SECRET-SENTINEL"


@pytest.mark.asyncio
async def test_no_route_on_this_module_returns_either_secret(
    connected_env: Any,
) -> None:
    paths = [
        "/api/v1/email-intake/mailboxes",
        f"/api/v1/email-intake/mailboxes/{connected_env.mailbox_id}",
        "/api/v1/email-intake/oauth-app",
        "/api/v1/email-intake/messages",
    ]
    for path in paths:
        response = await connected_env.client.get(path)
        assert REFRESH.encode() not in response.content, path
        assert CLIENT_SECRET.encode() not in response.content, path


@pytest.mark.asyncio
async def test_neither_secret_is_in_any_column_in_plaintext(
    connected_env: Any, admin_engine: AsyncEngine
) -> None:
    async with AsyncSession(admin_engine) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT ciphertext FROM credentials "
                    "UNION ALL SELECT last_error FROM email_mailbox "
                    "UNION ALL SELECT sealed_verifier FROM email_oauth_authorization"
                )
            )
        ).scalars().all()
    for value in rows:
        assert REFRESH not in (value or "")
        assert CLIENT_SECRET not in (value or "")


@pytest.mark.asyncio
async def test_neither_secret_reaches_the_audit_chain(
    connected_env: Any, admin_engine: AsyncEngine
) -> None:
    async with AsyncSession(admin_engine) as session:
        rows = (
            await session.execute(text("SELECT metadata FROM audit_events"))
        ).scalars().all()
    dump = json.dumps(list(rows), default=str)
    assert REFRESH not in dump
    assert CLIENT_SECRET not in dump


@pytest.mark.asyncio
async def test_a_failing_poll_never_writes_a_secret_to_last_error(
    connected_env: Any,
) -> None:
    """The IMAP server's refusal line routinely echoes what it refused."""
    await connected_env.poll_against_a_server_that_echoes_the_credential()
    row = await connected_env.reload()
    assert REFRESH not in (row.last_error or "")
    assert len(row.last_error or "") <= 500
```

- [ ] **Step 2: Run it**

Run: `cd backend && .venv/bin/pytest tests/test_email_intake_oauth_secrecy.py -v`
Expected: PASS.

- [ ] **Step 3: Write the acceptance checklist**

Append to `deploy/README.md`:

```markdown
## Microsoft OAuth for the support mailbox — acceptance (run once, against the real tenant)

⚠ **UNVERIFIED against a live Microsoft tenant**, on the same terms as the
outgoing half above. `MockTransport` answers what the tests say.

**Before any of this**, set `ITS_PUBLIC_CONSOLE_URL` in `.env` to the exact
browser-facing origin of the console (scheme, no trailing slash) and register
`<that origin>/settings/email-intake/oauth/callback` as a redirect URI on the
Azure application. A mismatch is refused by Microsoft with `AADSTS50011` before
anything else happens.

1. Email Intake → **Microsoft connection**: directory id, application id, client
   secret. Save. The panel shows the redirect URI to register.
2. Add a mailbox with **Sign-in: Microsoft**. It saves as *Not connected*.
3. Press **Connect with Microsoft**, sign in as the mailbox account, consent.
   The console should return saying **Connected**.
4. Activate the mailbox, send it a test message, confirm a ticket appears.
5. **Revoke it deliberately:** from that account's *My Account → Apps*, remove
   this application. Within one poll interval the console must show **Microsoft
   sign-in expired** with a **Reconnect** button — not a login error, and not a
   retry loop in the logs.
6. Press **Reconnect** and confirm polling resumes.

If step 3 ends at a Microsoft consent screen the operator cannot clear, a tenant
admin must consent to `IMAP.AccessAsUser.All` for the application.

If the support mailbox is a **shared** mailbox, the mailbox username is the
licensed account that has full access, in the proxy form
`user@domain/shared@domain` — see the design's §8 Q7, which is an open question
precisely because it changes what that field means.
```

- [ ] **Step 4: Run everything**

Run:
```bash
cd backend && .venv/bin/pytest -q && .venv/bin/mypy app && .venv/bin/ruff check app tests
cd ../frontend && npx vitest run && npm run typecheck && npm run build
```
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_email_intake_oauth_secrecy.py deploy/README.md
git commit -m "email-intake: sweep for both secrets everywhere, and say what only a tenant can show"
```

- [ ] **Step 6: Merge, then deploy**

Open the PR, get it reviewed and merged to `main`, then hand the operator **Appendix B**.

---

## Appendix A — the deploy sequence for Cycle A+B

**The operator deploys from `main`, never from a feature branch**, so this
starts after the merge. Every step is listed, including the ones that are no-ops
for this change — a step that is missing reads as a step that was forgotten.

```sh
# 0. On the host, in the checkout that runs the stack.
cd /opt/it-suite/deploy
git checkout main
git pull

# 1. Is OpenBao sealed? It is sealed after EVERY restart and EVERY host reboot,
#    and a sealed OpenBao makes `up -d` BLOCK rather than fail -- openbao-init
#    waits on a healthcheck that only passes when unsealed. Whether it is sealed
#    right now depends on this host's history, which no session can see. So this
#    step is CONDITIONAL, never omitted: it is not this change that seals it.
docker compose exec openbao bao status

#    If it reports Sealed: true -- three shares, one per command:
docker compose exec openbao bao operator unseal
docker compose exec openbao bao operator unseal
docker compose exec openbao bao operator unseal

# 2. BACKUP, BEFORE ANYTHING ELSE, AND NOT OPTIONAL FOR THIS RELEASE.
#    Keycloak migrates its own schema in the `keycloak` database on first start
#    of 26.2 and does not migrate back. Re-pinning the image afterwards produces
#    a container that refuses to start; the only way back is this dump.
./scripts/backup.sh
#    Note the directory it prints, and copy it OFF this host.

# 3. Pull the new identity-provider image (the pin moved 26.0 -> 26.2).
docker compose pull keycloak

# 4. Rebuild the application images from the merged source.
docker compose build api gateway worker web

# 5. Bring everything up. This ALSO runs, as ordinary dependencies:
#      migrate          -> `alembic upgrade head`. NO NEW MIGRATION IN THIS
#                          RELEASE, so it will find nothing to do -- that is a
#                          statement about what it finds, not a step to omit.
#      postgres-grants  -> grant-app-privileges.sql. No new tables in this
#                          release, so it is a no-op, and it is idempotent.
#      keycloak-init    -> converges the realm allowlist. Runs every time, and
#                          deliberately never touches the smtpServer block.
docker compose up -d

# 6. Watch Keycloak migrate its schema. This is the irreversible step: if it
#    fails, STOP and go to the rollback below rather than retrying.
docker compose logs -f keycloak

# 7. Verify.
docker compose ps                       # every service Up; the one-shots Exited (0)
docker compose exec keycloak /opt/keycloak/bin/kcadm.sh --help >/dev/null && echo "kcadm ok"
curl -sS -o /dev/null -w '%{http_code}\n' https://<your-host>/api/v1/health
```

**Then, in the console:** Settings → Email. The existing password settings must
still load and still read *Password: set*. **No deployment's mail stops working
because of this release** — the Email tab simply grows a "Sign-in" choice that
defaults to Password.

**To actually use Microsoft**, follow `deploy/README.md` → *"Microsoft OAuth for
the outgoing relay — acceptance"*.

**No new `.env` keys in this cycle.** `ITS_PUBLIC_CONSOLE_URL` arrives with
Cycle C.

**Rolling back:**

```sh
# Application code only (the Email tab, the routes). Keycloak STAYS at 26.2 --
# 26.2 serves a 26.0-era realm perfectly well.
IT_SUITE_VERSION=<previous sha> docker compose up -d

# The Keycloak upgrade itself -- RE-PINNING THE IMAGE IS NOT ENOUGH:
docker compose stop keycloak
./scripts/restore.sh /opt/it-suite/deploy/backups/<the directory from step 2>
git checkout <the commit before the pin moved> -- docker-compose.yml
docker compose up -d
# NOTE: restore.sh restores BOTH databases, so anything written to the
# application database since step 2 is lost. That is exactly why the backup is
# taken immediately before the upgrade and not the night before.
```

---

## Appendix B — the deploy sequence for Cycle C

```sh
# 0. From main, on the host.
cd /opt/it-suite/deploy
git checkout main
git pull

# 1. OpenBao. Sealed after every restart and every host reboot; a sealed vault
#    makes `up -d` block rather than fail. Conditional, never omitted.
docker compose exec openbao bao status
#    If Sealed: true --
docker compose exec openbao bao operator unseal    # x3, one share each

# 2. NEW .ENV KEY IN THIS RELEASE -- set it BEFORE `up -d`.
#    The browser-facing origin of the console, scheme included, no trailing
#    slash. It is sent to Microsoft as the OAuth redirect URI and CANNOT be
#    derived from the request (host-header injection). Left blank, the OAuth
#    routes answer 503 naming it and nothing else changes.
grep -q '^ITS_PUBLIC_CONSOLE_URL=' .env || echo 'ITS_PUBLIC_CONSOLE_URL=' >> .env
#    Then edit .env and set it, e.g.:
#      ITS_PUBLIC_CONSOLE_URL=https://itms.toeshee.com

# 3. Backup. THERE IS A NEW MIGRATION IN THIS RELEASE (0083) and migrations are
#    forward-only: rollback is restore-from-backup.
./scripts/backup.sh
#    Copy the directory it prints OFF this host.

# 4. Rebuild.
docker compose build api gateway worker web

# 5. Up. This runs, as ordinary dependencies:
#      migrate         -> `alembic upgrade head`. THIS RELEASE HAS ONE: 0083.
#                         It adds two columns to email_mailbox, relaxes
#                         email_mailbox.credential_id to NULL under a new check
#                         constraint, and creates two tables.
#      postgres-grants -> grant-app-privileges.sql. NOT A NO-OP THIS TIME:
#                         `GRANT ... ON ALL TABLES` has to cover the two new
#                         tables or the application cannot read them.
#      keycloak-init   -> unchanged, runs every time.
docker compose up -d

# 6. Verify.
docker compose ps                    # services Up; the one-shots Exited (0)

docker compose exec -T postgres psql -U its_admin -d itsuite -c \
  "SELECT version_num FROM alembic_version;"          # expect 0083

docker compose exec -T postgres psql -U its_admin -d itsuite -c \
  "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class
    WHERE relname IN ('email_oauth_app','email_oauth_authorization');"
#   Expect TWO rows, both t/t. A missing policy would be a tenant-isolation
#   failure on a table holding a client-secret reference -- check this, do not
#   assume it.

curl -sS -o /dev/null -w '%{http_code}\n' https://<your-host>/api/v1/health
```

**Then, in the console:** Email Intake. Every existing password mailbox must
still show as connected and keep polling — `auth_kind` defaults to `password`
and nothing about that path changed. Then follow `deploy/README.md` →
*"Microsoft OAuth for the support mailbox — acceptance"*.

**Rolling back:**

```sh
# There is a migration, so this is restore-from-backup, not a container swap.
docker compose stop api gateway worker
./scripts/restore.sh /opt/it-suite/deploy/backups/<the directory from step 3>
IT_SUITE_VERSION=<previous sha> docker compose up -d
```

---

## Self-review

**Spec coverage.** §2 Keycloak upgrade → Tasks 1, 5. §3.2 the five realm keys →
Task 2. §3.2–3.3 the API and its two refusal rules → Task 3. §3.2 item 5, the
console → Task 4. §3.4 multi-tenancy → **documentation only**, in the spec; no
task, because the decision is to defer it and Q3 asks the operator to confirm
that. §4.1 refresh token in the vault → Tasks 6, 9. §4.2 rotation ordering →
Task 10. §4.3 the callback → Tasks 9, 13. §4.4 the flow, click by click →
Tasks 8, 9, 13. §4.5 failure paths → Task 9 (cancel, no-refresh-token, bad
application), Task 10 (revoked, expired, transient), Task 11 (the XOAUTH2
refusal continuation). §4.6 the host pin → Task 7. §4.7 schema → Task 6. §4.8
the six audit events → Tasks 8, 9, 12. §5.1 no regression → Tasks 3, 6, 11, 12,
each of which carries an explicit unchanged-path test. §5.2 redirect URI from
config → Task 7. §5.3–5.4 going back → Tasks 3, 13. §6.1's ten testable things →
items 1–2 Task 11, 3–4 Tasks 9 and 10, 5 Tasks 10 and 12, 6 Task 9, 7 Task 14,
8 Task 3, 9 Task 6 (through the existing `migrations` CI job), 10 Task 5.
§6.2 what cannot be tested → Tasks 5 and 14, as written acceptance checklists
rather than as a claim. §7 deploy and rollback → Appendices A and B.

**Placeholder scan.** No "TBD", no "similar to Task N", no "add appropriate
error handling". Three places defer deliberately and say what to do instead:
the test fixtures name the harness each file already has rather than inventing a
second one (Tasks 2, 3, 8, 10, 11, 12), Task 9 specifies its new fixture by the
methods it must expose, and Task 13's list/form changes are given as an explicit
condition→chip→button table rather than as a rewritten component, because four
agents are editing that tree.

**Type consistency.**
`_store_secret(..., name=, credential_type=)` — defined Task 8, called at those
exact names in Tasks 9 and 10.
`imap_client_factory(..., access_token=)` — defined Task 11, passed at that name
in Task 12, and the `ImapClientFactory` Protocol widened in the same task so
`mypy` accepts it.
`auth_kind` / `credential_state` — spelled identically in the migration and ORM
(Task 6), `MailboxOut` (Task 9), the poller (Task 12) and the TypeScript
(Task 13).
`OAuthFlowError.status` — set in Task 9's service, read by Task 9's routes.
`TokenRevoked` / `TokenTransportError` — defined Task 10, and the difference
between them is load-bearing in Task 12: one marks `needs_reconsent`, the other
deliberately falls through to the existing transient handling.
`microsoft.CALLBACK_PATH` (Task 7) and the React route path (Task 13) are the
same string, `/settings/email-intake/oauth/callback`, and so is the
`redirect_uri` the acceptance checklist tells the operator to register.

**Task numbering.** The original decomposition split the OAuth service from its
routes; they are merged into Task 9, because the tests are route-level and a
split would have ended a task with a red suite for reasons its implementer could
not fix. Everything after renumbers accordingly: 10 tokens, 11 IMAP, 12 poller,
13 console, 14 sweep and deploy.
