"""NO ROUTE MAY OPEN ITS OWN DATABASE SESSION WITHOUT SAYING WHY.

Audit `docs/superpowers/audits/2026-08-21-tenancy-test-efficacy.md`, R7.

THE FINDING THIS WOULD HAVE CAUGHT ON THE DAY IT SHIPPED. `app/api/v1/errors.py`
opened `AsyncSession(request.app.state.app_engine)` directly instead of
`org_session(...)`, so no `app.current_org` GUC was ever set for those two
routes. Everywhere else in this codebase that is harmless -- the GUC read is
`missing_ok=true`, the comparison is NULL, and a tenant table returns zero rows.
It was not harmless there, because `platform_error_event`'s only RLS policy is
`USING (true)`, and the route returned every tenant's `org_id`, `user_id`,
`job_id` and `device_id` to any org administrator (F1). Finding it took someone
reading a migration three revisions later and noticing a policy shape. The
structural fact -- a route layer that opens a raw session -- was visible in the
diff that introduced it.

WHAT THIS TEST IS AND IS NOT. It is not a claim that `org_session` is always
right; several of the exceptions below are correct and permanent. It is a claim
that the choice is never IMPLICIT. A route that reaches past `org_session` or
`portal_session` for a raw engine is making a tenancy decision, and it must be
written down here, with a reason, before it can merge.

THE ALLOWLIST IS EXACT, NOT A FLOOR. An entry that no longer exists fails just
as loudly as a hit that is not listed, so the reasons cannot outlive the code
they describe -- the failure mode of every allowlist that only checks one
direction.

WHY THE SCAN IS SOURCE-LEVEL. The alternative is importing every route module
and inspecting call graphs, which finds the same four call sites and depends on
import side effects, app state and the module registry all being healthy. A
route that opens a session builds it with a literal `AsyncSession(...)` call in
its own body; `ast` sees that with nothing running.
"""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
APP = BACKEND / "app"

