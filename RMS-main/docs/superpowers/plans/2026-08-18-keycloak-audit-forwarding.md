# Keycloak credential events in the ITMS audit chain — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Passkey registration, credential removal and identity-provider linking — credential changes on privileged accounts — appear in ITMS's immutable hash-chained audit log, not only in Keycloak's own non-tamper-evident event log.

**Architecture:** A worker sweep pulls Keycloak's admin API on a schedule, maps the handful of credential-relevant event types onto ITMS audit actions, resolves each to an org through `users.external_subject`, and writes them through the existing `audit_service`. Deduplicated on a fingerprint, because Keycloak's date filters are coarse and every sweep re-reads an overlapping window.

**Tech Stack:** Python 3.12, httpx, Celery beat, SQLAlchemy 2.x, Alembic, pytest.

## The decision this plan makes, which `console-auth` §7.1 left open

> Options: Keycloak's SPI event listener posting to the API, or a worker sweep pulling the admin events endpoint. The first is push and needs a credential; the second is a sweep and has a lag. Neither is chosen.

**Chosen: the sweep.** The push option means shipping a Java plugin into a Keycloak image this project pins and does not build, and giving Keycloak a credential to call back into ITMS — a new inbound path, authenticated by something Keycloak stores. The sweep needs no plugin, no image change, and no inbound path; its cost is latency bounded by the schedule, which is stated rather than hidden.

**The credential is the part to get right.** The sweep authenticates as a dedicated service-account client with **`view-events` only**. Not the bootstrap admin: that account can reset any password and create any user, and `deploy/GO-LIVE.md` already tells the operator to delete it. The secret is read from a **file path**, following `audit_hmac_key_path`, not from an environment variable holding the secret inline.

## Global Constraints

