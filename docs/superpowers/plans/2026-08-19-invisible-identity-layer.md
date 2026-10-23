# The invisible identity layer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After installation, no task in normal operation requires a second
portal, an SSH session, or knowing that Keycloak exists.

**Architecture:** Keycloak stays and becomes an appliance component, like
Postgres. Its user-facing surfaces are replaced by ITMS ones: credential
actions become in-flow OIDC redirects rendered in the existing ITMS theme,
realm configuration moves into ITMS Settings, and first boot becomes a setup
screen. Its admin console leaves the public entrypoint.

**Tech Stack:** React + TypeScript strict + Vite + vitest (console), Python
3.12 / FastAPI / SQLAlchemy 2.x / Alembic (server), pytest against a real
Postgres, Traefik labels in `deploy/docker-compose.yml`.

## Global Constraints

Copied from `docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md`.

- **THE BUYER TEST governs every task:** no second portal, no SSH, no user-
  visible mention of "Keycloak" in normal operation.
- **The word "Keycloak" may not appear in any user-visible string.** Code
  comments, module names, and vendor-facing docs are exempt — the guard is
  about what a USER can see.
- **console-auth §1 is unchanged:** no password ever enters the ITMS process.
  The one stated exception is the SMTP password in Task 6, which ITMS writes
  through and never stores or echoes.
- **Every privileged action is audited** through the hash-chained log, in the
  same transaction as the change.
- **RBAC is server-side.** The console never gates security; it reflects it.
- **Tenant scoping via `org_session`.** Cross-org assertions run on the
  BYPASSRLS admin engine or they pass with the predicate deleted.
- **Alembic head is `0066`.** Exactly one migration this cycle (Task 7),
  `revision="0067"`, `down_revision="0066"`. Two files sharing a
  `down_revision` fork the chain and kill the suite at fixture setup.
- **`tests/test_ai_tool_surface.py` is measured ONCE, at the end, by the
  controller** — never per task.

---

## File Structure

| File | Responsibility |
|---|---|
| `frontend/src/lib/auth.ts` | `startAccountAction(action)` — the kc_action redirect |
| `frontend/src/lib/auth.accountAction.test.ts` | The URL it builds, and what it refuses |
| `frontend/src/modules/settings/ProfileTab.tsx` | In-flow buttons replace the deep links |
| `frontend/src/lib/noVendorNames.test.ts` | The word guard over all user-visible strings |
| `deploy/docker-compose.yml` | Traefik rule drops `/admin` |
| `backend/tests/test_public_surface.py` | Pins the routed prefixes |
| `frontend/src/modules/settings/EmailTab.tsx` | Settings → Email |
| `backend/app/modules/user_admin/realm_config.py` | SMTP read/write through the admin API |
| `backend/app/api/v1/realm_settings.py` | `GET/PUT /api/v1/settings/email`, test-send |
| `backend/migrations/versions/0067_org_console_domain.py` | The allowlist as org setting |
| `backend/app/core/identity/policy.py` | Resolve the allowlist: org setting → env override |
| `backend/app/api/v1/setup.py` | First-boot: status + claim |
| `backend/app/core/setup_code.py` | The install code: generate, read, compare |
| `frontend/src/modules/setup/SetupPage.tsx` | The setup screen |
| `deploy/keycloak/init-clients.py` | Init job: service clients, secrets, retire bootstrap |

---

# CYCLE 1 — kill every user-visible seam

### Task 1: `startAccountAction` — the in-flow redirect

**Files:**
- Modify: `frontend/src/lib/auth.ts` (after `signIn`, ~line 84)
- Test: `frontend/src/lib/auth.accountAction.test.ts` (create)

**Interfaces:**
- Produces: `startAccountAction(action: AccountAction): Promise<void>` and
  `type AccountAction = "UPDATE_PASSWORD" | "CONFIGURE_TOTP" | "webauthn-register-passwordless"`.
  Task 2 consumes both.

- [ ] **Step 1: Write the failing test**

