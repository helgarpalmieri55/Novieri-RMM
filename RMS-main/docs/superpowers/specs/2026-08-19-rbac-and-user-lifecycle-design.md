# Access control and user lifecycle — Design

**Date:** 2026-08-19
**Status:** approved by the operator, not yet built
**Supersedes nothing. Amends:** `docs/modules/console-auth/spec.md` §4, §7.3

## Goal

Give an administrator a console surface that controls **who can sign in** and
**what each person may reach, module by module and level by level** — and make
the email-domain restriction a real server-side control rather than a form hint.

Four pieces, delivered as one cycle:

| | Piece | Depends on |
|---|---|---|
| A | RBAC administration: roles, an 88-code permission matrix, scoped assignment, effective-permission view | nothing external |
| B | Full-lifecycle user management against Keycloak | Keycloak SMTP |
| C | Email-domain allowlist enforced at token acceptance, and `users.status` enforcement | nothing external |
| D | Passkey finishing touches | nothing external |

---

## 0. What was found before designing

Recorded because each finding changed the design, and because several of them
contradict what the repository's own documentation implies.

**0.1 The RBAC engine is real and complete; nothing can change it but SQL.**
88 permission codes across 32 modules (`app/core/rbac.py`), roles,
role→permission grants, and user→role assignments that already support
**scoping a grant to one device group** — `rbac.py`'s module docstring says a
`user_roles` row with `device_group_id` NULL grants org-wide and one with a
group grants only for that group. No API and no UI exposes any of it.
`orgs.manage` is described as *"Manage organization settings, users, and
roles"*: a permission code written for a surface that was never built.

**0.2 Writes to `role_permissions` are REVOKED from `app_user`, deliberately.**
`deploy/postgres/grant-app-privileges.sql:59`, mirrored in
`backend/tests/conftest.py:120`. Its own rationale block (lines 47–54) explains
why: without the REVOKE, `app_user` could write the RBAC catalog directly,
"a way to self-grant permissions that bypasses the API's own RBAC checks
entirely". **No migration applies this** — that SQL file is the only place it is
enforced for a real deployment. Every permission-matrix write would fail with
`permission denied for table role_permissions` until this is addressed.

**0.3 There are three doors that turn a token into a principal, not one.**
`validate_token` is called at `app/api/deps.py:52`, `app/api/v1/ws.py:78`, and
`app/modules/remote/tunnel.py:272`. The agent gateway is **not** a fourth: it
identifies agents by TLS peer-certificate fingerprint with no bearer token. The
AI modules read the `Authorization` header only to replay it into a loopback
call that re-enters `deps.py`. **An allowlist enforced at one of three doors is
not an allowlist**, and nothing in the test suite would have shown the gap.

**0.4 `User.status` is written by nothing and read by nothing.** It defaults to
`active` and no code path in `app/` consults it. A "disabled" user today is
fully authorized. Any disable feature must ship its enforcement, not inherit it.

**0.5 The five seeded roles are GLOBAL.** `Role.org_id` is nullable and the seed
leaves it NULL, so `technician` is one row shared by every tenant. Editing it
in place from one org changes it for all of them. See §1.3.

**0.6 89 foreign-key columns across 21 tables reference `users.id`** —
`created_by`, `approver_id`, audit `actor_id`. A hard user delete either fails
on the constraints or shreds the history that makes the audit log evidence.

**0.7 `orgs.manage` today gates almost nothing.** Verified: the org
name/settings PATCH (`app/api/v1/orgs.py:392`) and remote-tunnel authorization
(`app/modules/remote/tunnel.py:250`). Its description promises far more than it
delivers, which makes reusing it for identity lifecycle look pre-approved when
it would in fact be a silent privilege escalation for every current holder.

---

## 1. Piece A — RBAC administration

### 1.1 The matrix is the product

Codes are already `<module>.<action>`, so the grid writes itself: 32 modules
down, actions across, 88 cells occupied. That grid **is** "what modules, at what
level". No new vocabulary is invented; the catalogue is served from
`rbac.py::PERMISSIONS`, never re-listed in the frontend, so a code added to the
platform appears in the UI without a second edit.

### 1.2 Matrix writes go through a SECURITY DEFINER function

**The REVOKE in §0.2 stays.** `app_user` gets no direct write to
`role_permissions`. Migration 0065 creates a function owned by `its_admin`:

