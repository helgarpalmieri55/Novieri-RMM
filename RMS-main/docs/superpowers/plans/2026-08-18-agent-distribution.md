# Agent Distribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** An operator can download the agent for any OS/arch from the console and get a one-line install command whose enrollment token never touches an artefact or argv.

**Architecture:** A global (non-tenant) `agent_releases` table; artefacts in object storage under a platform-owned prefix, served only by presigned time-bounded GETs; a console page under Platform. No push install — the control plane never connects to an endpoint, so invariant 2 is untouched.

**Tech Stack:** FastAPI, SQLAlchemy 2.x typed, Alembic, Pydantic v2, React + TanStack Query, existing `ObjectStore` presign machinery.

**Spec:** `docs/modules/agent-distribution/spec.md`

## Global Constraints

- **Invariant 2 is unchanged.** Nothing in this module may open a connection to an endpoint. No SSH, WinRM, SMB, or socket client anywhere in the new code.
- **Artefacts are token-free and byte-identical for every tenant.** `agent/packaging/README.md` forbids a token in a distributable artefact.
- **The token never reaches argv.** Generated commands pass it by pipe or stdin redirect. The shipped install scripts already refuse `--token`.
- **`agent_releases` has no `org_id`.** RLS therefore cannot be the guard: every route needs an explicit `require_permission`, and a missing one is a security defect, not a style nit.
- **`sha256` is computed server-side** from received bytes. A client-supplied digest is never stored.
- Enrollment tokens minted here default to `max_uses: 1` and an expiry in hours.
- New permission codes: `agent.release.read`, `agent.release.manage`. Both MUST be added to `app/core/rbac.py::PERMISSIONS` **and** seeded to `org-admin`, or `tests/test_rbac.py::test_org_admin_has_all_permissions` goes red.
- Migrations are forward-only. Head is `0058`; these are `0059` (table) and `0060` (permissions), split the way `0056`/`0057` were so RBAC can be reverted without touching schema.
- Type hints mandatory; mypy strict must pass. Frontend has no `any`.
- Windows and macOS artefacts carry a visible "never executed on this platform" marker, per `agent/packaging/README.md` §"What is untested".

---

### Task 1: The `agent_releases` table

**Files:**
- Create: `backend/migrations/versions/0059_agent_releases.py`
- Modify: `backend/app/models/core.py` (append `AgentRelease`)
- Test: `backend/tests/test_agent_release_model.py`

**Interfaces:**
- Produces: `AgentRelease` ORM class with fields `id, version, os, arch, storage_ref, size_bytes, sha256, signed, notes, published_at, published_by_user_id`
- Consumes: nothing from earlier tasks

- [x] **Step 1: Write the failing test**

```python
"""`agent_releases` is deliberately GLOBAL, and that has consequences.

Every other table in this schema carries `org_id` and is guarded by RLS. This
one does not, because the artefact bytes are identical for every tenant and a
per-org copy would multiply storage for no isolation benefit. The isolation
that matters -- a tenant cannot PUBLISH a release -- is an RBAC property.

So this test pins the absence of org_id as DELIBERATE, and the routes' own
tests pin the permission checks that replace RLS. If someone later adds
`org_id` without removing the RBAC checks, that is fine; if someone removes a
permission check because "RLS covers it", this table is why it does not.
"""

from sqlalchemy import inspect

from app.models.core import AgentRelease


def test_agent_releases_has_no_org_id() -> None:
    columns = {c.name for c in inspect(AgentRelease).columns}
    assert "org_id" not in columns, (
        "agent_releases is global on purpose; see this module's spec §4.2. "
        "If it gains org_id, the RBAC checks on its routes must still stay."
    )


def test_the_columns_the_console_and_the_install_command_need() -> None:
    columns = {c.name for c in inspect(AgentRelease).columns}
    assert {
        "id", "version", "os", "arch", "storage_ref",
        "size_bytes", "sha256", "signed", "published_at",
    } <= columns


def test_one_row_per_version_os_arch() -> None:
    # Two artefacts for the same (version, os, arch) is ambiguous: the install
    # command would have to pick one, and there is no basis to pick.
    constraints = {c.name for c in inspect(AgentRelease).local_table.constraints if c.name}
    assert any("version" in name for name in constraints), (
        f"expected a uniqueness constraint over (version, os, arch); found {constraints}"
    )
```

