"""Spec §12.1's "no kernel change", ASSERTED rather than reviewed for.

**THE SPEC'S OWN SENTENCE IS FALSE ON DAY ONE, AND THIS FILE SAYS SO.** §12.1
ends: *"If a builder finds themselves editing any file in `app/jobs/`,
`app/gateway/`, or `app/core/` to make this module work, the design has
drifted."* Taken literally that is contradicted two sections earlier by the same
document: §5.1 REQUIRES editing `app/gateway/main.py::register_module_job_types`,
and the repo's shipped permission pattern requires adding three codes to
`app/core/rbac.py::PERMISSIONS`. Both are under the named packages. A guard
built on the literal sentence would be permanently red, and a permanently red
guard gets deleted or `xfail`ed, which is worse than no guard because it reads
as protection.

So the claim is restated precisely, and THIS is what is asserted:

> **v1 appends to SEVEN registration points and changes no kernel logic.** Every
> one is an existing extension point that every module in this repo appends to.
> **No kernel file gains a BRANCH** — no `site_server` case in dispatch, no
> second identity resolution, no second job-loading path, no widened column.

**THE PLAN SAID FOUR AND IT WAS WRONG; THE FULL SUITE FOUND THE OTHER THREE.**
`plan.md` Task 5 named `app/core/rbac.py::PERMISSIONS`,
`app/models/__init__.py`, `app/modules/__init__.py::register_all` and
`app/gateway/main.py::register_module_job_types`. It did not know about the
three **deny-by-default partitions over the live job registry** — `direct_
dispatch.py`, `dispatch_permissions.py` and `approved_actions.py` — each of
which asserts that EVERY registered job type has been classified, precisely so
that a module registering a new one breaks a test instead of picking a side by
accident. `site.wake` had to be classified in all three, and `app/worker.py`
had to register the job type as well, because those three tests use
`register_worker_job_types()` as their definition of "the live registry".

That is a good outcome twice over: the kernel's own guard caught the omission,
and THIS file's guard then caught the fix — it went red naming the three new
files, and `test_no_kernel_file_gained_a_branch_on_this_module` stayed GREEN,
which is exactly the distinction between "appended to a registration list" and
"grew a special case" that the two assertions exist to separate.

**EVERY MAP BELOW IS COMPARED WITH `==`, NEVER `>=` AND NEVER A SUBSET.** A
subset assertion passes against the exact change it exists to forbid.

**DOCSTRINGS AND COMMENTS ARE STRIPPED BEFORE MATCHING** (`_code_of`, lifted
from `tests/test_ipam_sweep_dispatch.py` rather than re-derived). Written as a
raw-text `grep` instead, this test would go red on the very paragraph in
`register_module_job_types` that explains why it is green — a guard must match
DEFINITIONS, not mentions.

**WHAT THIS FILE CANNOT CHECK, SAID PLAINLY SO NOBODY THINKS IT DOES:** a test
can see REFERENCES, not AUTHORSHIP. §12.1's "the module adds no file to
`app/jobs/`, `app/gateway/`, `app/core/`" is a review obligation —
`git diff --stat` for new paths under those three trees — and
`test_the_module_added_no_file_to_the_kernel_trees` below narrows it to "the set
of files under those trees is what it was", which catches an ADDITION but cannot
attribute one.
"""

import ast
import re
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.signing import LocalEd25519Signer
from app.core.tenancy import org_session
from app.jobs.engine import JobEngine
from app.modules.site_server.dispatch import wake_via_site_server
from app.modules.site_server.handler import register_job_types
from tests.ipam_fixtures import seed_agent, seed_device, seed_org
from tests.site_server_fixtures import seed_site_server

register_job_types()

BACKEND = Path(__file__).resolve().parents[1]
KERNEL_TREES = ("app/jobs", "app/gateway", "app/core")
MODULE_DIR = BACKEND / "app" / "modules" / "site_server"