```
set_role_permissions(role_id uuid, codes text[]) RETURNS int
```

which replaces a role's whole code set atomically and is the only writable
surface. Two properties this buys that a plain `GRANT` cannot:

1. **The anti-self-grant control survives.** A compromised app process or a
   SQL-write primitive still cannot mint permissions, because the only path is a
   function that validates its arguments.
2. **`super-admin` is untouchable in the database**, not merely in Python. The
   function refuses a `role_id` whose row is the cross-org `super-admin`, so the
   rail holds even for a caller that bypasses the service layer.

Rejected — `GRANT INSERT, DELETE ON role_permissions TO app_user`: one line, and
it deletes exactly the control the SQL file argues for in writing. Rejected —
writing through the BYPASSRLS admin engine: it would make tenant scoping for
those writes application-code-only, which invariant 5 calls a security bug
rather than a style nit.

### 1.3 Editing a seeded role clones it into your org first

The operator asked for seeded roles to be **fully editable**. Taken literally
against §0.5 that means one org's edit silently reshapes another tenant's
`technician`. So: editing a role whose `org_id` is NULL **transparently creates
an org-scoped copy** carrying the same name and current code set, repoints that
org's `user_roles` rows at the copy, and edits the copy.
`UniqueConstraint("org_id", "name")` makes the copy legal beside the global row.

The administrator sees "I edited technician" and gets exactly that. What they do
not get is the ability to reach into another tenant, which was never what
"editable" was asking for.

**`super-admin` is excluded entirely** — never listed as grantable, never
editable, never clonable. It is cross-org platform administration
(`"Cross-org platform administration (SaaS operator)"`), and an org-scoped page
that could grant it is a tenant-escalation path.

### 1.4 Rails

- A user may not remove their **own** `orgs.manage` or `roles.manage`.
- The **last `org-admin` assignment in an org** may not be removed.
- Editing a role reports the **count of users affected** before applying;
  "edit role" reads local and is not.
- Every change writes an audit event naming the role, the codes added and
  removed, and the actor.

Without the first two, the first mis-click is unrecoverable without SQL on
production.

### 1.5 Effective permissions

A computed, read-only view per user: the union of their roles' codes, annotated
with which role granted each and whether the grant is org-wide or scoped to a
device group. An access-control UI whose result you have to simulate in your
head is one you will get wrong.

---

## 2. Piece B — user lifecycle

### 2.1 Scope, and the accounting

Create, disable/enable, assign roles, trigger a password reset, delete. The
credential is a **separate** Keycloak service-account client from
`it-suite-events`, holding `manage-users`.

**Stated plainly, because it is the cost of the feature the operator chose:
with full lifecycle, whoever owns ITMS owns every identity in the realm.**
`manage-users` has no finer split in Keycloak. The mitigations are that the
credential is separate from the events one, the code path calls exactly six
operations — create a user, enable/disable a user, `execute-actions-email`,
delete a user, set a user's own `locale` attribute, and remove one AI app's
consent for one user
(`app/modules/user_admin/kc_client.py`; "assign roles" is an ITMS table, not a
Keycloak call. The fifth was added 2026-10-14 by the Spanish-language work:
`PATCH /me` mirrors the caller's language to *their own* Keycloak user only,
writing `username`, `email`, `firstName`, `lastName` and the attributes map with
just `locale` changed, then reading it back. The sixth was added 2026-10-14 by
the MCP server: revoking an AI app (`POST /api/v1/mcp/connections/{client_id}/revoke`)
calls `DELETE /users/{id}/consents/{client}` for the person, or for every
person of the organization when an org-admin revokes it for everyone, so the
app's refresh token cannot mint new access tokens. Consents are part of the
user resource, so this needs nothing beyond `manage-users`) —
RBAC gates every route, every action is audited, and impersonation —
also inside `manage-users` — is never called. The *credential* stays broader
than the *code*; this document says so rather than implying the grant is narrow.

### 2.2 Three rules that are not negotiable

1. **No password ever transits ITMS.** Reset means calling Keycloak's
   `execute-actions-email` with `UPDATE_PASSWORD`. ITMS never generates, stores,
   displays or returns a password — invariant 3.
2. **Delete removes the Keycloak identity only.** The ITMS row is marked
   `status='deleted'` and retained, because of §0.6. It stops authorizing
   anything the moment §3 lands.