- [x] **Step 2: Run it to see it fail**

Run: `cd backend && .venv/bin/pytest tests/test_agent_release_model.py -q`
Expected: FAIL — `ImportError: cannot import name 'AgentRelease'`

- [x] **Step 3: Add the model**

Append to `backend/app/models/core.py`, following the file's existing style (`uuid7_pk()`, `Mapped[...]`, explicit `String(n)`):

```python
class AgentRelease(Base):
    """One downloadable agent artefact.

    NO `org_id`, deliberately: the bytes are identical for every tenant, so a
    per-org copy multiplies storage for no isolation. What must be per-tenant is
    the RIGHT TO PUBLISH, which is RBAC, not a row filter. Because RLS therefore
    guards nothing here, every route over this table needs an explicit
    permission check -- see this module's spec §4.2.
    """

    __tablename__ = "agent_releases"
    __table_args__ = (
        UniqueConstraint("version", "os", "arch", name="uq_agent_releases_version_os_arch"),
    )

    id: Mapped[UUID] = uuid7_pk()
    version: Mapped[str] = mapped_column(String(64))
    os: Mapped[str] = mapped_column(String(16))    # linux | windows | darwin
    arch: Mapped[str] = mapped_column(String(16))  # amd64 | arm64
    storage_ref: Mapped[str] = mapped_column(String(512))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    # Hex digest computed by the SERVER over the bytes it received. A digest
    # supplied by the uploader verifies nothing -- same party, same bytes.
    sha256: Mapped[str] = mapped_column(String(64), default="")
    signed: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str] = mapped_column(String(500), default="")
    published_at: Mapped[datetime | None] = mapped_column(default=None)
    published_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"), default=None
    )
```

- [x] **Step 4: Write migration 0059**

`backend/migrations/versions/0059_agent_releases.py`, `revision = "0059"`, `down_revision = "0058"`. Create the table with the same columns and the unique constraint. `downgrade()` drops the table. Docstring states why there is no `org_id` and no RLS policy — every other table's migration adds one, so its absence must be explained or it reads as an oversight.

- [x] **Step 5: Run the model tests and the migration round trip**

Run: `cd backend && .venv/bin/pytest tests/test_agent_release_model.py -q`
Expected: 3 passed

Run: `cd backend && .venv/bin/alembic upgrade head && .venv/bin/alembic downgrade 0058 && .venv/bin/alembic upgrade head`
Expected: exit 0 each time. This is what CI's `migrations` job does; a `downgrade` that leaves the table behind fails the next `upgrade`.

- [x] **Step 6: Commit**

```bash
git add backend/app/models/core.py backend/migrations/versions/0059_agent_releases.py backend/tests/test_agent_release_model.py
git commit -m "agent-distribution: the agent_releases table"
```

---

### Task 2: The two permission codes

**Files:**
- Modify: `backend/app/core/rbac.py`
- Create: `backend/migrations/versions/0060_agent_release_permissions.py`
- Test: `backend/tests/test_rbac.py` (existing — it will fail until seeded)

**Interfaces:**
- Produces: `agent.release.read`, `agent.release.manage` in `PERMISSIONS`
- Consumes: Task 1's migration as `down_revision`

- [x] **Step 1: Add the codes to `PERMISSIONS`**

In `backend/app/core/rbac.py`, in the existing dict, with descriptions in the file's established voice:

```python
    "agent.release.read": (
        "List agent releases and generate an install command (which mints a "
        "single-use enrollment token)"
    ),
    "agent.release.manage": (
        "Publish, replace and unpublish agent artefacts -- the bytes every "
        "endpoint in the fleet will execute"
    ),
```