```ts
// frontend/src/lib/auth.accountAction.test.ts
import { describe, expect, it, vi } from "vitest";

/**
 * WHY A REDIRECT AND NOT A LINK. The account console is a SECOND APPLICATION:
 * different chrome, different navigation, and it says "Keycloak" on it. An
 * application-initiated action runs ONE step — set a password, enrol a key —
 * inside the same themed pages the user signs in on, then returns here.
 *
 * The `kc_action` parameter is what selects that step. If it is dropped, the
 * redirect degrades into an ordinary re-authentication: the user is bounced
 * to sign-in, comes back, and NOTHING HAPPENS. That is the failure this file
 * exists for, and it is silent — no error, no console message, just a button
 * that does not work.
 */
describe("startAccountAction", () => {
  it("asks the identity provider for exactly the requested action", async () => {
    const signinRedirect = vi.fn().mockResolvedValue(undefined);
    vi.doMock("./auth", async (orig) => {
      const real = (await orig()) as Record<string, unknown>;
      return { ...real, userManager: { signinRedirect } };
    });
    const { startAccountAction } = await import("./auth");

    await startAccountAction("UPDATE_PASSWORD");

    expect(signinRedirect).toHaveBeenCalledWith(
      expect.objectContaining({ extraQueryParams: { kc_action: "UPDATE_PASSWORD" } }),
    );
  });

  it("refuses an action it does not know", async () => {
    const { startAccountAction } = await import("./auth");
    // @ts-expect-error deliberately outside the union
    await expect(startAccountAction("DELETE_ACCOUNT")).rejects.toThrow(/unsupported/i);
  });
});
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd frontend && npx vitest run src/lib/auth.accountAction.test.ts`
Expected: FAIL — `startAccountAction is not a function`.

- [ ] **Step 3: Implement**

```ts
// frontend/src/lib/auth.ts — append after signIn()

/**
 * The credential steps a user may start from inside ITMS.
 *
 * A CLOSED UNION, not a string. These values are handed to the identity
 * provider as `kc_action`; an unknown one produces a redirect that
 * re-authenticates and silently does nothing, which presents as a button that
 * appears to work and does not.
 */
export type AccountAction =
  | "UPDATE_PASSWORD"
  | "CONFIGURE_TOTP"
  | "webauthn-register-passwordless";

const ACCOUNT_ACTIONS: readonly AccountAction[] = [
  "UPDATE_PASSWORD",
  "CONFIGURE_TOTP",
  "webauthn-register-passwordless",
];

/**
 * Run ONE credential step in the sign-in pages, then come back here.
 *
 * This is what replaces linking to the identity provider's own account
 * console: same theme, same tab, no second application, and the user lands
 * back on the page they left. The platform still never handles the credential
 * — it only asks for the step to be run.
 */
export async function startAccountAction(action: AccountAction): Promise<void> {
  if (!ACCOUNT_ACTIONS.includes(action)) {
    throw new Error(`unsupported account action: ${action}`);
  }
  await userManager.signinRedirect({
    extraQueryParams: { kc_action: action },
    // Return to whatever page started this, not to the app root: a user who
    // changed their password from Settings should land back in Settings.
    state: { returnTo: window.location.pathname + window.location.search },
  });
}
```

- [ ] **Step 4: Run it and watch it pass**

Run: `cd frontend && npx vitest run src/lib/auth.accountAction.test.ts`
Expected: PASS, 2 tests.

- [ ] **Step 5: Falsify**

Delete `extraQueryParams` from the `signinRedirect` call. Run the test. Expect
the first test RED. Restore.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/lib/auth.ts frontend/src/lib/auth.accountAction.test.ts
git commit -m "auth: run one credential step in-flow instead of linking out"
```

---

### Task 2: The profile page stops leaving ITMS

**Files:**
- Modify: `frontend/src/modules/settings/ProfileTab.tsx` (lines 50–66 and 195–222)
- Test: `frontend/src/modules/settings/__tests__/profile.test.tsx`

**Interfaces:**
- Consumes: `startAccountAction`, `AccountAction` from Task 1.

- [ ] **Step 1: Write the failing test**

```tsx
// append to frontend/src/modules/settings/__tests__/profile.test.tsx