# What counts as "the request layer": everything under `app/api/` (routers,
# their dependencies, and the probe endpoints) plus every module file that
# constructs an `APIRouter`. Derived, not listed, so a new module's routes are
# in scope the moment they exist.
def _route_modules() -> list[Path]:
    files = sorted(p for p in (APP / "api").rglob("*.py") if "__pycache__" not in p.parts)
    for path in sorted((APP / "modules").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if "APIRouter(" in path.read_text():
            files.append(path)
    return files


# key: "<path relative to app/>::<enclosing function>" -> why it is allowed.
#
# Every entry names an engine that is NOT reached through `org_session`, so
# every entry is a place where RLS is not doing the tenant scoping and
# something else has to be.
RAW_SESSIONS_BY_DESIGN: dict[str, str] = {
    "modules/agent_distribution/api.py::_unpublish": (
        "admin_engine, READ-WRITE, and deliberately so: `agent_releases` has "
        "no org_id -- the catalogue is the platform operator's, shared by every "
        "tenant -- and the rollouts that point into it belong to every org. "
        "A tenant session would hide other orgs' rollouts and let a release be "
        "unpublished under them. Behind `agent.release.manage` (super-admin "
        "only), refused while ANY org's active rollout uses the release, and "
        "every removal written to the caller's own audit chain in the same "
        "transaction. The only cross-org write is clearing `release_id` on "
        "past rollout targets, whose history keeps the version strings."
    ),
    "modules/agent_distribution/api.py::delete_version_route": (
        "admin_engine, READ-ONLY, one query: the ids of one version's catalogue "
        "rows, which carry no org_id. The removal itself is `_unpublish`."
    ),
    "api/deps.py::admit": (
        "admin_engine, and it is the ONE pre-tenant step in the request path "
        "(`auth_context` and the portal principal both admit through it): "
        "the org is looked up BY SLUG from the validated token, so there is no "
        "org_id to pin a tenant session with yet. Read-only, one column, and "
        "the very next statement opens `org_session(org_id)` for everything "
        "else the dependency does."
    ),
    "api/v1/ws.py::ui_websocket": (
        "admin_engine, the same pre-tenant org-slug lookup as `auth_context`, "
        "for the same reason: a socket carries its token in the subprotocol "
        "header and the org is not known until that token is validated. "
        "Read-only; the handler moves onto tenant-scoped reads afterwards."
    ),
    "api/v1/errors.py::list_error_groups": (
        "app_engine WITHOUT a tenant GUC, deliberately: `platform_error_group` "
        "is platform-scoped by design (error-monitoring spec §4.1) and pinning "
        "`app.current_org` here would claim a tenant boundary the table does "
        "not have. The boundary is the `platform.errors.read` permission, "
        "which migration 0068 moved to `super-admin` -- no tenant "
        "administrator holds it and no org-scoped surface can grant it."
    ),
    "api/v1/errors.py::get_error_group": (
        "app_engine WITHOUT a tenant GUC, same reasoning as the list route -- "
        "and this is the route audit F1 reproduced the disclosure through, so "
        "the reasoning is worth stating twice. It reads "
        "`platform_error_event`, whose `org_id`/`user_id`/`job_id`/`device_id` "
        "columns cross every tenant and whose RLS policy is `USING (true)`. "
        "Nothing beneath this route scopes it; `platform.errors.read` "
        "(super-admin only, 0068) is the entire boundary."
    ),
}


def _raw_session_sites() -> dict[str, str]:
    """Every `AsyncSession(<engine>)` construction in the request layer, keyed
    by module and enclosing function, valued by the engine expression."""
    found: dict[str, str] = {}
    for path in _route_modules():
        tree = ast.parse(path.read_text())
        enclosing: dict[ast.AST, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                for child in ast.walk(node):
                    enclosing.setdefault(child, node.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name != "AsyncSession":
                continue
            where = enclosing.get(node, "<module>")
            key = f"{path.relative_to(APP)}::{where}"
            found[key] = ast.unparse(node.args[0]) if node.args else "<no engine argument>"
    return found


def test_no_route_opens_a_raw_session_without_a_recorded_reason() -> None:
    """The whole point, and it fails in both directions.

    A NEW hit means somebody wrote a route that talks to Postgres outside
    `org_session`/`portal_session`. That may be right -- two of the four
    recorded exceptions are -- but it is a tenancy decision, and an undocumented
    tenancy decision is how F1 happened.

    A STALE entry means a reason is describing code that no longer exists, which
    is worse than no reason at all: the next reader takes it as evidence the
    pattern is blessed.
    """
    found = _raw_session_sites()
    undocumented = {k: v for k, v in found.items() if k not in RAW_SESSIONS_BY_DESIGN}
    assert not undocumented, (
        "these request-layer functions open a raw AsyncSession instead of "
        "org_session()/portal_session(), so RLS is not scoping them and "
        "something else must be. Use org_session, or add the site to "
        "RAW_SESSIONS_BY_DESIGN with the reason it is safe: "
        f"{sorted(undocumented.items())}"
    )
    stale = sorted(set(RAW_SESSIONS_BY_DESIGN) - set(found))
    assert not stale, (
        "these recorded exceptions no longer exist in the code. Delete them "
        f"rather than leaving a reason attached to nothing: {stale}"
    )


def test_only_the_errors_routes_reach_for_the_app_engine_without_a_tenant() -> None:
    """The two classes of exception are not equally serious and the test says so.

    `admin_engine` is BYPASSRLS but the two sites that use it read ONE column
    of `organizations` before any tenant exists. `app_engine` with no GUC is the
    F1 shape: the session is exactly the one every request uses, minus the
    setting that makes RLS mean anything. Today that shape lives in two
    functions, both in the error monitor, both gated on a permission no tenant
    administrator holds. A third would be a finding, not a style question."""
    app_engine_sites = sorted(
        key for key, engine in _raw_session_sites().items() if "app_engine" in engine
    )
    assert app_engine_sites == [
        "api/v1/errors.py::get_error_group",
        "api/v1/errors.py::list_error_groups",
    ], app_engine_sites


def test_the_scan_is_not_vacuous() -> None:
    """Without this, a rename of `AsyncSession`, a moved package or a typo in
    the glob reports a perfectly disciplined route layer. Both halves are
    checked: that the scan sees the modules, and that it finds the call sites it
    is supposed to find."""
    modules = _route_modules()
    assert len(modules) > 30, len(modules)
    assert any(m.name == "errors.py" for m in modules)
    assert any(m.parts[-2] == "modules" or "modules" in m.parts for m in modules)
    assert len(_raw_session_sites()) >= 4
