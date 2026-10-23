# Tenancy test efficacy — what the invariant-5 suite can and cannot observe

**Date:** 2026-08-21
**Branch:** `claude/handoff-spec-review-f45idd`
**Prompted by:** `787461f`, whose author wrote a tenancy test, deleted the
`WHERE org_id` it was meant to protect, and watched it pass.
**Invariant under audit:** #5 — *"All data access is tenant-scoped. Every query
is constrained by `org_id`, enforced at the database layer via Row-Level
Security, not only in application code. A missing tenant filter is a security
bug, not a style nit."*

---

## SECURITY FINDING, FIRST AND UNMISSABLY

### F1 — `GET /api/v1/platform/errors/{group_id}` hands any tenant's `org-admin` the org, user, job and device identifiers of every other tenant's errors

> **CLOSED `e25bbe7`.** Migration `0068` moves `platform.errors.read` and
> `platform.errors.write` off `org-admin` and onto `super-admin`. The finding
> as written below stood on the current tree when it was filed; everything from
> "NOT FIXED HERE, DELIBERATELY" onward is superseded by the closure note at the
> end of this section, which records which of the three answers was taken, why,
> and what it costs a running deployment.

**This was live on the tree when this audit was written. It is not a
hypothetical and it is not a redundant assertion.** It is the one place in this codebase where a tenant table
is read with neither an application `org_id` predicate nor an RLS predicate, and
the rows reach an HTTP response.

Three facts compose it, and each is individually defensible:

1. **`app/api/v1/errors.py:166` and `:195` open `AsyncSession(request.app.state.app_engine)`
   — not `org_session`.** No `app.current_org` GUC is ever set for these two
   routes. Everywhere else in the codebase that would mean *zero rows* (the GUC
   read is `missing_ok=true`, so the comparison is NULL and nothing matches),
   which is why the idiom is normally harmless. Here it is not, because —
2. **`platform_error_event`'s only RLS policy is `USING (true) WITH CHECK (true)`**
   (`migrations/versions/0056_platform_errors.py:73-76`), granted `FOR ALL TO
   app_user`. Measured against the migrated schema rather than read off the
   migration:

   ```
   POLICY ROLE BINDING:
      platform_error_event   application_only     cmd=b'*' perm=True roles=app_user USING=true
   ```

   It is the **only** table in the whole schema carrying an `org_id` column
   whose policy `USING` clause never mentions `org_id`. The full sweep is in
   §3.3 below; the other four hits are restrictive `portal_user`-bound policies
   that intersect with `tenant_isolation` rather than replacing it.
3. **`platform_error_event` carries `org_id`, `user_id`, `job_id` and
   `device_id`** (`0056_platform_errors.py:63-66`) and `app/api/v1/errors.py:210-220`
   returns all four verbatim in `ErrorEventOut`.

**Reproduced.** A throwaway probe (written into `tests/`, run, then deleted —
it is not committed) captured one exception with org B's correlation context,
drained it, and fetched the group as an org A administrator holding only
`platform.errors.read`:

```
GROUPS VISIBLE TO ORG A: 1
DETAIL EVENTS ORG A RECEIVED: [{'id': '06a88d1e-0980-7dbe-8000-35ac37b9e60c',
  'occurred_at': '2026-08-21T22:32:00.557973Z', 'request_id': None,
  'org_id': '06a88d1e-08e5-7a65-8000-951c95c5e6f7',
  'user_id': '5e66b939-e59c-4c0d-8cfd-37f7c7667ef3',
  'job_id': None, 'device_id': '177663a4-7b62-46a9-8ce8-5f8806926274'}]
ORG B is: 06a88d1e-08e5-7a65-8000-951c95c5e6f7  ORG A is: 06a88d1e-08bd-7257-8000-27989cd050ad
>>> CROSS-ORG IDENTIFIERS REACHED ORG A'S ADMIN
```

**WHY THE RECORDED JUSTIFICATION DOES NOT COVER IT.**
`tests/test_tenant_column_sweep.py` names `platform_error_event` as a deliberate
exception and says why:

> Carries `org_id` as a nullable CORRELATION column, not as a tenancy key: an
> error at process startup belongs to no org. Access is by the platform-wide
> `platform.errors.read` code, not by tenant scope.

The premise is *"the platform-wide `platform.errors.read` code"*. It is not
platform-wide. `migrations/versions/0057_platform_errors_permission.py:44-48`
grants both codes to the role named `org-admin` — the built-in role every
tenant's own administrator holds:

```sql
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id FROM roles r, permissions p
 WHERE r.name = 'org-admin' AND p.code IN ('platform.errors.read', 'platform.errors.write')
```

`tests/test_error_api.py:110-111` pins that grant as intended behaviour. So the
exemption's reasoning ("a broken code path is platform information") and the
RBAC seed ("every tenant admin may read it") were decided separately and do not
meet. On the single-tenant on-prem deployment this platform runs today, nothing
happens. On the multi-tenant deployment `docs/00-vision.md` is aiming at, a
customer's administrator can enumerate the existence and UUIDs of other
customers' organisations, users and devices.

**Bounded, and the bound is worth stating precisely.** What escapes is
identifiers only. No exception text (`test_error_api.py::test_no_exception_text_in_list_or_detail_response`
pins that, and it holds), no hostname, no email, no parameters. The group rows
themselves (`exc_type`, `module`, `lineno`, `qualname`, `where`) genuinely are
platform facts and the exemption covers them correctly. It is the **event**
rows, and only their four correlation columns, that cross.

**NOT FIXED IN THIS PASS, DELIBERATELY.** There are at least three correct
answers — (a) null the three tenant-bearing columns unless the caller is a
platform operator, (b) filter events to `org_id IS NULL OR org_id = ctx.org_id`
and set the GUC by moving these routes onto `org_session`, (c) re-gate the
routes on a permission `org-admin` does not hold — and they differ in what they
do to the triage workflow the module exists for. Choosing between them is a spec
amendment to `docs/superpowers/specs/2026-08-15-error-monitoring-design.md`
§4.1/§5, not a line edit, and the brief for this audit says survey first. **It
is the one item here that should not wait for the next cycle.**

#### CLOSED 2026-08-22, `e25bbe7` — answer (c), and what it cost

The operator chose **(c)**: take the codes off `org-admin`. (a) and (b) were
rejected for the same reason, which the finding above only half states — the
cross-org view is not the defect, it is the feature. "This line has failed 4,000
times, for these orgs, these users, these devices" is the triage the module
exists to serve, and both alternatives delete it for everyone in order to fix
who could see it.

`super-admin` was already the right holder and already had the properties this
needs: `0003` describes it as "Cross-org platform administration (SaaS
operator)", `app/core/rbac_admin.py` will not list, clone, edit or delete it,
`app/modules/user_admin/service.py` will not grant, revoke or disable it, and
`0065`'s database function refuses it by name. `0003`'s `ALL` was computed from
`0003`'s own ten codes, so it never received `platform.errors.*`; `0068` is the
grant.