def _code_of(path: Path) -> str:
    """`path`'s source with every docstring blanked, round-tripped through the
    AST — so comments vanish too, since `ast.unparse` never emits them.

    Copied verbatim from `tests/test_ipam_sweep_dispatch.py::_code_of`. One
    implementation, two guards; two copies is how one of them ends up matching
    prose.
    """
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(
            node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        ):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body[0].value.value = ""
    return ast.unparse(tree)


def _kernel_files() -> list[Path]:
    found: list[Path] = []
    for tree in KERNEL_TREES:
        # `app/core/i18n/` is the Spanish catalogue: it carries a translation of
        # each allowlisted message above, so it mirrors those mentions and is not
        # a place where the kernel gains knowledge of this module.
        found.extend(p for p in sorted((BACKEND / tree).rglob("*.py")) if "i18n" not in p.parts)
    return found


# --------------------------------------------------------------------------- #
# (a) The kernel names this module at exactly two allowlisted points
# --------------------------------------------------------------------------- #

#: The counts OBSERVED, pinned. Not the counts anybody guessed.
#:
#: **THE PLAN SAID FOUR REGISTRATION POINTS. THERE ARE SEVEN, AND THE FULL SUITE
#: IS WHAT FOUND THE OTHER THREE.** `docs/modules/site-server/plan.md` Task 5
#: restated spec §12.1 as "v1 appends to four registration points" —
#: `app/core/rbac.py::PERMISSIONS`, `app/models/__init__.py`,
#: `app/modules/__init__.py::register_all` and
#: `app/gateway/main.py::register_module_job_types`. That list is incomplete.
#: This repo also maintains **three deny-by-default partitions over the live job
#: registry**, each of which asserts that every registered job type has been
#: classified, so a module registering a new type breaks a test rather than
#: picking a side by accident:
#:
#:   * `app/jobs/direct_dispatch.py` — may a human dispatch it through the
#:     KERNEL route, bypassing the module's own record? (`site.wake`: no.)
#:   * `app/jobs/dispatch_permissions.py` — which permission does the module's
#:     OWN route require? (`site.wake`: `site.wake`.)
#:   * `app/core/approved_actions.py` — may a MACHINE dispatch it autonomously?
#:     (`site.wake`: no, and it is `_DESTRUCTIVE_JOB_TYPES` for the same reason
#:     `net.sweep` is — it emits packets at addresses that are not the target
#:     device, so its cost is not bounded by the machine the job is signed for.)
#:
#: These are appends to existing extension points, exactly like the permission
#: codes in `rbac.py` — which is why `test_no_kernel_file_gained_a_branch_on_
#: this_module` below still passes: a dict or frozenset entry is not a branch.
#: The distinction between "appends to a registration list" and "grows a special
#: case" is the whole reason that second assertion exists beside this one.
#:
#: Per-file, what is counted (docstrings and comments stripped first):
#:   * `app/gateway/main.py` — the import, which spells `site_server` in the
#:     module path AND again in the alias, plus the call.
#:   * `app/core/rbac.py` — the three permission code keys, plus TWO
#:     cross-references from other codes' descriptions. One is inside
#:     `site.manage`'s own text. The other (added 2026-08-26) is inside
#:     `agent.push.run`'s, which cites `provisioning`'s `wake_on_lan`
#:     additionally requiring `site.wake` as the precedent for a permission
#:     that is not sufficient on its own. That is prose in a description
#:     string, not a branch and not a registration: the kernel gained no
#:     knowledge of this module from it, which is the property this file
#:     exists to hold. A cross-reference EARNS its mention here -- deleting it
#:     to keep the number at four would make the permission harder to
#:     understand in exchange for nothing.
#:   * `app/jobs/direct_dispatch.py` — the map key plus the occurrences inside
#:     its refusal message, which names the route and permission on purpose.
#:   * `app/jobs/dispatch_permissions.py` — the key and its value.
#:   * `app/core/approved_actions.py` — the `_DESTRUCTIVE_JOB_TYPES` entry, the
#:     `NON_APPROVABLE_REASONS` key, and its reason text.
#:
#: `app/models/__init__.py`, `app/modules/__init__.py` and `app/worker.py` are
#: NOT under these three trees, so they do not appear here.
EXPECTED_KERNEL_MENTIONS: dict[str, int] = {
    "app/gateway/main.py": 3,
    "app/core/rbac.py": 5,
    "app/core/approved_actions.py": 4,
    "app/jobs/direct_dispatch.py": 8,
    "app/jobs/dispatch_permissions.py": 2,
}

