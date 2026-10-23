"""ONLY NAMED TABLES MAY LACK A TENANT COLUMN -- OR CARRY ONE THAT ISN'T A KEY.

Invariant #5: all data access is tenant-scoped, enforced at the database layer
via RLS. `platform_health_sample` was the first deliberate exception --
Postgres being unreachable is not any organisation's fact, and an `org_id`
there would be a fiction every read had to filter on for no meaning.
`platform_error_group`/`platform_error_event` are the second and third (spec
§4.1 of `docs/superpowers/specs/2026-08-15-error-monitoring-design.md`): a
broken code path is platform information, not one org's property, and
`platform_error_event.org_id` -- present in the schema, unlike the other two
-- is recorded here anyway as a nullable CORRELATION column rather than a
tenancy key, so a stricter sweep that checks *how* `org_id` is used, not merely
whether the column exists, still finds it named.

THAT STRICTER SWEEP NOW EXISTS and this file is no longer the last word:
`tests/test_rls.py::test_every_org_scoped_table_has_a_policy_that_mentions_org_id`
(and block 3 of `deploy/postgres/assert-rls.sql`) require a tenant table to
carry a permissive RLS policy whose predicate actually references `org_id` --
because `platform_error_event` satisfied "RLS enabled, forced, and has a
policy" with `USING (true)`, and that is how a table with an `org_id` column
came to have no tenant boundary at all.

The guarantee that matters is not "this table is fine". It is "no untracked
one appears without somebody deciding". A sweep that names the permitted
exceptions makes the next one fail loudly instead of arriving unnoticed.
"""

from app.models.base import Base

TENANTLESS_BY_DESIGN = frozenset(
    {
        # Postgres being unreachable is not any organisation's fact.
        "platform_health_sample",
        # A code path being broken is platform information, and grouping is
        # across orgs by construction -- the whole point is "this line has
        # failed 4,000 times", which is not one org's property.
        "platform_error_group",
        # Carries `org_id` as a nullable CORRELATION column, not as a tenancy
        # key: an error at process startup belongs to no org. Access is by the
        # `platform.errors.read` code rather than by tenant scope.
        #
        # THAT PREMISE WAS FALSE FOR NINE DAYS, AND THIS IS THE CORRECTION.
        # This reason used to read "the platform-wide `platform.errors.read`
        # code". The code was not platform-wide: migration 0057 granted it to
        # `org-admin`, the role every tenant's own administrator holds. So the
        # exemption's justification and the RBAC seed were decided separately
        # and contradicted each other, and the gap between them was a live
        # cross-tenant disclosure -- this table's `org_id`, `user_id`, `job_id`
        # and `device_id` reaching any org administrator through
        # `GET /api/v1/platform/errors/{group_id}`
        # (`docs/superpowers/audits/2026-08-21-tenancy-test-efficacy.md` F1).
        # Migration 0068 made the premise true: both codes now sit on
        # `super-admin`, which no org-scoped console surface can grant, clone
        # or assign.
        #
        # An exemption from tenancy is only ever as good as the authorization
        # that replaced it. Anything added to this set owes the same sentence:
        # not merely "RLS does not apply here", but "and this is what does".
        "platform_error_event",
    }
)

# Tables that legitimately have no tenant because they are global reference or
# identity data, not per-org facts. Measured, not guessed: this is the actual
# output of running this sweep with GLOBAL_BY_NATURE empty against
# Base.metadata (app/models/core.py), not a list written from memory of what
# "should" be global. Each entry is listed with why, so a new entry is a
# visible decision rather than a silent expansion of the set.
GLOBAL_BY_NATURE = frozenset(
    {
        # The tenant table itself. An org row cannot be scoped to an org_id --
        # that would be self-reference, not tenancy.
        "organizations",
        # The RBAC permission catalog: fixed, code-defined action identifiers
        # ("device.read", ...) shared by every org, not a per-org fact. Roles
        # attach org-scoped meaning to these via role_permissions/roles.org_id.
        "permissions",
        # Join table between roles and permissions. It carries no org_id of its
        # own; its tenancy is inherited transitively through roles.org_id
        # (roles.org_id is itself nullable -- NULL for the global roles seeded
        # in 0028_portal.py -- so role_permissions has nothing consistent to
        # denormalize). Note "roles" itself is NOT in this set: the Role model
        # (app/models/core.py) does carry org_id and is correctly covered by
        # the tenant_isolation RLS pattern already.
        "role_permissions",
        # The agent artefact catalogue. Same shape of argument as
        # `permissions`: the rows describe things shared by every org, not one
        # org's facts. An artefact is byte-identical for every tenant because an
        # enrollment token is never inside one (agent/packaging/README.md
        # forbids it), so a per-org copy would isolate nothing and multiply
        # storage. What IS per-tenant is the right to publish, which lives in
        # RBAC (`agent.release.manage`).
        #
        # The cost of being in this set, stated because it is easy to miss: RLS
        # is not the backstop for these routes. A forgotten
        # `require_permission` on an agent-release route fails OPEN, where the
        # same mistake on a tenant table still fails closed at the database.
        # See docs/modules/agent-distribution/spec.md §4.2.
        "agent_releases",
    }
)


def test_only_the_named_table_lacks_a_tenant_column() -> None:
    offenders = {
        table.name
        for table in Base.metadata.sorted_tables
        if "org_id" not in table.columns
        and table.name not in TENANTLESS_BY_DESIGN
        and table.name not in GLOBAL_BY_NATURE
    }
    assert not offenders, (
        "these tables have no org_id and are not a recorded exception. Either "
        "add a tenant column, or add the table to TENANTLESS_BY_DESIGN with a "
        f"written reason in the spec: {sorted(offenders)}"
    )


def test_the_exception_list_has_not_rotted() -> None:
    """A named exception pointing at a table nothing defines exempts nothing."""
    live = {t.name for t in Base.metadata.sorted_tables}
    stale = (TENANTLESS_BY_DESIGN | GLOBAL_BY_NATURE) - live
    assert not stale, f"recorded exceptions that no longer exist: {sorted(stale)}"


def test_the_sweep_is_not_vacuous() -> None:
    """Without this, a broken metadata import reports a perfectly tenant-scoped
    schema."""
    assert len(Base.metadata.sorted_tables) > 50