**Three things building it turned up that the finding did not predict.**

1. **The revocation would have been cosmetic on its own.** `org-admin` also
   holds `roles.manage` and `users.manage`, so the same administrator could
   create a role carrying `platform.errors.read`, assign it to themselves, and
   undo `0068` in two console calls with no SQL. `app/core/rbac_admin.py`'s own
   docstring licensed exactly that — "it does not stop a caller with
   `roles.manage` from adding codes to a role they hold … `org-admin` already
   holds every code" — a justification whose premise `0068` is what falsified.
   So the fix also excludes `PLATFORM_OPERATOR_CODES` from `/api/v1/permissions`,
   refuses them in `_validate_codes` with a 403, and replaces
   `set_role_permissions` so the refusal holds below the service layer too.

2. **A test was manufacturing the grants.** `0054`, `0057` and `0060` each open
   their "who gets it" paragraph saying `org-admin` "is not a choice", because
   `test_rbac.py::test_org_admin_has_all_permissions` asserted an org-admin's
   codes equal `set(PERMISSIONS)` — so a platform-scoped code in the dict and
   not seeded to every tenant's administrator turned the suite red. That is the
   mechanism, and it would have produced the next one. The assertion now reads
   `set(PERMISSIONS) - PLATFORM_OPERATOR_CODES`: the useful property survives,
   and a new `platform.*` code becomes a decision instead of an instruction.

3. **`super-admin` is quietly less privileged than `org-admin`, and this
   audit's scope did not cover it.** Four migrations grant to `org-admin`
   alone — `0054` (`platform.status.read`), `0057`, `0060`
   (`agent.release.*`) and `0062` (`asset.*`) — so `super-admin` holds none of
   those four families. It is now the only holder of `platform.errors.*` while
   not holding `platform.status.read`, which is incoherent but not unsafe, and
   fixing it is a separate decision about what a SaaS operator role means.

**Deliberately not moved: `platform.status.read`.** Same mechanical origin
(`0054` says so in as many words), different consequence: it reads probe
results, process heartbeats and the module registry, none of which come from a
table carrying `org_id`, so it discloses no tenant row. Revoking it would take a
working page from the on-prem administrator and close nothing. It should move
before this platform is multi-tenant; it is recorded here rather than decided.

**Worth a finding of its own, and outside this audit's frame:**
`agent.release.manage` is granted to `org-admin` by `0060`, and
`agent_releases` is in `test_tenant_column_sweep.py`'s `GLOBAL_BY_NATURE` — one
catalogue shared by every tenant. On a multi-tenant deployment that is any
customer's administrator publishing "the bytes every endpoint in the fleet will
execute" (`0060`'s own words) for every other customer's fleet. It is not a
tenancy-test-efficacy question, which is why it is a pointer and not an F4, but
it is the larger sibling of what F1 turned out to be.

**What a deployment that has already run `0057` loses.** Every `org-admin` —
on the single-tenant on-prem install, the operator's own everyday account —
gets `403 {"detail": "requires platform.errors.read"}` from both GET routes and
`403 ... requires platform.errors.write` from resolve and unresolve, and the
console's Error Monitor nav entry disappears by itself because
`frontend/src/shell/nav.ts:129` gates it on `platform.errors.read` read from
`/me`. No frontend change was needed and none was made. Restoring access means
assigning the global `super-admin` role by direct `user_roles` insert, which is
deliberately unreachable from every console surface.

**The regression test asserts both sides** (`test_error_api.py::
test_an_org_admin_is_refused_and_a_super_admin_is_not`): an `org-admin` gets
403 from both GET routes and a `super-admin` gets 200 from both, so the fix
cannot pass by refusing everyone.

Two smaller findings that *were* fixed appear as F2 and F3 in §2.

---

## 0. Method, and what it can and cannot see

Two engines exist and the whole question turns on which one a test drives
(`app/core/db.py:1-6`, `tests/conftest.py:52-77`):

| Role | Fixture / entry point | RLS |
|---|---|---|
| `app_user` | `app_engine`, `org_session()`, every HTTP request via `api_env.client` | **NOBYPASSRLS** — Postgres rejects cross-tenant rows on its own |
| `portal_user` | `portal_engine`, `portal_session()` | **NOBYPASSRLS**, plus a restrictive per-requester policy |
| `its_admin` | `admin_engine`, `admin_session()` | **BYPASSRLS** (`conftest.py:76`, `deploy/postgres/init-app-user.sh:38`) |

**The mechanism, stated exactly.** `0002_rls.py` puts `ENABLE + FORCE ROW LEVEL
SECURITY` and a `tenant_isolation` policy — `USING (org_id = current_setting('app.current_org', true)::uuid)`
— on every tenant table, and `tests/test_rls.py::test_every_org_scoped_table_has_forced_rls_policy`
holds every later migration to it. So inside an `org_session`, a `SELECT` with
no tenant predicate at all returns exactly the same rows as one carrying
`WHERE org_id = :org`. **Deleting the application predicate is therefore
unobservable through any test that reaches the database as `app_user`** — which
is every HTTP test and every service test driven through `org_session`.

Tests were classified by parsing `backend/tests/test_*.py` with `ast`, selecting
every test function whose **name or docstring** claims tenancy (`tenan|cross_org|
other_org|org_scop|leak|neighbour|isolat|another_org|wrong_org|not_visible|
does_not_see|cannot_see|rls|...`), and asking of each: *does the code under test
run on a BYPASSRLS session?* Three idioms make it so, all three of which are
already in the tree:

1. `async with admin_session(engine=...)` / `AsyncSession(admin_engine)`, with the
   bound session passed to the function under test — `tests/test_reporting_patch_compliance.py:287`.
2. The same, with the session passed to a service directly — `tests/test_device_groups_api.py:400-403`.
3. Repointing the running app: `users_env.app.state.app_engine = users_env.admin_engine`,
   then driving the **real route** through it — `tests/test_user_admin_api.py:431`,
   `tests/test_rbac_admin_api.py`, `tests/test_org_settings.py`.

**THE CLASSIFIER'S BLIND SPOT IS WHERE BOTH REAL DEFECTS WERE HIDING, so it is
stated up front.** A fourth shape exists: a function that takes `admin_engine`
as a *parameter* because it genuinely needs one (the retention sweeps in §2).
Every test of such a function passes `admin_engine` whether or not it seeds a
neighbour org, so the idiom carries no information and no static rule can tell
an observable test from a vacuous one. Both fixed findings below were found by
reading those functions by hand, not by the classifier — and the classifier
still files the two tests added to cover them under D.

## 1. Part 1 — how much of the tenancy suite is vacuous

Against the current tree (5,410 test functions across 390 files; 6,945 tests
after parametrisation):