3. **Creation is Keycloak first, then the ITMS row.** Keycloak's create returns
   the user id, which *is* the `sub` claim, so the ITMS row is written with the
   correct `external_subject` immediately. It also fails safe: if the ITMS half
   fails, what remains is an account with no roles — exactly the state of any
   federated user before first login, which `deps.py` already self-heals. The
   reverse order strands a row holding roles no identity can ever claim.

### 2.3 SMTP is a hard prerequisite

`deploy/keycloak/realm-it-suite.json` has no `smtpServer`. A user created with
no credential cannot sign in to enrol a passkey, and Keycloak 26 exposes no
admin API that returns the action link for an operator to pass on by hand. So:
creation succeeds, the enrolment email is attempted, and **a failure to send is
reported in the response rather than swallowed** — plus a **resend** action,
without which one transient mail failure leaves an account nobody can ever sign
into. The runbook makes SMTP a step, not an assumption.

---

## 3. Piece C — the domain allowlist, at all three doors

### 3.1 The rule

`is_allowed_domain(email, base)`: allowed iff the domain equals `base` or ends
with `"." + base`, both lowercased. `mail.toeshee.com` passes;
`eviltoeshee.com` fails (no dot); `toeshee.com.evil.com` fails (wrong suffix).
More than one `@` is refused outright rather than parsed — quoted local parts
are legal RFC and a parsing risk for no benefit. An absent `email` claim yields
`""` today (`app/core/security.py:124`), which fails closed.

`ITS_CONSOLE_EMAIL_DOMAIN`, **defaulting to `None` meaning no restriction**. The
default is load-bearing: any other value would lock out every existing
deployment on restart. One base domain, not a list — the operator declined a
list — but in config rather than in source, because `toeshee.com` compiled into
a platform meant to be sold is a bug for the second customer.

### 3.2 Enforced three times, or not at all

| Door | File | Refusal |
|---|---|---|
| HTTP API | `app/api/deps.py:52` | `403` |
| Console WebSocket | `app/api/v1/ws.py:78` | close `4403` |
| Remote-control tunnel | `app/modules/remote/tunnel.py:272` | `4403` |

`403`, not `401`: the token is genuine and the identity simply is not permitted,
and `401` would send the browser round the sign-in loop again. The WebSocket
door uses a distinct `4403` so "wrong domain" stays distinguishable from
`4401` "bad token" on the wire, matching the tunnel's existing vocabulary.

The same gate refuses `status != 'active'`, closing §0.4 and the window in which
a disabled user's unexpired token still works.

### 3.3 The lockout, which is the dangerous half

Turning this on can lock out existing users **including the operator's own
account**. Therefore:

- a **preflight** that lists every existing user whose email would fail, to be
  run *before* the variable is set — the only way to find out beforehand;
- a **break-glass**: unset the variable and restart the api (settings are read
  once at `create_app`);
- both in `deploy/README.md`, with the preflight as a required step rather than
  a suggestion.

---

## 4. Piece D — passkeys

Deliberately small. What already exists: the passwordless WebAuthn policy in the
realm, the `webauthn-register-passwordless` required action, the browser-flow
runbook, and Traefik routing for `/realms`. What is added: the required action
on ITMS-created users, `execute-actions-email` to deliver it, a console entry
point to `/realms/it-suite/account` so nobody needs to know the URL, and an
answer to spec §7.3. Keycloak's own account console is not reimplemented.

---

## 5. Falsifiers

1. **A permission-matrix save succeeds as `app_user` with the REVOKE removed
   from the test harness** — proving the SECURITY DEFINER path is what carries
   the write, not a loosened grant.
2. **Editing a seeded role changes another org's users.** The clone-on-write
   test asserts org B's effective permissions are untouched after org A edits
   `technician`, on the BYPASSRLS admin engine so RLS cannot mask it.
3. **`super-admin` is grantable or editable** through any route.
4. **A user can remove their own `orgs.manage`**, or the last `org-admin` can be
   stripped.
5. **A token failing the domain rule is admitted at any of the three doors.**
   One test per door; the WebSocket and tunnel tests must assert the *distinct*
   close code, since a test asserting "refused" passes on the wrong code.
6. **A disabled user's unexpired token still authorizes.**
7. **A password or reset token appears in an API response or a log.**
8. **A deleted user's ITMS row is hard-deleted**, or an audit event's actor
   becomes unresolvable.
9. **The domain setting defaults to anything but `None`** — a test asserting the
   default, because the wrong default is a fleet-wide lockout on upgrade.