it("starts the credential step in place rather than opening another app", async () => {
  /**
   * THE SEAM THE BUYER SEES. A link to the account console is a visibly
   * different product: different chrome, different navigation, and the vendor's
   * name on it. This asserts the button starts an in-flow action and that no
   * anchor to an account console survives anywhere on the page.
   */
  const startAccountAction = vi.fn().mockResolvedValue(undefined);
  vi.doMock("../../../lib/auth", async (orig) => ({
    ...(await orig() as object),
    startAccountAction,
  }));
  renderWithProviders(<ProfileTab />);

  const button = await screen.findByRole("button", { name: /change password/i });
  await userEvent.click(button);
  expect(startAccountAction).toHaveBeenCalledWith("UPDATE_PASSWORD");

  const links = screen.queryAllByRole("link");
  expect(links.filter((a) => a.getAttribute("href")?.includes("account"))).toEqual([]);
});
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd frontend && npx vitest run src/modules/settings/__tests__/profile.test.tsx`
Expected: FAIL — no button matching /change password/.

- [ ] **Step 3: Implement**

Replace the `CREDENTIAL_ACTIONS` const (lines 53–66) with:

```tsx
const CREDENTIAL_ACTIONS: readonly { label: string; hint: string; action: AccountAction }[] = [
  {
    label: "Change password",
    action: "UPDATE_PASSWORD",
    hint: "You will be asked for your current password first. This platform never sees either one.",
  },
  {
    label: "Manage MFA device",
    action: "CONFIGURE_TOTP",
    hint: "Add, replace or remove your second factor.",
  },
  {
    label: "Add a passkey",
    action: "webauthn-register-passwordless",
    hint: "A passwordless credential bound to this device.",
  },
] as const;
```

Delete the `accountConsoleUrl` export (line 51). Replace the `<Panel
title="Sign-in and security">` body (lines 197–222) with:

```tsx
      <Panel title="Sign-in and security">
        <p className="text-sm text-[var(--muted)]">
          {`Each of these runs one step on the sign-in pages and brings you straight back.
          Passwords, second factors and passkeys are never handled by this platform.`}
        </p>
        <ul className="mt-3">
          {CREDENTIAL_ACTIONS.map((item) => (
            <li key={item.action} className="py-1">
              <button
                type="button"
                className="text-sm underline"
                onClick={() => {
                  void startAccountAction(item.action);
                }}
              >
                {item.label}
              </button>
              <div className="text-xs text-[var(--muted)]">{item.hint}</div>
            </li>
          ))}
        </ul>
      </Panel>
```

Update the imports at the top: add `startAccountAction` and `type AccountAction`
from `../../lib/auth`; remove `oidcAuthority` if now unused. Also rewrite the
file's header comment (lines 12–17) so it describes in-flow actions rather than
the account console, and remove the subtitle `"Held by the identity provider,
not by ITMS"` — it names the seam it no longer has.

- [ ] **Step 4: Run it and watch it pass**

Run: `cd frontend && npx vitest run src/modules/settings/__tests__/profile.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/modules/settings/ProfileTab.tsx frontend/src/modules/settings/__tests__/profile.test.tsx
git commit -m "profile: credential steps run in place, not in a second console"
```

---

### Task 3: The word guard

**Files:**
- Test: `frontend/src/lib/noVendorNames.test.ts` (create)
- Modify: whatever files it names on first run.

- [ ] **Step 1: Write the guard**

```ts
// frontend/src/lib/noVendorNames.test.ts
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * NO USER-VISIBLE STRING MAY NAME THE IDENTITY VENDOR.
 *
 * A buyer does not know what Keycloak is and must never need to. The word
 * leaking into a rendered string is not cosmetic: it tells the reader there is
 * a second system they are expected to understand, which is the exact
 * impression this whole cycle exists to remove.
 *
 * SCOPED TO STRINGS, DELIBERATELY. Import paths, module names, type names and
 * comments are exempt — the platform does integrate with Keycloak and pretending
 * otherwise in the source would make the code lie. A guard broader than its own
 * claim gets deleted the first time it is inconvenient; this one is exactly as
 * wide as "what a user can see".
 */
const SRC = join(import.meta.dirname, "..");
const FORBIDDEN = /keycloak/i;
// Quoted strings and JSX text, not identifiers or import paths.
const STRING_LITERAL = /(["'`])((?:\\.|(?!\1)[^\\])*)\1/g;

function* sourceFiles(dir: string): Generator<string> {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) {
      yield* sourceFiles(path);
    } else if (/\.(ts|tsx)$/.test(entry) && !/\.test\.tsx?$/.test(entry)) {
      yield path;
    }
  }
}