| | Class | Count |
|---|---|--:|
| **A** | **Observable on a BYPASSRLS session — the application predicate can fail** | **255** |
| B | No database at all — static/registry/metadata assertions | 85 |
| C | DB-layer only — exercise no application code, so **RLS itself is the subject** | 135 |
| **D** | **Exercise application code under RLS — the application predicate is NOT observable** | **221** |
| | of D, through HTTP routes | 72 |
| | of D, through `org_session` directly | 149 |
| | **Total claiming tenancy** | **696** |

Two of the 221 in D are the tests this audit added (§2); they are demonstrably
observable — proven by watching them fail — and are counted in D only because
of the blind spot in §0. So the honest split is **A = 257, D = 219**.

### 1.0 Re-derived independently while building R3–R6 (2026-08-22)

The classification was rebuilt from scratch rather than taken on trust, against
the same tree plus R1/R2/R7. It agrees on the number that matters and disagrees
on two, and both disagreements have a cause worth writing down.

| | Class | This audit | Re-derived | Why |
|---|---|--:|--:|---|
| **A** | Observable on BYPASSRLS | **255** | **252 (+13 unresolved)** | Agreement. |
| B | No database | 85 | 120 | Broader claim regex (see below). |
| C | DB-layer only | 135 | 106 | Boundary with D; see below. |
| **D** | App code under RLS | **221** | **254** | Broader claim regex. |
| | **Total claiming tenancy** | **696** | **745** | +49 from the regex. |

**The +49 is the regex, not a finding.** The re-derivation added `org_filter`,
`org_predicate`, `foreign_org`, `per_org` and `second_org` to the claim
vocabulary, which pulls in the whole `..._org_filter_is_load_bearing` family —
tests that are class A and were never in question.

**The A/C/D boundaries are where a classifier earns or loses its answer, and
three rules decide them.** Written down because the first two passes got each
one wrong in a way that moved hundreds of tests:

1. **ORM models and Pydantic schemas are not "application code under test."**
   Nearly every test imports them, and a seed block doing `session.add(Device(…))`
   inside an admin session otherwise scores as application code running
   BYPASSRLS. Counting them put A at 416.
2. **`org_session` / `admin_session` are not application code either.** They are
   the plumbing under test, not the subject; counting the context manager as an
   app call turned the entire honest-RLS C family into D (C fell to 53).
3. **`nm[:1].islower()` is not a test for snake_case.** `"_".islower()` is
   False, so every leading-underscore private — `_tickets_out`, `_scope`,
   `_load_license` — was filed as a type, and the A tests calling them directly
   on a raw admin session were misclassified as D. Fixing this alone moved
   `test_email_intake_api.py::test_the_message_log_org_predicate_is_load_bearing`
   and `test_service_desk_api.py::test_tickets_out_has_attachments_org_predicate_is_exercised`
   from D to A, which is where they belong — both say so in their own
   docstrings.

**R5's FIXTURE IS A FOURTH IDIOM, AND A CLASSIFIER THAT DOES NOT KNOW IT IS
WRONG BY FIFTEEN.** §0 lists three shapes that make a test observable. `e78f730`
adds `bypassrls_session`, and the first run of the re-derived classifier against
the finished tree scored every test built on it as D — A moved by one instead of
by fifteen — because the fixture name appears in no `with` header. Anyone
re-running this measurement must treat a `bypassrls_session` parameter as a
BYPASSRLS binding. That is the cost of the fixture, stated where the next
measurement will look for it.

**The remaining 13 "unresolved" are §0's blind spot, unchanged.** Functions that
take `admin_engine` as a parameter because they need one: the idiom carries no
information, and no static rule separates an observable test from a vacuous one.
They still have to be read by hand.

**Where the re-derivation disagrees with a docstring rather than with a count.**
Four tests claimed an admin-engine assertion as an RLS-free observation of a
predicate and are class D all the same, because the ACTION still goes through
the route on the app engine and only the verification is on the admin engine.
They are named in the R3 row below; their docstrings are corrected rather than
their assertions.

**84 files, not 80, contain a D test and no A test** — the same +49 regex effect.
Fifteen of the 84 now have one (R4).

**"Vacuous" is the wrong word for most of D, and the distinction matters.**

* **C is not vacuous and is not overclaiming.** These 135 tests assert what RLS
  does, by writing a row under org A and reading it back under org B with no
  application code in the path at all. `tests/test_notification_tenancy.py` is
  the model: its docstring says in as many words that `admin_engine` "would pass
  every test in this file even with all six policies dropped entirely, so it is
  used ONLY to seed the prerequisite rows", and it records the red-verification
  it did — recreating one policy as `USING (true)` and watching the test go red.
  That file is what an honest RLS test looks like.
* **D is the population at issue.** These tests call a service or a route,
  assert that org B's row is unreachable, and would pass with the service's
  `WHERE org_id` deleted. They are still testing *something* — that the route
  answers 404 rather than 500, that the error type is right, that a 404 is not a
  403 existence oracle — but not the thing several of their names promise.
* **A is large, and that is the good news.** 255 tests already do exactly what
  `787461f` reached for. The convention is established well beyond one commit:
  `test_sla_*.py` has a whole family named `test_tenancy_..._org_filter_is_load_bearing`,
  the four `test_reporting_*.py` files each carry `test_the_org_predicate_holds_where_rls_is_not_doing_the_rejecting`,
  and `test_ipam_*.py` uses `..._on_the_admin_engine` as a naming suffix.

**Where D is unaccompanied.** Of the 125 files containing a D test, **80 contain
no A test at all**. That is the number to act on if anything systematic is done
later; the list is dominated by files whose subject is not primarily a query
(`test_error_capture_wiring.py`, `test_worker_sweep_containment.py`) but includes
several that are (`test_licenses_service.py` ×4, `test_bcp_plan_service.py` ×3,
`test_ai_admin_api.py` ×4, `test_domain_policy.py` ×5).

### 1.1 Spot-checks — predicates actually deleted, tests actually run

Four falsifications beyond the two in §2. Everything was restored; `git status`
at the end of this audit shows two modified files, both under `backend/tests/`.

**(i) `app/modules/health/profiles.py::get_profile` — the pairing works.**
Deleted `HealthProfile.org_id == org_id` from the `select`:

```
$ .venv/bin/pytest tests/test_health_profiles.py -q
>       assert leaked == "refused"
E       assert "B's profile" == 'refused'
tests/test_health_profiles.py:931: AssertionError
FAILED tests/test_health_profiles.py::test_list_and_get_carry_their_own_org_predicate
1 failed, 57 passed in 12.27s
```

The D test — `test_getting_another_orgs_profile_raises_not_found`, whose
docstring is *"404, never 403 — a 403 confirms the id exists, which is a
cross-tenant existence oracle"* — **passed**. The A test in the same file, on the
admin engine, is the only thing that went red. This is the healthy shape: the D
test is about the status code, the A test is about the predicate, and the file
has both.