- **An unconfigured deployment must be VISIBLY unconfigured.** An audit-forwarding feature that silently does nothing is worse than one that does not exist: somebody will read the chain and believe it is complete. No credential → the sweep is a no-op **and** a platform-status probe says so.
- **Never file an event under a guessed org.** Keycloak's realm is shared by every tenant. An event whose user does not resolve to an ITMS user is **counted and reported**, never written to a default org — that would put one tenant's credential change into another's evidence.
- **`view-events` only.** If the implementation ever needs a second Keycloak role to work, stop: that is a change to what this credential can do, and it is the operator's decision.
- **Deduplicate.** Keycloak's `dateFrom`/`dateTo` are day-granular, so every sweep re-reads an overlapping window. A duplicate does not corrupt the hash chain — it pollutes it, which is nearly as bad for evidence.
- **The events this forwards are a closed list**, and it is short: WebAuthn credential registration and removal, federated identity link and unlink. Not logins (`auth.login` already exists), not every admin action — a firehose into the audit log makes the log unreadable, and unreadable evidence is not evidence.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/core/config.py` | Four settings, secret by file path |
| `backend/app/modules/keycloak_events/client.py` | Token, fetch, and nothing else |
| `backend/app/modules/keycloak_events/mapping.py` | Keycloak event → ITMS action, pure |
| `backend/app/modules/keycloak_events/sweep.py` | Resolve org, dedupe, write, report |
| `backend/migrations/versions/0064_keycloak_event_ingest.py` | The dedupe table |
| `backend/app/worker.py` | The task and its beat entry |
| `backend/app/core/health/probes.py` | "configured / never run / stale" |
| `backend/tests/test_keycloak_event_mapping.py` | The closed list, and what it refuses |
| `backend/tests/test_keycloak_event_sweep.py` | Org resolution, dedupe, no-credential |
| `deploy/README.md`, `deploy/.env.example` | The service account, by hand |
| `docs/modules/console-auth/spec.md` | §7.1 answered |

---

### Task 1: The mapping, which is pure and therefore testable alone

- [x] Failing test → `mapping.py` → pass → commit.

The closed list, from `console-auth` §4:

| Keycloak | ITMS action |
|---|---|
| `UPDATE_CREDENTIAL` with `credential_type=webauthn-passwordless` | `auth.passkey_registered` |
| `REMOVE_CREDENTIAL` with the same type | `auth.passkey_removed` |
| `FEDERATED_IDENTITY_LINK` | `auth.identity_linked` |
| `REMOVE_FEDERATED_IDENTITY` | `auth.identity_unlinked` |

Everything else maps to `None` and is dropped. **A test asserts that a login event maps to nothing** — the firehose is the failure mode here, and it is one nobody notices until the audit log is unusable.

---

### Task 2: The dedupe table

- [x] Migration 0064 + model + round trip.

`keycloak_event_ingest(id, org_id, fingerprint UNIQUE, ingested_at)`, RLS as usual. The fingerprint is a SHA-256 over `(realm, time_ms, type, user_id, details)`, so the same Keycloak event fingerprints identically on every sweep. Insert `ON CONFLICT DO NOTHING`; write the audit event **only when the insert took**.

**Retention:** rows older than the audit retention window are swept, because this table exists to stop duplicates within an overlapping fetch window and nothing else reads it.

---

### Task 3: The client

- [x] `client.py`: client-credentials token against `/realms/{realm}/protocol/openid-connect/token`, then `GET /admin/realms/{realm}/events` with `dateFrom`.

Timeouts everywhere; a Keycloak that is slow must not wedge the worker. Errors are raised for the sweep to report, never swallowed.

---

### Task 4: The sweep

- [x] Failing tests → `sweep.py` → pass → commit.

Per run: fetch since the watermark, map, resolve `users.external_subject == event.userId` to an org, dedupe, write, and return counts — `{fetched, written, unresolved, skipped}`. Counts are RETURNED rather than logged, because "the sweep ran without raising" is satisfied by a sweep that wrote nothing.

Required tests:
- no credential configured → returns zeroes and writes nothing, **and does not raise**;
- an event for a user this platform does not know → `unresolved`, nothing written, **and specifically not written to the default org**;
- the same event twice → written once;
- a login event → not written;
- the written event lands in the hash-chained log with the Keycloak user and event type in its metadata.

---

### Task 5: The probe, so silence is visible

- [x] A `keycloak_events` probe: `ok` when configured and swept recently, `degraded` when configured and stale or erroring, and — deliberately — **`ok` with `detail={"configured": false}` when it is not set up at all**, because an operator who has not enabled this has not broken anything. The distinction that matters is *visible*, not *alarming*.

---

### Task 6: The runbook

- [x] `deploy/README.md`: create the `it-suite-events` client (service accounts on, `view-events` from `realm-management`), enable **Admin events** and **User events** in the realm (both are OFF by default — without them this feature has nothing to read and would sit at "configured, zero events" forever), and write the secret to the file the setting names.
- [x] `deploy/.env.example`: the four settings, with the secret as a PATH.

---

### Task 7: Answer §7.1

- [x] `docs/modules/console-auth/spec.md`: §4 and §7.1 updated — the sweep is chosen, the lag is stated, the credential is named and scoped, and what is still true is said plainly: **Keycloak's own log remains the source, and a Keycloak compromised before the sweep runs can drop an event this will never see.** Forwarding puts events in a tamper-evident chain; it does not make their absence detectable.

---

## Verification before claiming completion

- [x] `cd backend && .venv/bin/pytest -q` — green
- [x] `mypy`, `ruff` — clean
- [x] Migration 0064 round-trips against a real Postgres (`upgrade head →
      downgrade base → upgrade head`, ending at `0064 (head)`)
- [x] The no-credential path has a test proving it neither raises nor writes
- [x] The unresolved-user path has a test proving nothing lands in the default
      org — asserted as a global before/after delta, because "not in this org"
      is a weaker claim than "in no org"
- [x] **Nothing here has run against a real Keycloak**, and the spec says so

## What was added beyond the plan, and why

**Task 5's probe needed the status page to render `detail`, and it did not.**
The probe answers `ok` with `configured: false` for a deployment that is not
forwarding — deliberately unalarming, because nothing is broken — so the whole
distinction lives in `detail`. `PlatformStatusPage` discarded it, which would
have rendered `keycloak_events / ok / —` and told an operator forwarding
works. The page now renders detail (`jwks`'s `keys: n` had been invisible for
the same reason since it shipped), with its own falsified guard.

**A guard that the sweep is actually scheduled.** Every other test here would
pass with no beat entry at all: the probe would read `configured, never run`
for ever and the chain would stay empty. The entry is asserted against
`SWEEP_INTERVAL_S` rather than a literal.

**Each rule was falsified by planting the regression it exists for**, with the
plant confirmed on disk before the result was read: a default-org fallback (two
tests red), `ON CONFLICT DO UPDATE` (one red), a catch-all action for unmapped
events (one red), a fixed lookback instead of the watermark (two red), and —
in the runbook guards — `realm-admin` in place of `view-events` and an inline
secret setting (one red each).