Read/write split, per this project's convention (`patch.read`/`patch.apply`, `health.read`/`health.alert.manage`).

- [x] **Step 2: Run the RBAC test to see it fail**

Run: `cd backend && .venv/bin/pytest tests/test_rbac.py -q`
Expected: FAIL on `test_org_admin_has_all_permissions` — the codes are in the dict and not seeded.

- [x] **Step 3: Write migration 0060**

`revision = "0060"`, `down_revision = "0059"`. Copy the shape of `0057_platform_errors_permission.py` exactly: insert both codes with `module='agent'`, then seed both to `org-admin` only. `downgrade()` deletes the `role_permissions` rows then the `permissions` rows, in that order.

Docstring must state who else gets them and why not: `agent.release.manage` publishes bytes the whole fleet will execute, so it is not a technician tier; an org that wants that grants it deliberately.

- [x] **Step 4: Run the tests**

Run: `cd backend && .venv/bin/pytest tests/test_rbac.py -q`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add backend/app/core/rbac.py backend/migrations/versions/0060_agent_release_permissions.py
git commit -m "agent-distribution: agent.release.read and agent.release.manage"
```

---

### Task 3: Presign an agent artefact

**Files:**
- Modify: `backend/app/core/objectstore.py` (add `presign_agent_release_get` and `presign_agent_release_put`)
- Test: `backend/tests/test_objectstore_agent_release.py`

**Interfaces:**
- Consumes: nothing from Tasks 1–2
- Produces: `presign_agent_release_get(storage_ref: str, expires_in: int) -> str`, `presign_agent_release_put(storage_ref: str) -> dict[str, Any]`

**Before writing:** read `presign_package_artifact` in `app/modules/deploy/api.py` and its `ObjectStore` counterpart. The prefix-scoping, expiry handling and size cap are patterns to copy, not re-derive. The backup module's `presign_backup_read` is the other reference.

- [x] **Step 1: Write the failing tests**

```python
def test_the_key_is_confined_to_the_agent_release_prefix() -> None:
    # A storage_ref is read from the database, and a database value is not a
    # trusted path. `../` in a ref must not be able to presign somebody's
    # backup chunk or a deploy artefact.
    store = ObjectStore(...)
    with pytest.raises(ValueError):
        store.presign_agent_release_get("agent-releases/../backups/org/x", 300)


def test_the_grant_is_time_bounded() -> None:
    url = store.presign_agent_release_get("agent-releases/abc123/its-agent-linux-amd64", 300)
    assert "X-Amz-Expires=300" in url


def test_an_unbounded_expiry_is_refused() -> None:
    with pytest.raises(ValueError):
        store.presign_agent_release_get("agent-releases/abc/x", 0)
```

- [x] **Step 2: Run to see them fail**

Run: `cd backend && .venv/bin/pytest tests/test_objectstore_agent_release.py -q`
Expected: FAIL — attribute does not exist

- [x] **Step 3: Implement both methods**

Refuse any ref that does not start with `agent-releases/`, and refuse any ref containing `..` after normalisation. Refuse `expires_in <= 0` and cap it (15 minutes is the value the backup read path uses; match it rather than inventing a second number).

- [x] **Step 4: Run the tests**

Expected: 3 passed

- [x] **Step 5: Commit**

```bash
git add backend/app/core/objectstore.py backend/tests/test_objectstore_agent_release.py
git commit -m "agent-distribution: prefix-scoped presign for agent artefacts"
```

---

### Task 4: The API routes

**Files:**
- Create: `backend/app/modules/agent_distribution/__init__.py`, `api.py`, `service.py`, `schemas.py`
- Modify: `backend/app/main.py` (include the router)
- Test: `backend/tests/test_agent_release_api.py`

**Interfaces:**
- Consumes: `AgentRelease` (Task 1), the two permission codes (Task 2), `presign_agent_release_*` (Task 3)
- Produces: the five routes in the spec's §4.3 table

- [x] **Step 1: Write the failing tests — start with the security ones**

```python
@pytest.mark.parametrize("method,path", [
    ("GET",  "/api/v1/agent/releases"),
    ("POST", "/api/v1/agent/releases"),
    ("DELETE", "/api/v1/agent/releases/{id}"),
])
def test_every_route_refuses_an_unauthenticated_caller(method, path) -> None:
    # agent_releases has no org_id, so RLS guards nothing here. These checks
    # are the ONLY thing standing between a reader and the bytes the fleet
    # executes -- which is why they are the first tests written.
    assert client.request(method, path).status_code == 401