**(ii) `app/modules/subscriptions/service.py::get_subscription` — covered, but
only by accident and from another file.** Deleted `Subscription.org_id == org_id`:

```
$ .venv/bin/pytest tests/test_subscriptions_service.py -q
42 passed in 6.26s
```

Its own file — including `test_tenancy_other_orgs_subscription_is_unreachable`
(renamed to `test_another_orgs_subscription_is_hidden_by_rls` by R3),
which drives exactly this function — is green with the predicate gone. Widening
to the module:

```
$ .venv/bin/pytest tests/test_subscriptions_service.py tests/test_subscriptions_api.py \
    tests/test_subscriptions_lifecycle.py tests/test_subscriptions_seats.py \
    tests/test_subscriptions_reporting.py -q
>           with pytest.raises(SubscriptionNotFoundError):
E           Failed: DID NOT RAISE SubscriptionNotFoundError
tests/test_subscriptions_seats.py:224: Failed
FAILED tests/test_subscriptions_seats.py::test_assign_seat_cross_org_subscription_org_filter_is_load_bearing
1 failed, 101 passed in 19.49s
```

So the predicate *is* pinned — by a seat-assignment test in a different file
that happens to call through it. That is coverage by luck of the call graph. It
holds today and would evaporate the moment `assign_seat` stopped resolving the
subscription through `get_subscription`.

**(iii) and (iv)** are F2 and F3 in §2 — both uncovered, both fixed.

### 1.2 The one place a *test* overclaims in its name

`tests/test_api_routes.py::test_device_detail_404_for_foreign_org` reads as an
assertion about the device route's tenant scoping. It cannot be: **`app/api/v1/devices.py`
contains no application `org_id` predicate at all.** `grep -cE '\.org_id == ' app/api/v1/devices.py`
returns 0 across eight `org_session` blocks; `get_device` (`:530-536`) is
`select(Device, ...).where(Device.id == device_id)` and nothing else. The test is
not wrong — RLS is the enforcement and the 404 is real — but there is no
application filter for it to be protecting. This is discussed as a design
position, not a defect, in §3.1.

## 2. Part 2 — every path where RLS is not the backstop

Enumerated exhaustively: every `admin_session(`, every `AsyncSession(admin_engine`,
every `create_async_engine(settings.database_admin_url)`, and every
`AsyncSession(` in `app/` that is not one of those, across `app/`, `scripts/`
and `deploy/`.

### 2.1 The dominant pattern, and it is correct

Every `*_all_orgs` shell in `app/worker.py` (19 of them), plus the module-owned
equivalents in `health/sweep.py::_for_each_org`, `sla/sweep.py`,
`ipam/reconcile.py`, `ipam/retention.py`, `core/ai/retention.py` and
`email_intake/poller.py`, uses the BYPASSRLS engine for **exactly one statement**:

```python
async with AsyncSession(admin_engine) as session:
    org_ids = list((await session.execute(select(Organization.id))).scalars())
```

and then does all work inside `org_session(org_id, engine=app_engine)`. RLS is
fully in force for every row that is read or written. Verified at
`app/worker.py:270, 366, 533, 606, 664, 699, 853, 948, 990, 1041, 1115, 1193,
1288, 1368, 1451, 1553` and in each module file named above. **Verdict: safe,
and the reasoning is already written down at each site.**

### 2.2 The full table

| # | Path | Engine / role | Verdict |
|---|---|---|---|
| 1 | `app/worker.py` — all `*_all_orgs` shells | admin **only** to list `Organization.id`; work under `org_session` | **Safe.** RLS in force for every row. |
| 2 | `app/modules/security_logs/retention.py::apply_retention:41-58` | **BYPASSRLS `DELETE`** | **F2 — filter correct, coverage absent. FIXED (test added).** |
| 3 | `app/modules/remote/recording.py::apply_recording_retention:80-100` | **BYPASSRLS `SELECT` + `UPDATE` + object-store delete** | **F3 — filter correct, coverage absent. FIXED (test added).** |
| 4 | `app/api/v1/errors.py:166, 195` | `app_engine`, **no tenant GUC**, table policy `USING (true)` | **F1 — live cross-tenant disclosure. See §F1.** |
| 5 | `app/worker.py:2036, 2063, 2091` (`sample_health_all`, `prune_health_samples_all`, `drain_platform_errors_all`) + `app/main.py:68` | `app_engine`, **no tenant GUC** | **Safe.** `platform_health_sample` has no `org_id`; the error tables are F1's, and the worker only writes/prunes, never serves. |
| 6 | `app/modules/keycloak_events/sweep.py::_window_start:98-105` | `admin_session`, **global** `max(ingested_at)` | **Safe but under-documented.** Deliberately fleet-wide (the Keycloak realm is), so org A's ingest advances org B's fetch window. The docstring explains the watermark and never says it is cross-org. |
| 7 | `app/modules/keycloak_events/sweep.py::_resolve_org:118-127` | `admin_session`, `users.external_subject` → org | **Safe by design and documented.** Cross-org is the point; `LIMIT 2` and `len(rows) != 1 → None` refuses to guess when a subject exists in two orgs. Fingerprint dedupe is globally unique on purpose (`0064:57`). |
| 8 | `app/gateway/main.py::_resolve_agent:203-210` | admin, `agents.cert_fingerprint` → `(device, org)` | **Safe.** Pre-tenant by necessity — the certificate *is* the tenant proof (invariant 2). Comment says so. |
| 9 | `app/api/deps.py::auth_context:62-70` | admin, `organizations.slug` → id | **Safe.** The only pre-tenant step in the request path; read-only; everything after is `org_session`. |
| 10 | `app/api/v1/ws.py:86-92` | admin, slug → id | **Safe.** The `users.status` lookup beside it was deliberately put *inside* `org_session` rather than bolted onto this read; the comment names invariant 5 as the reason. |
| 11 | `app/modules/remote/tunnel.py::authorize_tunnel:287-292` | admin, slug → id | **Safe.** Same shape; every authorisation decision after it is inside `org_session`. |
| 12 | `app/services/enrollment.py::enroll:204-209` | admin, `enrollment_tokens.id` → `org_id` | **Safe.** Picks the tenant, does not authorize; the token is consumed by a guarded `UPDATE` inside `org_session`. |
| 13 | `app/api/v1/setup.py:302, 336` | `admin_session` | **Safe.** First-boot claim, before any organisation exists. Serialised on a `pg_advisory_xact_lock`; refuses once any user exists. |
| 14 | `app/modules/ai_admin/api.py::_owning_org:345-348` | `admin_session`, one column | **Safe, covered.** Returns `ai_session.org_id` only, to raise 409 instead of a silently empty transcript. Pinned by `test_ai_admin_store.py::test_another_orgs_session_is_not_found_on_the_admin_engine` (class A). |
| 15 | `app/modules/reporting/` (all of it) | `org_session` on `app_engine` throughout (`api.py:92, 141, 180`; `deps.py:72`) | **Safe, and the best-covered layer in the repo.** Every definition carries explicit predicates (`grep '\.org_id == ' definitions/` → 20 hits) and each of the four has an A test named `test_the_org_predicate_holds_where_rls_is_not_doing_the_rejecting`. The `jobs.params`/`signature`/`nonce` denylist (`registry.py`, `5417267`) is a *column*-level control and is orthogonal to tenancy; it holds. |
| 16 | `app/gateway/dispatch.py`, `app/jobs/engine.py` | `org_session` throughout (`dispatch.py:194, 249, 352, 412`) | **Safe.** No admin session in the job path at all. |
| 17 | `app/modules/deploy/artifacts.py::LazyObjectStoreSigner` | `org_id` passed in by the caller | **Safe, covered** by `test_deploy_artifacts.py::test_the_refresher_will_not_cross_orgs_without_rls_to_stop_it` (class A). |
| 18 | `scripts/preflight_email_domain.py:161-173` | admin, **cross-org by design** | **Safe.** An operator CLI that must see every tenant; refuses to run on the app DSN with the message *"the application role sees one tenant at a time and would silently under-report."* |
| 19 | `scripts/demo_e2e.py`, `smoke_site_server.py`, `verify_backup_run.py` | admin, throwaway clusters | **Safe.** Seed-and-verify harnesses against a database they create. |
| 20 | Migrations (`alembic`), `deploy/postgres/init-app-user.sh` | `its_admin` (BYPASSRLS) | **Safe by definition**, but see §3.3 — the checks that guard them do not inspect policy *shape*. |