10. **`/api/v1/users` is served by two routers.** A test asserting each path in
    the app appears exactly once, because FastAPI shadows duplicates silently
    and every other guard in the suite passes while it does.

## 6. What this design does not claim

- **Nothing here has run against a live Keycloak.** The admin-API conversation
  is covered by a mock transport only, exactly as `keycloak_events` is.
- **The `manage-users` credential is broader than the code that uses it**, and
  no wording changes that (§2.1).
- **The domain rule keys on the email claim**, which is asserted by the IdP. It
  restricts which identities *this platform* admits; it is not a control over
  what Entra will authenticate.
- The preflight reports who *would* be refused at the moment it runs. It cannot
  predict an account created between the preflight and the restart.

---

## 7. Piece E — the account profile (added 2026-08-19, mid-cycle)

**Nothing exists.** `GET /api/v1/me` is read-only — `user_id`, `org_id`,
`email`, `display_name`, `permissions` — with no write route, no frontend
module, and no nav entry. There is no `phone` column anywhere in the schema.

### 7.1 The split, which the architecture already drew

| Owned by Keycloak | Owned by ITMS |
|---|---|
| password, MFA device, passkeys, **email** | phone, display-name override, timezone/locale, notification preferences |

Credentials cannot move into ITMS: `console-auth` §1 exists so that the password
field is not in this application, and invariant 3 forbids credentials in API
responses. Keycloak's account console at `/realms/it-suite/account` already
does all of the left-hand column, with the re-authentication and verification
flows that make an **email change** a safe operation rather than an
account-takeover pivot — change the address, then request a password reset to
it. Reimplementing that badly is worse than not having it.

So the profile page **deep-links** per action ("Change password", "Manage MFA
device", "Add a passkey") into the account console, which is already routed
(`/realms` PathPrefix) and already wears the ITMS theme, so it does not read as
a different product.

Rejected — **iframing the account console**: Keycloak denies framing by default
(`X-Frame-Options` / `frame-ancestors`), so it would require loosening the realm
on exactly the pages where a hijacked click changes a password or drops an MFA
device. Rejected — **proxying the actions through the provisioning credential**:
it keeps the buttons in ITMS and stays invariant-safe, but routes every
*self-service* action through `manage-users` rather than the user's own
re-authenticated session, which makes ITMS the thing that can reset anybody's
MFA. Self-service should need the user's own session; that is the entire point
of self-service.

### 7.2 The staleness bug this uncovered, which is not cosmetic

`app/api/deps.py` writes `email` and `display_name` **only** in the
`if user is None` branch. They are never refreshed. Change an address in
Keycloak and ITMS shows the old one for ever.

That was untidy before this cycle. It is now a correctness problem: §3's
admission gate tests `claims.email` — the live value — while every screen, every
audit metadata field and the §3.3 preflight read the stale row. **The security
decision and the displayed fact come from different sources.**

Fix: refresh `email` from the claim on each admission when it differs, and audit
the change (`auth.email_changed`) — an address changing is exactly the kind of
identity event §4 of `console-auth` says belongs in the chain.

`display_name` is deliberately **not** refreshed, because §7.1 gives ITMS the
override. The asymmetry is the design: Keycloak is authoritative for the
identity fact, ITMS for the presentation preference.

### 7.3 What the profile owns

`users` gains `phone`, `timezone`, `locale` (migration 0066 — **after** this
cycle's 0065, never beside it). `display_name` already exists and becomes
user-writable. Notification preferences layer onto the existing notification
module's channels and routing rather than inventing a second mechanism.

`PATCH /api/v1/me` writes only these fields, only for the calling user. It
takes **no** `user_id`: the target is `ctx.user_id` from the validated token,
never the body — the same rule `orgs.py` states for the org PATCH, and the
reason an admin cannot escalate through it.

### 7.4 Falsifiers

11. **`PATCH /api/v1/me` alters another user** when handed an id in the body.
12. **The profile route writes `email`, `status`, or anything role-shaped** —
    privilege escalation through a self-service form.
13. **An email changed in Keycloak still shows the old value after sign-in**,
    or changes without an audit event.
14. **A `display_name` set in ITMS is clobbered** by the next sign-in.
15. **Nothing sends SMS**, so a phone number is directory data. If a later
    change treats it as a second factor, that is a new decision and this line is
    where it gets contradicted.