def test_publishing_requires_manage_not_read() -> None:
    assert as_user_with({"agent.release.read"}).post("/api/v1/agent/releases", json=...).status_code == 403


def test_the_server_computes_sha256_and_ignores_a_supplied_one() -> None:
    # Plant a wrong digest. The stored value must be the digest of the bytes.
    resp = upload(release_id, body=b"hello", claimed_sha256="0" * 64)
    assert row.sha256 == hashlib.sha256(b"hello").hexdigest()


def test_an_install_command_mints_a_single_use_token() -> None:
    body = generate_install_command(release_id)
    assert token_row.max_uses == 1
    assert token_row.expires_at is not None


def test_the_install_command_never_puts_the_token_in_argv() -> None:
    # The shipped install scripts refuse --token for this reason; a generated
    # command that reintroduces it defeats them.
    command = generate_install_command(release_id)["command"]
    token = generate_install_command(release_id)["token"]
    assert f"--token {token}" not in command
    assert f"--token={token}" not in command
    assert "install -m600 /dev/stdin" in command or "| " in command
```

- [x] **Step 2: Run to see them fail**

Expected: all fail — module does not exist

- [x] **Step 3: Implement**

`service.py` holds the logic (business logic lives in services, not route handlers). Routes are thin. Every route declares `Depends(require_permission("agent.release.read"))` or `...manage`. The install-command route requires **both** `agent.release.read` and the existing enrollment-token create permission — it mints a credential.

Audit events per spec §5: `agent.release_published`, `agent.release_unpublished`, `agent.install_command_generated` (naming the token id).

- [x] **Step 4: Run the tests**

Expected: all pass

- [x] **Step 5: mypy and ruff**

Run: `cd backend && .venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: clean

- [x] **Step 6: Commit**

```bash
git add backend/app/modules/agent_distribution backend/app/main.py backend/tests/test_agent_release_api.py
git commit -m "agent-distribution: release routes, server-computed digest, install command"
```

---

### Task 5: An invariant-2 guard over the new module

**Files:**
- Test: `backend/tests/test_agent_distribution_opens_no_connection.py`

**Interfaces:** consumes the module from Task 4.

This is crude and worth having anyway: the module's whole justification is that it does not connect to endpoints, and the cheapest way that regresses is somebody adding "just a quick reachability check".

- [x] **Step 1: Write the test**

```python
FORBIDDEN = ("paramiko", "pywinrm", "winrm", "smbprotocol", "socket.create_connection",
             "asyncssh", "fabric")

def test_the_module_imports_nothing_that_could_reach_an_endpoint() -> None:
    source = "\n".join(p.read_text() for p in MODULE.rglob("*.py"))
    for name in FORBIDDEN:
        assert name not in source, (
            f"{name} in agent_distribution would mean the control plane can "
            f"connect to an endpoint, which invariant 2 forbids and this "
            f"module exists to avoid -- see its spec §1"
        )
```

- [x] **Step 2: Run it**

Expected: PASS (non-vacuously — assert the module has files first, or a rename makes this pass over an empty string)

- [x] **Step 3: Commit**

```bash
git add backend/tests/test_agent_distribution_opens_no_connection.py
git commit -m "agent-distribution: guard that nothing here dials an endpoint"
```

---

### Task 6: The console page

**Files:**
- Create: `frontend/src/modules/agent-releases/AgentReleasesPage.tsx`, `api.ts`, `types.ts`, `InstallCommandDialog.tsx`
- Modify: `frontend/src/lib/api.ts`, `frontend/src/App.tsx`, `frontend/src/shell/nav.ts`
- Test: `frontend/src/modules/agent-releases/AgentReleasesPage.test.tsx`