### F2 — `security_logs` retention: the only cross-tenant `DELETE` in the platform, with no test on its predicate

`app/modules/security_logs/retention.py` deletes on the admin engine because it
must — `0006`'s append-only grants deny `app_user` any `DELETE` on
`security_events`. So `SecurityEvent.org_id == org_id` in the batch select
(`:46`) is the entire isolation boundary, and it guards a **destructive**
operation on the evidence trail.

`tests/test_security_retention.py` had three tests. Every one seeded a single
org, so the returned count and the surviving rows were identical with or without
the predicate. Falsified by deleting it:

```
$ sed: .where(SecurityEvent.org_id == org_id, SecurityEvent.ts < cutoff)  ->  .where(SecurityEvent.ts < cutoff)
$ .venv/bin/pytest tests/test_security_retention.py -q
3 passed in 3.47s
```

`test_retention_does_not_delete_a_neighbour_orgs_events` was added. Against the
broken code:

```
>       assert deleted == 4
E       assert 10 == 4
tests/test_security_retention.py:128: AssertionError
1 failed, 3 passed in 3.46s
```

The predicate was restored and all four pass. The test asserts both directions —
the neighbour's eight rows survive **and** the target's four still go — so it
cannot pass by sweeping nothing.

### F3 — `remote` recording retention: same shape, and the miss is irreversible

`app/modules/remote/recording.py::apply_recording_retention` selects expired
sessions on the admin engine, **deletes each recording out of object storage**,
then blanks `recording_ref`/`recording_bytes`. `RemoteSession.org_id == org_id`
(`:84`) is again the only scoping, and unlike a read that returns too much, a
miss here destroys another tenant's session recordings permanently.

The three existing tests each seed one org. Falsified:

```
$ sed: removed `RemoteSession.org_id == org_id,` from the expired-sessions select
$ .venv/bin/pytest tests/test_remote_recording.py tests/test_remote_retention_worker.py -q
4 passed in 4.60s
```

`test_retention_does_not_expire_a_neighbour_orgs_recording` was added. The
neighbour holds a recording of exactly the age the victim's 30-day policy sweeps
but its own 90-day default does not, so the predicate is the only thing that can
tell them apart. Against the broken code:

```
>       assert deleted == 1
E       assert 2 == 1
tests/test_remote_recording.py:192: AssertionError
1 failed, 3 passed in 4.42s
```

Restored; `9 passed in 5.39s` across the three affected files.

## 3. Part 3 — the judgement

**It is a different answer for each of three layers, and collapsing them into one
verdict is how the question stayed open this long.**

### 3.1 The kernel API — (a) fine as-is, by explicit design

`app/api/v1/` contains **2** application `org_id` predicates across **40**
`org_session` blocks. `app/modules/` contains **641**.

```
$ for f in app/api/v1/*.py; do echo "$(grep -c 'org_session(' $f) $(grep -cE '\.org_id == ' $f) $(basename $f)"; done | awk '$1>0'
8 0 devices.py      5 0 roles.py       3 0 jobs.py           2 0 me.py        2 0 audit.py
6 1 credentials.py  4 0 enrollment.py  2 0 realm_settings.py 2 0 errors.py    2 0 device_groups.py
3 1 orgs.py         1 0 ws.py
```