describe("user-visible copy", () => {
  it("never names the identity vendor", () => {
    const offenders: string[] = [];
    for (const file of sourceFiles(SRC)) {
      const source = readFileSync(file, "utf8");
      for (const [, , body] of source.matchAll(STRING_LITERAL)) {
        // Import specifiers and URLs are not copy.
        if (/^[./@]/.test(body) || /^https?:/.test(body)) continue;
        if (FORBIDDEN.test(body)) {
          offenders.push(`${file.replace(SRC, "src")}: ${JSON.stringify(body).slice(0, 80)}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});
```

- [ ] **Step 2: Run it — it should FAIL, naming real offenders**

Run: `cd frontend && npx vitest run src/lib/noVendorNames.test.ts`
Expected: FAIL listing at least `ProfileTab.tsx` if Task 2's hint rewrite was
missed, plus any other copy carrying the word.

- [ ] **Step 3: Fix every string it names**

Rewrite each to say "the identity provider", or to drop the actor entirely
("You will be asked for your current password first"). Do NOT weaken the guard.

- [ ] **Step 4: Run it and watch it pass**

Run: `cd frontend && npx vitest run src/lib/noVendorNames.test.ts`
Expected: PASS.

- [ ] **Step 5: Falsify**

Add `const x = "Keycloak is great";` to `ProfileTab.tsx`. Run. Expect RED
naming that file. Remove it.

- [ ] **Step 6: Commit**

```bash
git add frontend/src
git commit -m "console: no user-visible string names the identity vendor"
```

---

### Task 4: `/admin` leaves the public entrypoint

**Files:**
- Modify: `deploy/docker-compose.yml:503`
- Modify: `deploy/README.md` (the SMTP and provisioning sections that say `/admin`)
- Test: `backend/tests/test_public_surface.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_public_surface.py
"""What the public entrypoint routes, pinned.

THE IDENTITY PROVIDER'S ADMIN CONSOLE MUST NOT BE INTERNET-REACHABLE. It can
create users, reset any password and rewrite the realm — the whole fleet's
authority in one page, behind one password, on the same hostname as the
product. Sign-in needs `/realms`, `/resources` and `/js`; it does not need
`/admin`.

This is a guard over a Traefik label, which is the only place the decision is
expressed. Nothing here starts a proxy — it asserts the committed rule.
"""

import re
from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[2] / "deploy/docker-compose.yml"


def _keycloak_rule() -> str:
    match = re.search(r"traefik\.http\.routers\.kc\.rule:\s*(.+)", COMPOSE.read_text())
    assert match, "the keycloak router rule is gone; this test is measuring nothing"
    return match.group(1)


def test_sign_in_is_still_routed() -> None:
    """Non-vacuity: removing `/admin` must not remove sign-in with it."""
    rule = _keycloak_rule()
    for needed in ("/realms", "/resources", "/js"):
        assert needed in rule, f"sign-in needs {needed} and the rule no longer routes it"


def test_the_admin_console_is_not_publicly_routed() -> None:
    assert "/admin" not in _keycloak_rule(), (
        "the identity provider's admin console is reachable from the internet; "
        "it can reset any password in the fleet. Reach it over an SSH tunnel "
        "to the container port instead (deploy/README.md, vendor appendix)."
    )
```

- [ ] **Step 2: Run it and watch the second test fail**

Run: `cd backend && .venv/bin/pytest tests/test_public_surface.py -q`
Expected: 1 passed, 1 failed.

- [ ] **Step 3: Change the rule**

`deploy/docker-compose.yml:503` becomes:

```yaml
      # `/admin` is DELIBERATELY ABSENT. The identity provider's admin console
      # can reset any password in the fleet; it does not belong on the same
      # public hostname as the product. Sign-in needs only these three.
      # Vendor break-glass: ssh -L 8080:localhost:8080 and hit the container.
      traefik.http.routers.kc.rule: PathPrefix(`/realms`) || PathPrefix(`/resources`) || PathPrefix(`/js`)
```

- [ ] **Step 4: Run it and watch both pass**

Run: `cd backend && .venv/bin/pytest tests/test_public_surface.py -q`
Expected: 2 passed.

- [ ] **Step 5: Update the runbook**

In `deploy/README.md`, replace every instruction that sends the operator to
`https://<host>/admin` with the SSH-tunnel break-glass, and mark those sections
**vendor-only**. The SMTP section additionally gets a line pointing at Settings →
Email once Task 6 lands.

- [ ] **Step 6: Commit**

```bash
git add deploy/docker-compose.yml deploy/README.md backend/tests/test_public_surface.py
git commit -m "deploy: the identity admin console leaves the public entrypoint"
```

---

# CYCLE 2 — configuration moves into ITMS

### Task 5: Realm SMTP read/write

**Files:**
- Create: `backend/app/modules/user_admin/realm_config.py`
- Test: `backend/tests/test_realm_config.py`

**Interfaces:**
- Produces: `async def get_smtp(config: KeycloakAdminConfig) -> dict[str, str]`,
  `async def set_smtp(config, values: dict[str, str]) -> None`,
  `async def send_test_email(config, *, user_id: str) -> None`.
  Task 6 consumes all three. `KeycloakAdminConfig` is the existing dataclass in
  `backend/app/modules/user_admin/kc_client.py`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_realm_config.py
"""Reading and writing the realm's SMTP block through the admin API.

MIRRORS `tests/test_user_admin_client.py` EXACTLY in shape — MockTransport, a
factory captured at import, and assertions on the request this code SENDS. It
proves nothing about what a real identity provider replies; that gap is stated
in the spec and lives on the operator's deployment, as every other surface here
does.

THE PASSWORD IS THE INTERESTING PART. It travels once, on the way in, and is
never read back: the realm's GET returns the SMTP block WITHOUT the password,
and `get_smtp` must not invent one. A form that round-trips a password it never
received would write an empty string over a working credential the first time
somebody edited the port.
"""
```

Write four tests: `test_get_smtp_reads_the_realm_block`,
`test_get_smtp_never_returns_a_password`, `test_set_smtp_merges_rather_than_replacing_the_realm`
(the PUT body must carry the whole realm representation with only `smtpServer`
changed — a bare `{"smtpServer": ...}` PUT wipes unrelated realm settings), and
`test_send_test_email_targets_the_calling_user`.

- [ ] **Step 2–4:** run to fail, implement `realm_config.py` against
  `GET/PUT /admin/realms/{realm}` and
  `POST /admin/realms/{realm}/testSMTPConnection`, run to pass.

- [ ] **Step 5: Falsify** — make `set_smtp` PUT only the `smtpServer` key;
  `test_set_smtp_merges_rather_than_replacing_the_realm` must go red.

- [ ] **Step 6: Commit**

---

### Task 6: Settings → Email

**Files:**
- Create: `backend/app/api/v1/realm_settings.py`, `frontend/src/modules/settings/EmailTab.tsx`
- Modify: `frontend/src/modules/settings/SettingsPage.tsx`, `api.ts`, `types.ts`,
  `__tests__/harness.tsx`, `backend/app/main.py` (controller wires the router)
- Test: `backend/tests/test_realm_settings_api.py`, `frontend/src/modules/settings/__tests__/email.test.tsx`

**Interfaces:**
- Consumes: Task 5's three functions.
- Routes: `GET /api/v1/settings/email` (`orgs.manage`),
  `PUT /api/v1/settings/email` (`orgs.manage`),
  `POST /api/v1/settings/email/test` (`orgs.manage`).

- [ ] Steps: failing tests → implement → pass → falsify → commit.

Required backend tests: the PUT is RBAC-gated; the response NEVER contains the
password (assert the literal absent from the body, not merely that the key is
missing); the change is audited with the values omitted; an unconfigured state
returns `configured: false` rather than 404.

Required frontend tests: the form renders; saving calls the PUT with only the
supplied fields; the saved state shows `password: set` and never the value; the
test-send button reports the honest message
(`"Sent — check your inbox. A relay can accept a message and still drop it."`).

**Also in this task** (it is the same deliverable — the Users tab is unusable
without it): the Users tab shows a banner when email is unconfigured, reading
`"Email isn't set up, so invitations can't be sent — Settings → Email"`, with a
test that it appears only when `configured` is false.

---

### Task 7: The allowlist becomes a setting

**Files:**
- Create: `backend/migrations/versions/0067_org_console_domain.py`,
  `backend/app/core/identity/policy.py`
- Modify: `backend/app/api/deps.py:72`, `backend/app/api/v1/ws.py:90`,
  `backend/app/modules/remote/tunnel.py:290`,
  `backend/app/modules/user_admin/api.py:332`, `backend/app/api/v1/orgs.py`
- Test: `backend/tests/test_domain_policy.py`, plus updates to
  `backend/tests/test_domain_gate_doors.py`

**Interfaces:**
- Produces: `async def resolve_console_domain(session, org_id, settings) -> str | None`.
  All three doors and the create-user path consume it.

The org's allowlist lives in `organizations.settings["console_email_domain"]`
(the column is already a JSON dict; migration 0067 backfills it from the env
var so an existing deployment does not change behaviour on upgrade).
`ITS_CONSOLE_EMAIL_DOMAIN` is demoted to `ITS_CONSOLE_EMAIL_DOMAIN_OVERRIDE`,
which when set to `off` disables the gate everywhere — vendor break-glass for a
setting that locked everyone out.

- [ ] Required tests: the org setting is honoured at all three doors; the
  override disables it at all three; an org with no setting is unrestricted;
  **saving a value that would refuse the caller's own address is refused**
  (the structural fix for the lockout, replacing the SSH preflight); the
  in-UI preview lists exactly the users the old script would have listed.

- [ ] The `Organization` settings PATCH must reject an unknown settings key so
  a typo does not silently do nothing.

- [ ] Retire `scripts/preflight_email_domain.py` from the runbook (keep the
  file, mark it vendor-only) and delete the README section that told the
  operator to run it over SSH.

---

# CYCLE 3 — first boot is a screen

### Task 8: The install code

**Files:**
- Create: `backend/app/core/setup_code.py`, `backend/tests/test_setup_code.py`

**Interfaces:**
- Produces: `def ensure_setup_code(path: Path) -> str`,
  `def verify_setup_code(path: Path, presented: str) -> bool`.

Generated on first start, written to the shared secrets volume mode 600 and
logged once by the api container. `verify_setup_code` compares with
`hmac.compare_digest` — a timing-safe comparison, because this code is the only
thing standing between a stranger and ownership of the platform.

- [ ] Required tests: generated once and stable across calls; a wrong code is
  refused; comparison is constant-time (assert `hmac.compare_digest` is used —
  a behavioural timing test is flaky and proves less than the call site);
  the file is created mode 600.

---

### Task 9: Setup status and claim

**Files:**
- Create: `backend/app/api/v1/setup.py`, `backend/tests/test_setup_api.py`

**Routes:** `GET /api/v1/setup/status` → `{"needed": bool}` (UNGATED — it is
what the frontend asks before anyone can sign in; it reveals only whether the
platform has any users). `POST /api/v1/setup/claim` `{code, email,
display_name, organization_name}` (UNGATED, guarded by the code + zero-users).

- [ ] Required tests, and these are the security of the whole feature:
  claim SUCCEEDS on an empty platform with the right code; claim is REFUSED
  once any user exists (even with the right code); claim is REFUSED with a
  wrong code; `status` never reveals the code; the created user is org-admin;
  **the claim path calls the same service functions the Users tab uses**
  (asserted, so the two cannot drift); everything is audited.

- [ ] `GET /api/v1/setup/status` goes in `UNGATED_BY_DESIGN` **with its reason
  comment** — Task 10 of the previous cycle made that mandatory.

---

### Task 10: The setup screen

**Files:**
- Create: `frontend/src/modules/setup/SetupPage.tsx` + test
- Modify: `frontend/src/App.tsx` (check `setup/status` before the auth gate)

- [ ] Required tests: the screen shows when `needed` is true; a normal sign-in
  flow is untouched when false; a wrong code shows the error rather than a
  blank page; the form cannot be submitted twice (double-click creates one org).

---

### Task 11: The init job

**Files:**
- Create: `deploy/keycloak/init-clients.py`
- Modify: `deploy/docker-compose.yml` (a `keycloak-init` one-shot service),
  `deploy/GO-LIVE.md` (steps 8–10 replaced by "open the URL, enter the code")
- Test: `backend/tests/test_keycloak_init_job.py`

Idempotent: creates `it-suite-provisioning` and `it-suite-events`, grants
`manage-users`/`manage-realm` and `view-events` respectively, writes both
secrets to the secrets volume mode 600, then **rotates the bootstrap admin's
password to a random value written only to that volume** — retiring it from
human use without deleting the account the vendor needs for break-glass.

- [ ] Required tests (against the script's logic, not a live realm): running
  twice creates one of each client; the secrets are written mode 600; the
  bootstrap rotation happens AFTER the clients exist (rotating first would
  strand the job with no way to authenticate); a failure part-way leaves the
  job re-runnable.

---

## Verification before claiming completion

- `cd backend && .venv/bin/pytest -q` — green, whole suite
- `cd backend && .venv/bin/mypy app && .venv/bin/ruff check app tests` — clean
- `cd frontend && npx vitest run && npx tsc --noEmit` — green
- Migration 0067 round-trips against a real Postgres
- `test_ai_tool_surface.py` re-measured ONCE, with every new route present
- The word guard falsified by planting the vendor name in a rendered string
- **Not verified anywhere in CI, and stated in the spec:** the three `kc_action`
  flows, the SMTP write-through, and the init job have never run against a live
  Keycloak. They are exercised on the operator's deployment.