**Interfaces:** consumes the routes from Task 4.

- [x] **Step 1: Write the failing tests**

Mirror `modules/devices/DevicesPage.test.tsx`: the three states (loading, error, genuinely empty) must be distinguishable, plus:

```tsx
it("marks Windows and macOS artefacts as never executed", async () => {
  // agent/packaging/README.md: the Windows service has never started and the
  // macOS daemon has never started. A row that looks like the Linux one
  // implies a parity that does not exist, and implies it most convincingly to
  // whoever is deciding whether to trust it.
  renderPage(() => Promise.resolve({ items: [release({ os: "windows" })] }));
  expect(await screen.findByText(/never executed/i)).toBeInTheDocument();
});

it("shows the checksum, because it is how the operator verifies the download", async () => {
  ...
  expect(screen.getByText(/a1b2c3/)).toBeInTheDocument();
});

it("says the token is single-use in the install dialog", async () => { ... });
```

- [x] **Step 2: Run to see them fail**

Run: `cd frontend && npx vitest run src/modules/agent-releases`
Expected: FAIL

- [x] **Step 3: Implement using the shell primitives**

`PageHead`, `StatsGrid`/`StatCard`, `Panel`, `TableWrap`, `QueryState` from `shell/components` — the same set every migrated page uses. Form fields use `inputClass` (now `input-field`, not the search class).

Nav: add to the **Platform** group in `shell/nav.ts` with `permission: "agent.release.read"`.

- [x] **Step 4: Run the tests, tsc, and the whole suite**

Run: `cd frontend && npx vitest run && npx tsc --noEmit`
Expected: all pass, tsc clean

- [x] **Step 5: Commit**

```bash
git add frontend/src/modules/agent-releases frontend/src/lib/api.ts frontend/src/App.tsx frontend/src/shell/nav.ts
git commit -m "agent-distribution: Platform -> Agent packages"
```

---

### Task 7: Runbook and docs

**Files:**
- Modify: `deploy/README.md` (publishing a release, during or after first boot)
- Modify: `agent/packaging/README.md` (point at the console as the distribution path)
- Modify: `frontend/src/modules/enrollment/EnrollmentPage.tsx` (its header documents this gap as unfilled — update it)
- Modify: `docs/modules/agent-distribution/spec.md` (status → built, with what remains unverified)

- [x] **Step 1: Update `EnrollmentPage.tsx`'s header table**

Its line 17 currently reads that no release or artifact endpoint exists. That becomes a link to the new page. **Leaving a stale "this does not exist" comment is worse than no comment** — the next reader trusts it.

- [x] **Step 2: Add a `deploy/README.md` section**

How to publish the six artefacts after `./packaging/build.sh`, and the `sha256sum` the console will display, so an operator can compare what they built against what the console says it received.

- [x] **Step 3: Mark what is still unverified**

The spec's status line becomes "built"; §3's table keeps its Windows/macOS "never executed" rows, because publishing an artefact does not run it.

- [x] **Step 4: Commit**

```bash
git add deploy/README.md agent/packaging/README.md frontend/src/modules/enrollment/EnrollmentPage.tsx docs/modules/agent-distribution/spec.md
git commit -m "agent-distribution: runbook, and correct the comment that said this was missing"
```

---

## Verification before claiming completion

- `cd backend && .venv/bin/pytest -q` — green (note: `test_worker_sweep_containment.py` errors in a container without postgres; that is pre-existing)
- `cd backend && .venv/bin/mypy app` — clean
- `cd backend && .venv/bin/alembic upgrade head && .venv/bin/alembic downgrade 0058 && .venv/bin/alembic upgrade head` — exit 0
- `cd frontend && npx vitest run && npx tsc --noEmit` — green
- The falsifiers in the spec's §6 each map to a test above; item 3 (byte-identical artefacts) and item 7 (no outbound connection) are Tasks 4 and 5 respectively