Devices, jobs, audit events, roles, enrollment tokens, `me`, realm settings —
the kernel's own reads — carry **no** application tenant filter and rely wholly
on RLS. That is not an oversight; it is invariant 5 read the way it is written
(*"enforced at the database layer via Row-Level Security, **not only** in
application code"*). There is no belt to test here because the design says the
braces are the mechanism. `test_device_detail_404_for_foreign_org` and its
siblings are correct; they simply test RLS, and their names should not be read
as claims about a filter that does not exist. **(`device_groups.py` shows 0 only
because its logic lives in `app/services/device_groups.py`, which carries 3.)**

### 3.2 The module layer — (b) a documentation problem, on a base of real coverage

The 641 predicates in `app/modules/` are belt-and-braces: real, correct, and —
in 255 tests — genuinely pinned where RLS is not the thing rejecting. The
remaining 219 D tests are not broken; they assert error codes, existence-oracle
avoidance and route behaviour, which are worth asserting. The problem is
vocabulary. Names like `test_tenancy_other_orgs_subscription_is_unreachable`
(as it then was)
promise the predicate and deliver RLS, and a reader auditing invariant 5 by
grepping for `tenancy` will count them as coverage they are not.

The repo has already solved this and just has not applied it evenly. Three
naming conventions in the tree say exactly what a test observes:

* `..._org_filter_is_load_bearing` — the `test_sla_*` / `test_budget_*` family;
* `..._on_the_admin_engine` — the `test_ipam_*` / `test_site_server_*` family;
* `test_the_org_predicate_holds_where_rls_is_not_doing_the_rejecting` — all four
  `test_reporting_*` files.

**Nothing was renamed in this pass** — 219 renames is exactly the systematic
re-shaping the brief said to propose rather than build. It is R1 below.

### 3.3 The BYPASSRLS paths and the schema guards — (c) a real gap, now three-quarters closed

Two of the twenty paths in §2.2 were unprotected by any test (F2, F3) and both
guarded destructive operations. Both are fixed. One (F1) is a live disclosure
and is not fixed here because the fix is a decision.

The fourth part of the gap is in the guards themselves. Both schema-level
invariant-5 checks — `tests/test_rls.py::test_every_org_scoped_table_has_forced_rls_policy`
and `deploy/postgres/assert-rls.sql` — assert that a tenant table has RLS
enabled, forced, **and at least one policy**. Neither asserts that the policy's
predicate mentions `org_id`. A tenant table with `USING (true)` passes both, and
one already does. Measured against the migrated schema:

```
TENANT TABLES WHOSE POLICY USING-CLAUSE NEVER MENTIONS org_id:
    audit_events         | portal_isolation | USING: None | CHECK: (actor_id = current_setting('app.current_requester')::uuid)
    platform_error_event | application_only | USING: true | CHECK: true
    roles                | roles_insert     | USING: None | CHECK: (org_id = current_setting('app.current_org')::uuid)
    ticket               | portal_isolation | USING: (requester_id = ...) | CHECK: (requester_id = ...)
    users                | portal_isolation | USING: (id = ...)           | CHECK: (id = ...)
count: 5
```

Four of the five are fine and the query is what proves it: `roles_insert` is an
`INSERT` policy whose scoping is in `WITH CHECK`, and the three
`portal_isolation` rows are **restrictive** (`polpermissive = False`) and bound
to `portal_user` alone, so they intersect with `tenant_isolation` rather than
widening it —

```
audit_events   portal_isolation cmd=* perm=False roles=portal_user
audit_events   tenant_isolation cmd=* perm=True  roles=PUBLIC USING=(org_id = current_setting('app.current_org')::uuid)
ticket         portal_isolation cmd=* perm=False roles=portal_user USING=(requester_id = ...)
ticket         tenant_isolation cmd=* perm=True  roles=PUBLIC USING=(org_id = current_setting('app.current_org')::uuid)
```

— which is a genuinely good piece of design. `platform_error_event` is the sole
real hit, and the sweep that exists to catch exactly this class of thing does
not see it.

## 4. Recommendations — proposed, not built

| # | Recommendation | Size |
|---|---|---|
| **R1** | **LANDED `e25bbe7`, and the decision went the other way from the recommendation.** The operator chose (c) — re-gate on a code `org-admin` does not hold — rather than the nulling this row recommended, because nulling the four columns removes the cross-org correlation that is the module's purpose for every caller in order to fix which callers may see it. `0068` moves both codes to `super-admin`; §4.1 of the spec carries the amendment and `test_tenant_column_sweep.py`'s justification text was rewritten as this row asks. The full closure note is under F1 above. The original text follows. **Decide F1 and amend the spec.** `docs/superpowers/specs/2026-08-15-error-monitoring-design.md` §4.1 exempts `platform_error_event` from tenancy on the premise that `platform.errors.read` is platform-wide; `0057` grants it to `org-admin`. Either the exemption or the grant has to move. Recommended: keep the group rows platform-wide (they are), and null `org_id`/`user_id`/`job_id`/`device_id` in `ErrorEventOut` for any caller whose `ctx.org_id` does not match the row — the correlation id an operator actually triages with is `request_id`, which is not tenant-bearing. Whatever is chosen, `test_tenant_column_sweep.py`'s justification text must be rewritten to match. | Small code, real decision |
| **R2** | **LANDED `e25bbe7`.** Both guards now require a tenant table to carry at least one *permissive* policy whose `pg_get_expr(polqual, ...)` references `org_id` — `test_rls.py::test_every_org_scoped_table_has_a_policy_that_mentions_org_id` and block 3 of `deploy/postgres/assert-rls.sql`, with `platform_error_event` as the single named exemption and its reason written at both sites. `polpermissive` is read rather than assumed, because a restrictive `portal_isolation` policy mentioning `org_id` would otherwise score `audit_events`/`ticket`/`users` as tenant-scoped by a policy that is not the tenant rail. Three falsifiers run: a `USING (true)` tenant table fails, a table whose only `org_id` predicate is restrictive fails, and a properly scoped one passes — plus a fourth asserting the exemption is still LOAD-BEARING, so an exemption that stops being needed goes red instead of standing forever. The original text follows. **Teach both schema guards to inspect policy shape.** `test_rls.py::test_every_org_scoped_table_has_forced_rls_policy` and `deploy/postgres/assert-rls.sql` should require that a tenant table have at least one *permissive* policy whose `pg_get_expr(polqual, ...)` references `org_id`, with a named exception list carrying a written reason — the shape `test_tenant_column_sweep.py` already uses for columns. Today `platform_error_event` is the only entry that list would need. Falsifier: a new tenant table given `USING (true)` must turn the sweep red. | Small |
| **R3** | **LANDED `031235e`, and forty-five rather than two hundred.** The ~219 in this row was the whole class-D population; re-deriving it (§1.0) gives 254, but only **50** put a mechanism word — `tenancy`, `tenant_scoped`, `org_scoped`, `isolation` — in the NAME, and those are the ones a `grep tenancy` counts as coverage. The rest already name their observation ("is a 404 not another org's row", "rejects a cross-org calendar"), and renaming an honest name is churn that buries the fifty that matter. Five of the fifty were left alone after reading them: two describe running INSIDE a tenant session rather than claiming to verify one (`test_approved_actions_fuses.py`, `test_ipam_registry.py`), one is a Redis counter with no RLS in the picture (`test_notification_api.py::test_test_send_rate_limit_is_scoped_per_org`), one is about an exception not being swallowed (`test_sla_wiring.py`), and one is a per-request read (`test_domain_policy.py::test_resolve_reads_the_setting_out_of_the_tenant_session`). No assertion was changed. Four docstrings were: `test_asset_promote.py`, `test_asset_read_paths.py`, `test_asset_suggestions.py` and `test_enrollment_matching.py` each cited an admin-engine assertion as the RLS-free observation of a predicate, when the action still goes through the route on the app engine — what the admin-engine read actually establishes is that the fixture had something to leak, or that a refused write did not half-apply. `test_bcp_findings.py::test_list_findings_org_scoped_no_audit` seeds ONE org and had no neighbour to be scoped against at all. The original text follows. **Rename D tests to say what they observe.** Adopt one of the three conventions already in the tree. A test that drives `org_session` and asserts cross-org invisibility is testing RLS and should be named for it (`..._is_hidden_by_rls`, matching `test_notification_tenancy.py`'s honesty); `tenancy`/`org_filter` should be reserved for class-A tests. ~219 tests. **Do not do this as a bulk `sed`** — the classifier in §0 has a known blind spot and would mislabel the retention family. | Large, mechanical, needs judgement |
| **R4** | **LANDED `e78f730` — 15 of the 84 (not 80; see §1.0). Each falsified by deleting the predicate it guards, watching it fail, and restoring.** `test_licenses_service.py`, `test_licenses_vendor.py`, `test_bcp_plan_service.py`, `test_domain_policy.py`, `test_ai_admin_api.py`, `test_provisioning_repository.py`, `test_change_mgmt_service.py`, `test_change_mgmt_links.py`, `test_deploy_scripts.py`, `test_backup_repository.py`, `test_subscriptions_lifecycle.py`, `test_patch_api.py`, `test_notification_api.py`, `test_ipam_api.py`, `test_budget_api.py`. Where a module offered several predicates the test took the DESTRUCTIVE one, because a read that crosses tenants is the smaller half of this: `run_script` builds SIGNED jobs the agent executes as SYSTEM, `open_repo_key` returns backup key material, `revoke_approval` stops another tenant's patch rollout, `delete_subnet(cascade=True)` rewrites five tables, `cancel_subscription` ends a paid service. **66 REMAIN, and they are not all the same kind of remaining.** (Re-measured after the change rather than subtracted: 84 minus 15 is 69, but the renames moved three files' names in and out of the claim vocabulary.) `test_ai_portal.py` and `test_inventory_api.py` belong to modules with ZERO application `org_id` predicates, and the ~28 kernel-layer files (`test_api_routes.py`, `test_error_api.py`, `test_rbac.py`, `test_org_settings.py`, `test_credentials_api.py`, the `test_asset_*`, `test_ai_act_*` and `test_worker_*` families) are §3.1's design position — `app/api/v1/` carries 2 predicates against `app/modules/`'s 641 — so for those the honest answer is that there is no application filter to observe, not that a test is missing. The remainder with real predicates to cover are the rest of the `backup`, `deploy`, `patch`, `reporting`, `sla`, `bcp`, `licenses`, `change_mgmt`, `provisioning`, `subscriptions`, `ipam`, `email_intake`, `service_desk`, `site_server` and `portal` families. `bypassrls_session` (R5) makes each one about fifteen lines. The original text follows. **Add an A test to the 80 files that have a D test and no A test**, prioritising the ones whose subject is a query: `test_licenses_service.py`, `test_bcp_plan_service.py`, `test_domain_policy.py`, `test_ai_admin_api.py`. Each is one test of the `test_device_groups_api.py::test_the_query_is_tenant_scoped_where_rls_is_not_the_thing_rejecting` shape. | Medium |
| **R5** | **LANDED `e78f730`, after R3 as this row asks.** `bypassrls_session` in `tests/conftest.py` yields an `AsyncSession` on the BYPASSRLS `its_admin` role; all fifteen R4 tests are built on it. It opens NO transaction, deliberately — services under test call `session.commit()` themselves, and the `session.begin()` shape `admin_session()` uses closes the block's transaction out from under a test that then wants to read the rows back. Its docstring carries the reason the fixture exists (RLS answers correctly on its own, so the belt is unobservable while the braces are on) and, as loudly, what it is NOT for: it is not a faster `org_session`, and a test that merely wants rows in the database should still seed with `admin_engine` and drive the application through `org_session` or the route. The original text follows. **A shared fixture, but only after R3.** A `bypassrls_session` fixture would make the A shape one line instead of ten and would remove the reason people reach for `org_session`. Deliberately *not* built here: introducing it before the 219 renames means two conventions in the tree at once, and the file that adopts neither is the one that stops asserting. | Medium |
| **R6** | **LANDED `0e66b91`.** `_window_start`'s docstring now says the watermark is deliberately global, why (one Keycloak realm serves the whole deployment, so the stream is not a tenant's data and a per-org cursor would be a cursor over a stream no org owns), and — the half this row implies but does not state — why the obvious fix is a REGRESSION: per-org watermarks advance at different rates and an org with no forwardable events never advances at all, so the oldest cursor drags `dateFrom` back over swept ground, or, seeded from now to avoid that, loses exactly the late-in-the-day events the one-day lookback exists to catch. It points at §2.2 row 6 here. The original text follows. **Document `_window_start`'s fleet-wide read** (`keycloak_events/sweep.py:84`). One sentence in the docstring saying the watermark is deliberately global, so the next reader does not "fix" it into a per-org cursor and reintroduce the day-boundary loss the current shape avoids. | Trivial |
| **R7** | **LANDED `e25bbe7`.** `tests/test_route_session_discipline.py` AST-scans the request layer — everything under `app/api/`, plus every module file constructing an `APIRouter` — and fails on any `AsyncSession(...)` built outside `org_session`/`portal_session` that is not in `RAW_SESSIONS_BY_DESIGN` with a written reason. The allowlist is exact in both directions, so a reason left behind by deleted code fails as loudly as a new violation. Four entries: `api/deps.py::auth_context` and `api/v1/ws.py::ui_websocket` (admin engine, the pre-tenant org-slug lookup, correct and permanent) and the two error-monitor reads. A second test pins that only those two touch `app_engine` without a tenant, so a third of that shape is a finding rather than a style question. Verified by planting a violation in `api/v1/jobs.py` and watching both tests go red. The original text follows. **Consider a structural test that no route opens a bare `AsyncSession` on `app_engine`.** `tests/test_portal_api.py` already does the equivalent for portal routes (no portal route may use `org_session`). The inverse — every route must use `org_session`, `portal_session` or a named exception — would have made F1 visible as a structural violation the day it shipped, rather than requiring somebody to notice a `USING (true)` three migrations later. | Small |