_PATTERNS = ("site_server", "SiteWake", "site.read", "site.manage", "site.wake")
# A pattern preceded by a letter or digit is part of another word:
# `backup.offsite.manage` is the backup module's permission, not a mention.
_MENTION = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(p) for p in _PATTERNS) + ")")


def _mention_map() -> dict[str, int]:
    found: dict[str, int] = {}
    for path in _kernel_files():
        code = _code_of(path)
        count = len(_MENTION.findall(code))
        if count:
            found[str(path.relative_to(BACKEND))] = count
    return found


def test_the_kernel_names_this_module_only_at_the_allowlisted_points() -> None:
    """An EQUALITY over `{file: count}`, so an unexpected file is named and an
    expected one that grew is named too.

    *If a `target_type == "site_server"` branch were added to
    `_load_job_for_device`* the map gains `app/gateway/dispatch.py` with its
    count and the assertion prints that file by name. That mutation was run; see
    the commit message.
    """
    assert _mention_map() == EXPECTED_KERNEL_MENTIONS


def test_no_kernel_file_gained_a_branch_on_this_module() -> None:
    """The stronger half of (a): a MENTION at a registration point is fine, a
    CONDITIONAL on this module's name anywhere in the kernel is not.

    "Appends to a registration list" and "grows a special case" are different
    acts with different consequences, and the count map alone cannot tell them
    apart — `register_site_server_job_types()` and
    `if job.target_type == "site_server": ...` both read as one more mention.
    This walks every `If`, `IfExp`, `Match` and boolean operator in the three
    kernel trees and asserts none of them mentions this module at all.
    """
    offenders: list[str] = []
    for path in _kernel_files():
        tree = ast.parse(_code_of(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.If | ast.IfExp | ast.Match | ast.BoolOp):
                continue
            rendered = ast.unparse(node)
            if any(pattern in rendered for pattern in _PATTERNS):
                offenders.append(f"{path.relative_to(BACKEND)}:{node.lineno}")
    assert offenders == []


def test_the_module_added_no_file_to_the_kernel_trees() -> None:
    """No `site*.py` under `app/jobs/`, `app/gateway/` or `app/core/`.

    Narrower than §12.1's review obligation and honest about it: this catches a
    file this module might plausibly have added, and cannot attribute an
    arbitrary new kernel file to anybody. The full obligation is
    `git diff --stat` over those three trees, and it stays a human step.
    """
    added = [
        str(path.relative_to(BACKEND))
        for path in _kernel_files()
        if path.name.startswith("site")
    ]
    assert added == []


# --------------------------------------------------------------------------- #
# (b) `jobs.target_type` is still varchar(10), and "site_server" is 11
# --------------------------------------------------------------------------- #


async def test_jobs_target_type_is_still_varchar_ten(admin_engine: AsyncEngine) -> None:
    """THE PAIR IS THE POINT, and the failure message says why.

    The design §0.1 REJECTED — a first-class `site_server` job target — begins
    with a migration that widens a shipped column on the `jobs` table, because
    `"site_server"` does not fit in `varchar(10)`. A widened column is therefore
    the earliest observable sign that somebody has started building it, long
    before any dispatch code exists to notice.

    *Widened to `varchar(20)`* this reports `20` by name.
    """
    async with AsyncSession(admin_engine) as session:
        width = (
            await session.execute(
                text(
                    "SELECT character_maximum_length FROM information_schema.columns"
                    " WHERE table_name = 'jobs' AND column_name = 'target_type'"
                )
            )
        ).scalar_one()

    assert (width, len("site_server")) == (10, 11), (
        f"jobs.target_type is varchar({width}). It was varchar(10), and "
        '"site_server" is 11 characters: a widened column here is the first '
        "move of the rejected design in spec §0.1, in which a site server "
        "becomes a second job-target type with a second dispatch channel and a "
        "second identity resolution."
    )


# --------------------------------------------------------------------------- #
# (c) Every job this module creates is a device job naming the right device
# --------------------------------------------------------------------------- #


async def test_a_wake_job_is_a_device_job_naming_the_site_server(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Behavioural, on the admin engine, with TWO devices in the fixture.

    The second device — an ordinary endpoint with its own agent — is what makes
    `target_id` naming the site server an OBSERVATION rather than arithmetic on
    a one-element set. `target_type` is `"device"` because `JobEngine.create`
    hard-sets it and this module does not go near that; asserting it here is how
    "§0.1 held" becomes a fact about a row rather than a claim in a document.
    """
    org = await seed_org(admin_engine, "ssk-a")
    ordinary = await seed_device(admin_engine, org.org_id, hostname="ordinary")
    await seed_agent(admin_engine, org.org_id, ordinary)
    await seed_agent(admin_engine, org.org_id, org.device_id)
    site_server_id = await seed_site_server(
        admin_engine, org.org_id, device_id=org.device_id, segments=["10.20.7.0/24"]
    )

    async with org_session(org.org_id, engine=app_engine) as session:
        await wake_via_site_server(
            session,
            JobEngine(signer=LocalEd25519Signer(tmp_path / "job-signing.key")),
            org_id=org.org_id,
            site_server_id=site_server_id,
            target_mac="a4:bb:6d:11:22:33",
        )

    async with AsyncSession(admin_engine) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT type, target_type, target_id FROM jobs WHERE org_id = :org"
                ),
                {"org": org.org_id},
            )
        ).all()

    assert [(r.type, r.target_type, r.target_id) for r in rows] == [
        ("site.wake", "device", org.device_id)
    ]
    assert org.device_id != ordinary


# --------------------------------------------------------------------------- #
# (d) The module's own imports stay inside the boundary
# --------------------------------------------------------------------------- #

#: `handler.py` may reach the job REGISTRY (it registers a job type);
#: `dispatch.py` may reach the job ENGINE (it is the single job-creating path);
#: `api.py` may reach `app.gateway.dispatch` FOR `publish_job` AND NOTHING ELSE.
#: Nobody else may reach any of them, and NOBODY at all may reach
#: `app.core.signing` or `app.core.ca`.
#:
#: **THE PLAN GOT `api.py` WRONG AND THE CODE WINS.** `docs/modules/site-server/
#: plan.md` Task 5(d) pins the allowlist as `handler.py` and `dispatch.py` "and
#: nothing else", and offers `from app.gateway.dispatch import publish_job` added
#: to `api.py` as its example of a FORBIDDEN second egress path. That is
#: contradicted by the same plan's Task 6, which REQUIRES the wake route to
#: publish the envelope after the tenant transaction commits — and by the
#: shipped repo, where `backup`, `deploy`, `health`, `ipam` and `patch` api.py
#: all import exactly that symbol for exactly that reason. Publishing is not a
#: second egress path; it is the delivery half of the only one, and it must
#: happen in the route because only the route knows when the transaction
#: committed.
#:
#: So `api.py` is allowlisted, and NARROWED rather than merely permitted:
#: `test_the_api_imports_only_publish_job_from_the_gateway` pins the imported
#: NAME, so `from app.gateway.dispatch import publish_job, _load_job_for_device`
#: is still red.
#:
#: `app.core.rbac`, `app.core.audit` and `app.core.tenancy` are ordinary module
#: dependencies every module in this repo has, and are deliberately NOT in the
#: forbidden set: forbidding them would forbid auditing, which invariant #4
#: requires.
EXPECTED_MODULE_IMPORTS: dict[str, set[str]] = {
    "handler.py": {"app.jobs.registry"},
    "dispatch.py": {"app.jobs.engine"},
    "api.py": {"app.gateway.dispatch"},
}

_FORBIDDEN_PREFIXES = ("app.jobs", "app.gateway", "app.core.signing", "app.core.ca")


def _module_boundary_imports() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in sorted(MODULE_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text())
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        hits = {
            name
            for name in names
            if any(name == prefix or name.startswith(prefix + ".") for prefix in
                   _FORBIDDEN_PREFIXES)
        }
        if hits:
            found[path.name] = hits
    return found


def test_only_two_files_reach_the_kernels_job_machinery() -> None:
    """An EQUALITY over `{file: {modules}}`.

    *If `from app.gateway.dispatch import publish_job` were added to `api.py`* —
    the exact shape of a second egress path, and the one that would publish
    inside a tenant transaction — the map differs and the assertion names
    `api.py`. That mutation was run; see the commit message.
    """
    assert _module_boundary_imports() == EXPECTED_MODULE_IMPORTS


def test_nothing_here_imports_the_signer_or_the_ca() -> None:
    """A site server holds no signing key, signs nothing, and verifies nothing
    on another device's behalf (spec §9, invariant #1).

    Implied by the equality above and stated separately because it is the
    invariant a reader comes here to check: if any file in this package could
    reach `app.core.signing` or `app.core.ca`, a second signing path over a
    bespoke envelope is one import away — which is exactly the thing spec §8.2
    REMOVES from provisioning rather than adds here.
    """
    reachable = {
        name
        for names in _module_boundary_imports().values()
        for name in names
        if name.startswith(("app.core.signing", "app.core.ca"))
    }
    assert reachable == set()


def test_the_api_imports_only_publish_job_from_the_gateway() -> None:
    """`api.py`'s allowlisting is NARROWED to one symbol, not to a module.

    Allowlisting `app.gateway.dispatch` wholesale would silently permit
    `from app.gateway.dispatch import publish_job, _load_job_for_device` — a
    module reaching into the kernel's job-loading path, which is the one place
    "a device may complete only its own job" is enforced. Pinning the imported
    NAMES keeps the exception the size of the reason for it.
    """
    imported: dict[str, set[str]] = {}
    for path in sorted(MODULE_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "app.gateway"
            ):
                imported.setdefault(path.name, set()).update(
                    alias.name for alias in node.names
                )
    assert imported == {"api.py": {"publish_job"}}


def test_no_file_in_this_module_builds_a_job_or_sets_its_target_type() -> None:
    """§12.1's first row: `Job.target_type` stays `"device"`, set by the kernel.

    **THIS GUARD HAD TO BE NARROWED, AND THE REASON IS ITSELF WORTH RECORDING.**
    Written as "no `target_type=` keyword anywhere in this package", it went red
    on `audit_service.record(..., target_type="site_server", ...)` — which is a
    DIFFERENT COLUMN ON A DIFFERENT TABLE. `audit_events.target_type` is
    `varchar(50)` and says what KIND OF THING an audit row is about;
    `jobs.target_type` is `varchar(10)` and says what kind of thing a job is
    ADDRESSED to. Spec §10.3 requires the first to be `site_server` for
    `site.wake_dispatched`, and §12.1 requires the second never to be. A guard
    that conflated them would have forced the audit row to lie in order to stay
    green, which is exactly how a security test becomes a liability.

    So the property asserted is the one that is actually forbidden: this module
    never CONSTRUCTS a `Job`, and never assigns `.target_type` on anything. Job
    creation goes through `JobEngine.create`, which hard-sets `"device"` —
    pinned positively by `test_the_dispatch_call_names_target_device_id`.
    """
    offenders: list[str] = []
    for path in sorted(MODULE_DIR.rglob("*.py")):
        tree = ast.parse(_code_of(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Job"
            ):
                offenders.append(f"{path.name}:{node.lineno} constructs Job(...)")
            targets: list[Any] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign | ast.AugAssign):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "target_type":
                    offenders.append(f"{path.name}:{node.lineno} assigns .target_type")
    assert offenders == []


async def test_the_audit_target_type_and_the_job_target_type_are_different_columns(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The distinction the guard above had to learn, pinned as observed values.

    One wake produces an `audit_events` row whose `target_type` is
    `'site_server'` and a `jobs` row whose `target_type` is `'device'`. Both are
    correct, they are different columns on different tables, and asserting them
    together is what stops a future author "fixing" either to match the other.
    """
    org = await seed_org(admin_engine, "ssk-b")
    await seed_agent(admin_engine, org.org_id, org.device_id)
    site_server_id = await seed_site_server(
        admin_engine, org.org_id, device_id=org.device_id, segments=["10.20.7.0/24"]
    )

    async with org_session(org.org_id, engine=app_engine) as session:
        await wake_via_site_server(
            session,
            JobEngine(signer=LocalEd25519Signer(tmp_path / "job-signing.key")),
            org_id=org.org_id,
            site_server_id=site_server_id,
            target_mac="a4:bb:6d:11:22:33",
        )

    async with AsyncSession(admin_engine) as session:
        job_target_type = (
            await session.execute(
                text("SELECT target_type FROM jobs WHERE org_id = :org"),
                {"org": org.org_id},
            )
        ).scalar_one()
        audit_target_type = (
            await session.execute(
                text(
                    "SELECT target_type FROM audit_events WHERE org_id = :org"
                    "   AND action = 'site.wake_dispatched'"
                ),
                {"org": org.org_id},
            )
        ).scalar_one()

    assert (job_target_type, audit_target_type) == ("device", "site_server")


def test_the_dispatch_call_names_target_device_id() -> None:
    """The positive half of the assertion above.

    "Nobody sets `target_type`" is also true of a module that creates no jobs at
    all, so it is satisfied by a dispatcher that never ran. This pins that
    `dispatch.py` DOES call `job_engine.create` and passes `target_device_id` —
    the kernel's own parameter, through the kernel's own extension point.
    """
    tree = ast.parse(_code_of(MODULE_DIR / "dispatch.py"))
    create_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create"
    ]
    assert len(create_calls) == 1
    keywords = {kw.arg for kw in create_calls[0].keywords}
    assert "target_device_id" in keywords
    assert "target_type" not in keywords


def test_a_wake_job_completes_only_from_its_own_device(
    admin_engine: AsyncEngine,
) -> None:
    """§12.1's `_load_job_for_device` row: UNCHANGED, and asserted as such.

    The property is that a device may complete only ITS OWN job. This reads the
    shipped predicate out of the gateway's AST rather than standing up a TLS
    socket: `_load_job_for_device` compares the job's `target_id` against the
    connected `device_id` and discards a mismatch as
    `gateway.identity_mismatch`. A site server is an ordinary `Device`, so it
    inherits that rule and this module does nothing to weaken it — which is the
    whole reason spec §3.2 argues no site-server certificate profile is needed:
    "never to job command" is not a scope to carve out, it is the default, and
    it is enforced here.
    """
    code = _code_of(BACKEND / "app" / "gateway" / "dispatch.py")
    assert "gateway.identity_mismatch" in code
    assert "_load_job_for_device" in code
    # And the module under test does not mention that function at all.
    for path in sorted(MODULE_DIR.rglob("*.py")):
        assert "_load_job_for_device" not in _code_of(path)