## 5. What changed in this pass

| File | Change |
|---|---|
| `backend/tests/test_security_retention.py` | `+test_retention_does_not_delete_a_neighbour_orgs_events` (F2). Proven to fail (`assert 10 == 4`) without the predicate. |
| `backend/tests/test_remote_recording.py` | `+test_retention_does_not_expire_a_neighbour_orgs_recording` (F3). Proven to fail (`assert 2 == 1`) without the predicate. |

### And what changed in the follow-up pass, `e25bbe7` (2026-08-22)

| File | Change |
|---|---|
| `backend/migrations/versions/0068_platform_errors_operator_only.py` | F1's fix: `platform.errors.*` revoked from every role, granted to `super-admin`, and `set_role_permissions` replaced so the codes cannot be granted back from an org-scoped surface. |
| `backend/app/core/rbac.py` | `PLATFORM_OPERATOR_CODES`, with why `platform.status.read` is not in it. |
| `backend/app/core/rbac_admin.py` | The operator codes leave the permission matrix and are refused on write; the docstring paragraph that justified the self-grant path is corrected, since `0068` is what falsified its premise. |
| `backend/app/api/v1/errors.py` | The module docstring now says who holds the codes and why that sentence is the whole tenant boundary for these routes. |
| `backend/tests/test_rbac.py` | `test_org_admin_has_all_permissions` becomes `..._except_the_platform_operator_codes`; `+` the super-admin-only grant asserted from both sides, `+` no global role but `super-admin` carries either code, `+` migration and application name the same two codes. |
| `backend/tests/test_error_api.py` | The seed test moves to `super-admin`; `+` the both-sides 403/200 regression on the GET routes and on both writes. |
| `backend/tests/test_rbac_admin_api.py`, `test_rbac_matrix_function.py` | The self-grant path is refused at the route and again in the database; `platform.status.read` is asserted still grantable, so the rail is two codes rather than a prefix. |
| `backend/tests/test_rls.py`, `test_rls_invariant_sql.py`, `deploy/postgres/assert-rls.sql` | R2, and its four falsifiers. |
| `backend/tests/test_route_session_discipline.py` | R7. |
| `backend/tests/test_tenant_column_sweep.py` | The exemption's reason said "the platform-wide `platform.errors.read` code" — the sentence F1 turned on. Rewritten to say what is true and what now makes it true. |
| `docs/superpowers/specs/2026-08-15-error-monitoring-design.md` | §4.1 amended with the decision, the two rejected alternatives, and what a running deployment loses; §5 names `super-admin`. |

```
$ .venv/bin/ruff check app tests
All checks passed!
$ .venv/bin/mypy app
Success: no issues found in 374 source files
$ .venv/bin/alembic upgrade head && .venv/bin/alembic downgrade base \
    && .venv/bin/alembic upgrade head
UPGRADE OK / DOWNGRADE OK / RE-UPGRADE OK
```

Four shards again, 6,968 tests across 379 files — `787461f`'s 6,945 plus the
two this audit added plus twenty-one here:

```
shard 1 (95 files):  1838 passed, 4 warnings in 424.51s
shard 2 (95 files):  1923 passed, 2 warnings in 422.60s
shard 3 (95 files):  1661 passed, 3 warnings in 307.03s
shard 4 (94 files):  1546 passed, 2 warnings in 325.95s
```

**No application code was changed.** Every predicate deleted during
falsification was restored from a copy taken before the edit and verified with
`git diff --stat` against each file; `git status` shows exactly two modified
paths, both tests.

```
$ .venv/bin/ruff check app tests
All checks passed!
$ .venv/bin/mypy app
Success: no issues found in 374 source files
$ .venv/bin/pytest tests/test_remote_recording.py tests/test_remote_retention_worker.py \
    tests/test_security_retention.py -q
9 passed in 5.39s
$ .venv/bin/pytest tests/test_health_profiles.py tests/test_subscriptions_service.py \
    tests/test_device_groups_api.py tests/test_rls.py tests/test_tenant_column_sweep.py \
    tests/test_notification_tenancy.py tests/test_error_api.py -q
150 passed in 22.94s
```

And the whole backend suite, in four shards because a single process is killed by
this sandbox partway through — 6,947 tests across 378 files, which is `787461f`'s
6,945 plus the two added here:

```
shard 1 (98 files):  1669 passed, 3 warnings in 250.68s
shard 2 (94 files):  2197 passed, 6 warnings in 255.03s
shard 3 (94 files):  1248 passed, 1 warning  in 159.36s
shard 4 (92 files):  1833 passed            in 287.73s
```

Run whole rather than targeted deliberately: both new tests seed a neighbour org
that outlives them in the session-scoped database (`tests/conftest.py`'s
`pg_cluster` is one throwaway Postgres for the entire run), so an unfiltered
count anywhere else in the suite would have been the way to find out.

### And what changed in the third pass, R3–R6 (2026-08-22)

| Commit | Change |
|---|---|
| `031235e` | R3. Forty-six renames across 43 files, no assertion touched, plus five docstrings corrected where the docstring made the same overclaim the name did. Forty-five come from the mechanism-claiming sweep below; the forty-sixth is `test_domain_policy.py::test_the_preview_is_scoped_to_the_callers_tenant`, renamed alongside the A test that took over its claim (the commit message says forty-five, counting only the sweep). |
| `0e66b91` | R6. `keycloak_events/sweep.py::_window_start` — the fleet-wide watermark, why it is right, and why the per-org "fix" is a regression. |
| `e78f730` | R5 (`bypassrls_session` in `tests/conftest.py`) and R4 (fifteen falsified A tests). |

**Every falsification restored.** Twenty-six predicate deletions across
`licenses/service.py`, `bcp/service.py`, `identity/policy.py`,
`ai_admin/api.py`, `provisioning/repository.py`, `change_mgmt/service.py`,
`change_mgmt/links.py`, `deploy/scripts.py`, `backup/repository.py`,
`subscriptions/service.py`, `patch/approve.py`, `notification/channels.py`,
`ipam/subnets.py` and `budget/budgets.py`, each restored from a copy taken
before the edit and verified with `git diff --stat` per file.
`git diff -- backend/app backend/migrations` is empty across all three commits.

**The D tests stayed green throughout, which is the finding restated as
evidence.** Deleting `License.org_id == org_id` left
`test_another_orgs_license_is_hidden_by_rls` — the file's own tenancy test,
driving exactly that function — passing, and only the new A test went red. The
same held in every one of the fifteen files.

**One thing the audit did not predict, found while falsifying.**
`backup/repository.py::open_repo_key` with its predicate deleted fails on
`cryptography.exceptions.InvalidTag` rather than on a wrong row: the vault binds
`org_id` into the AEAD's associated data, so the sealed key does not open for the
wrong tenant even when the query hands the row over. A genuine second rail — and
the reason the new test asserts `NoResultFound` specifically rather than the bare
`Exception` its neighbour uses. A test that accepted any exception would score
the vault's refusal as the query's, leaving the predicate untested while looking
covered.

```
$ .venv/bin/ruff check app tests
All checks passed!                                    (exit 0)
$ .venv/bin/mypy app
Success: no issues found in 377 source files          (exit 0)
```

Four shards over `ls tests/test_*.py`, 385 files — 7,060 before this pass plus
the fifteen R4 adds, and the renames keep the count identical by construction:

```
shard 1 (99 files): 1685 passed, 3 warnings in 355.69s   EXIT=0
shard 2 (96 files): 2236 passed, 6 warnings in 351.76s   EXIT=0
shard 3 (96 files): 1299 passed, 1 warning  in 220.31s   EXIT=0
shard 4 (94 files): 1855 passed            in 405.20s    EXIT=0
                    ----
                    7075 = 7060 + 15
```

## 6. Deploy

**Nothing in the 2026-08-21 pass reaches the deployment.** Two test files were added to;
no application code, no migration, no new `.env` key, no new dependency. All
four are no-ops for this change and are listed so a missing step does not read
as a forgotten one. No `docker compose build`, no `up -d`, no `alembic upgrade`
is required.

**F1 is the exception and it does not ship from here.** When R1 is decided and
implemented it will touch `app/api/v1/errors.py` in the API process, and that
change will carry its own full deploy sequence.

**And it did.** `e25bbe7` ships migration `0068`, so the follow-up pass DOES
reach the deployment: it needs a `docker compose build` of the API, worker and
gateway images (the RBAC constant and the matrix rail are in code all three
load), an `alembic upgrade head`, and a restart. No new `.env` key, no new
dependency, no frontend rebuild required for the nav entry to disappear —
it is driven by `/me`. The full sequence is in that change's hand-off report.
