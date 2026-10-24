"""`/api/v1/device-groups` -- the fleet's targeting primitive, now writable (audit
`docs/superpowers/audits/2026-08-21-ux-remediation-closeout.md`, rec. 3).

A KERNEL ROUTE, NOT A MODULE ONE. `DeviceGroup` lives in `app/models/core.py`
and is read by `deploy`, `patch`, `backup`, `remote`, `health`, `inventory`,
`bcp` and `documentation` (and, until migration 0230, the RBAC grant table).
Putting the list in any one of those modules would make seven others depend on
it, which is the thing
`CLAUDE.md`'s "modules register into the kernel; they do not fork it" is about.

TWO CODES, AND NEITHER IS NEW. Reads take `devices.read` (see the list
handler); every WRITE takes `devices.manage`, which migration 0003 already
seeds to `super-admin` and `org-admin` alone and whose catalogue description
has always read "Create, edit, GROUP, and decommission devices". It has gated
no route since Phase 0. `e25bbe7` made adding a permission code a deliberate
act; this cycle needs none -- no migration, no decision about which seeded
roles hold it, no new row in the matrix.

`technician` DELIBERATELY DOES NOT GET IT, even though `0062` gave that role
`assets.manage` on the reasoning that "recording the switch you are standing in
front of is the ordinary work of the role". Creating or renaming a group
re-binds patch, backup and remote policy for machines the technician is not
standing in front of (and, before migration 0230, a group was an RBAC scope --
`user_roles.device_group_id`; a grant names a client now). That is a different
class of act from writing an asset row.

CYCLE 2 ADDED THE RULES DOCUMENT, AND WITH IT THE ONE ROUTE HERE THAT IS NOT
CRUD. `POST /device-groups/preview` answers "which machines would this rule
claim" against a caller-supplied predicate. A group is what a
`target_type="group"` deployment resolves to, so a rule that matches more
machines than its author intended is a way to aim a signed job at machines
nobody chose -- and §5.3 chose a BACKGROUND re-evaluation over an immediate
one precisely so that a form submit is never a fleet-wide policy change. The
preview is the other half of that decision: the matched set is inspectable
BEFORE it is used. It is gated `devices.manage` rather than `devices.read`,
because answering it is the authoring act.

THE DOCUMENT IS CLOSED, AND IT IS CLOSED IN THE EVALUATOR'S OWN MODULE.
`GroupRules` lives beside `evaluate_rules` in `app/modules/inventory/rules.py`
and is built from the same op table and fact namespace the evaluator
dispatches on, because two independently maintained lists is how a rule this
API accepts becomes a rule the ingest skips -- with a `logger.warning` nobody
reads as the only evidence.

THE HOUSE FORM: `ctx: AuthContext = Depends(require_permission(...))` as a
PARAMETER, and every query issued inside `org_session(ctx.org_id, ...)` so the
`app.current_org` GUC is pinned and RLS applies to the read.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.deps import AuthContext, require_permission
from app.core.i18n import tr
from app.core.tenancy import org_session
from app.gateway.dispatch import publish_job
from app.modules.health.sweep import run_policy_reconcile
from app.modules.inventory.rules import GroupRules
from app.services.device_groups import (
    MAX_MEMBERS_PER_REQUEST,
    PREVIEW_SAMPLE,
    REEVALUATE_INTERVAL_S,
    DeviceGroupError,
    add_group_members,
    archive_device_group,
    create_device_group,
    get_device_group,
    list_device_groups,
    live_references,
    preview_rules,
    remove_group_member,
    unarchive_device_group,
    update_device_group,
)

router = APIRouter(prefix="/device-groups", tags=["device-groups"])

def _http(exc: DeviceGroupError) -> HTTPException:
    """By status, never by matching message prose."""
    return HTTPException(exc.status_code, exc.detail)


async def _reconcile_health(request: Request, session: Any, org_id: UUID) -> list[Any]:
    """Re-push `health.policy.apply` wherever this write moved a device's
    effective profile ahead of its acknowledgement.

    CALLED ONLY AFTER A REPARENT, and health is the only module that needs it.
    `patch/policy.py`, `backup/policy.py` and `remote/policy.py` all resolve a
    policy at POINT OF USE by reading `Device.device_group_id` at dispatch
    time, so a membership or hierarchy change needs no push at all.
    `health/resolution.py` is different: it walks `parent_group_id` UPWARD and
    `sweep.run_policy_reconcile` pushes where a device's acknowledged
    `policy_version` is behind its effective profile. Five health routes
    already call it after a write; a reparent moves the resolution for every
    device beneath the moved subtree, so this one does too.

    Returns the envelopes; the CALLER publishes them after the transaction
    commits, for `deploy/scripts.py::run_script`'s reason -- a job published
    before the caller's transaction commits could reach an agent with no
    durable server record if that commit then failed.
    """
    return await run_policy_reconcile(
        session, org_id, job_engine=request.app.state.job_engine
    )


async def _publish(request: Request, envelopes: list[Any]) -> None:
    redis = request.app.state.redis
    for _job_id, envelope in envelopes:
        await publish_job(redis, UUID(envelope["job"]["device"]), envelope)


class DeviceGroupOut(BaseModel):
    """One device group: a named, addressable subset of this org's fleet.

    `id` is the value every group-scoped write in the platform takes -- a
    deployment's `target_id`, a patch/backup/remote policy's
    `device_group_id` and a health profile assignment's group.

    `parent_group_id` is here because the hierarchy means different things to
    different consumers and the console has to be able to show it: health
    profile assignment inherits UP the parent chain, while a group-targeted
    deployment does not descend it at all.

    `dynamic` is `True` when the group carries membership rules, so its
    members are RECOMPUTED on every inventory ingest -- a console offering to
    hand-place a device into one of these would be offering an edit the next
    ingest reverts. The rules document itself is deliberately not returned:
    nothing edits it over HTTP yet, and a `dict` field here would make this
    response an open key space.
    """

    id: UUID = Field(description="Group id -- the value every group-scoped act takes")
    name: str = Field(description="Operator-facing label, unique among this org's live groups")
    parent_group_id: UUID | None = Field(
        description="Parent in the hierarchy health resolution walks upward, or null"
    )
    description: str = Field(description="Free text the operator wrote about this group")
    dynamic: bool = Field(
        description="True when membership is decided by rules rather than by hand"
    )
    #: Devices whose `device_group_id` IS this group. Direct members only --
    #: it is the blast radius of a group-targeted job, which does not descend
    #: into child groups.
    device_count: int = Field(description="Direct members; does not descend into child groups")
    #: WHICH RULE WINS. Lower sorts first, and `name` is only the tiebreak.
    #: Before migration 0072 the alphabet decided this, so a rename moved
    #: machines between patch, backup and remote policies.
    precedence: int = Field(
        description="Dynamic-membership precedence; lower wins, name breaks ties"
    )
    archived_at: datetime | None = Field(
        description="When this group was archived, or null while it is live"
    )


class MembershipEvaluationOut(BaseModel):
    """When the membership sweep last ran for this org, and what it did.

    WHY IT IS ON A READ THE CONSOLE ALREADY MAKES. The design
    (`docs/superpowers/specs/2026-08-22-device-group-management-design.md` §5.3,
    §9) asks the group page to state "when membership was last re-evaluated …
    a timestamp, never alone", and cycle 2 could not: the sweep's only durable
    record was its `device_group.membership_reevaluated` audit event, and
    reading the audit log needs `audit.read`, a code this page's `devices.read`
    gate does not entitle a caller to hold. So the page stated the INTERVAL
    instead -- true, and unable to tell *swept four minutes ago* from *the
    worker has been down since Tuesday*.

    `device_group_evaluation` (migration 0081) is an org-scoped row under the
    ordinary forced-RLS policy, so serving it here needs no new permission code
    and discloses strictly less about any device than `GET /devices` already
    does. GATE UNCHANGED: `devices.read`.

    `null` ON THE RESPONSE MEANS NEVER RUN, AND THE CONSOLE MUST NOT RENDER
    THAT AS "just now". An org whose first sweep has not landed and an org
    whose worker died are different sentences.
    """

    #: When the sweep last COMPLETED for this org. Written on every run,
    #: including the short-circuit run for an org with no rule-bearing group --
    #: "the worker is alive" is what an org with no rules still needs to know.
    last_evaluated_at: datetime = Field(
        description="When the membership sweep last completed for this organization"
    )
    #: When it last read EVERY active device rather than only those whose
    #: inventory moved. Both are needed: an incremental pass proves the worker
    #: runs, a full pass is what bounds the drift a watermark cannot see.
    last_full_pass_at: datetime | None = Field(
        description="When the sweep last read every active device, or null"
    )
    #: True when the LAST run was a full pass. False is the steady state.
    full_pass: bool = Field(description="Whether the last run read every active device")
    groups_evaluated: int = Field(description="Rule-bearing groups the last run weighed")
    devices_evaluated: int = Field(
        description="Devices the last run considered -- the changed ones, unless full_pass"
    )
    devices_moved: int = Field(description="Devices the last run re-homed")
    #: Devices with no `InventorySnapshot`. The evaluator reads DOCUMENTS, so
    #: these can never be claimed by any rule (§5.3) -- an operator not told
    #: this concludes their rule is wrong.
    devices_without_inventory: int = Field(
        description="Devices the last run could not match because they have no inventory"
    )
    #: How long a membership change can take to appear, in seconds. THE
    #: CONSTANT ITSELF, not a literal: the console says what the beat is, and a
    #: number copied into a component drifts from the schedule with nothing
    #: noticing.
    interval_seconds: float = Field(
        description="The sweep's beat interval -- the lag a membership change costs"
    )


class DeviceGroupListResponse(BaseModel):
    """A page of this org's groups, in `(precedence, name)` order, with the two
    counts a page of rows cannot carry itself.

    Wrapped rather than returned as a bare list for the reason
    `CandidateListResponse` is: the counts are the half that keeps the screen
    honest, and a list cannot hold them.
    """

    items: list[DeviceGroupOut]
    #: Groups matching BEFORE `limit`/`offset` -- a short page and the end of
    #: the list are otherwise the same thing on screen.
    total: int
    #: Devices belonging to NO group, which is the population the ORG DEFAULT
    #: governs.
    #:
    #: Every policy table reads `device_group_id IS NULL` as the org-wide
    #: fallback, so the default's subject is exactly these devices -- and the
    #: number is not derivable from a page of groups. Without it the console
    #: could list groups and still not say what the default it was stuck on
    #: actually covers, which is the audit finding rather than a decoration
    #: on it.
    ungrouped_device_count: int
    #: The membership sweep's last run, or `null` if it has never run for this
    #: org. Every `device_count` above is as of that moment, which is why the
    #: two travel together.
    evaluation: MembershipEvaluationOut | None = None


@router.get("", response_model=DeviceGroupListResponse)
async def list_groups(
    request: Request,
    ctx: AuthContext = Depends(require_permission("devices.read")),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    include_archived: bool = Query(
        default=False,
        description="Include archived groups, which are excluded by default",
    ),
) -> DeviceGroupListResponse:
    """This org's device groups, in the `(precedence, name)` order that decides
    which membership rule wins.

    WHY THIS ROUTE EXISTS. A group id is what `deploy`, `patch`, `backup`,
    `remote` and `health` all take to scope an action to
    part of the fleet, and no route had ever returned one. The console could
    therefore express exactly one scope -- the org default -- which is the one
    that has no id.

    `devices.read` RATHER THAN A NEW CODE, and the precedent is already in the
    tree: `app/modules/reporting/registry.py` maps the `device_groups` table
    to `devices.read`, so `/reports/patch-compliance` and
    `/reports/backup-coverage` have been declaring exactly this since they
    shipped. A group is a name for a set of devices; a caller entitled to see
    the fleet is entitled to see how it is carved up, and this returns
    strictly less about any device than `GET /devices` already does. A new
    code would need a migration and a decision about which seeded roles hold
    it, to gate a smaller fact than the route beside it.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        page = await list_device_groups(
            session,
            org_id=ctx.org_id,
            limit=limit,
            offset=offset,
            include_archived=include_archived,
        )
        return DeviceGroupListResponse(
            items=[
                DeviceGroupOut(
                    id=group.id,
                    name=group.name,
                    parent_group_id=group.parent_group_id,
                    description=group.description,
                    dynamic=group.rules is not None,
                    device_count=device_count,
                    precedence=group.precedence,
                    archived_at=group.archived_at,
                )
                for group, device_count in page.rows
            ],
            total=page.total,
            ungrouped_device_count=page.ungrouped_device_count,
            evaluation=(
                MembershipEvaluationOut(
                    last_evaluated_at=page.evaluation.last_evaluated_at,
                    last_full_pass_at=page.evaluation.last_full_pass_at,
                    full_pass=page.evaluation.full_pass,
                    groups_evaluated=page.evaluation.groups_evaluated,
                    devices_evaluated=page.evaluation.devices_evaluated,
                    devices_moved=page.evaluation.devices_moved,
                    devices_without_inventory=page.evaluation.devices_without_inventory,
                    interval_seconds=REEVALUATE_INTERVAL_S,
                )
                if page.evaluation is not None
                else None
            ),
        )


class LiveReferencesOut(BaseModel):
    """Everything still pointing at one group, counted per kind.

    THIS IS THE ARCHIVE REFUSAL, AND IT IS ALSO WHAT THE CONSOLE READS BEFORE
    OFFERING THE BUTTON. A 409 that says "still in use" without saying by what
    is a dead end; each count below is a place the operator has to go, so the
    console renders it as a link rather than as a blob.

    Eight foreign keys point at `device_groups.id` and every one of them is `NO
    ACTION` -- there is no `ON DELETE` clause in any migration -- so a hard
    `DELETE` raises where one holds and dangles where none does. That is why
    the archive is soft and why this model exists. (A ninth, the role grants of
    `user_roles`, ended with migration 0230: a grant names a client now.)
    """

    devices: int = Field(description="Devices whose device_group_id is this group")
    child_groups: int = Field(description="Live groups naming this one as their parent")
    policies: int = Field(description="Kernel `policies` rows scoped to this group")
    patch_policies: int = Field(description="Patch policies scoped to this group")
    backup_policies: int = Field(description="Backup policies scoped to this group")
    remote_policies: int = Field(description="Remote-control policies scoped to this group")
    health_assignments: int = Field(
        description="Health profile assignments bound to this group"
    )
    credentials: int = Field(description="Stored credentials scoped to this group")


class DeviceGroupDetailOut(BaseModel):
    """One group, with its membership rules and what is holding it.

    `rules` IS HERE AND NOT ON THE LIST ROUTE, and that is not an
    inconsistency. `787461f` kept the document off the paged list because a
    `dict[str, Any]` field makes a response an open key space; `GroupRules` is
    a closed Pydantic model, so a typed document on a single-row detail route
    has neither problem. The paged route still does not carry it -- a rules
    document per row is a different decision about response size, and nothing
    on the list screen edits one.
    """

    group: DeviceGroupOut = Field(description="The group itself")
    rules: GroupRules | None = Field(
        description="Membership rules, or null when this group is hand-curated"
    )
    references: LiveReferencesOut = Field(
        description="Live references, which are exactly what an archive is refused for"
    )


class GroupCreateRequest(BaseModel):
    """A new group. Static only in this cycle."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    #: THE CLIENT WHOSE DEVICES THE GROUP ORGANISES (MSP clients spec §1.5).
    #: Required, and fixed for the group's life: a group holds one client's
    #: devices, its parent is of the same client, and its rules claim only
    #: that client's machines.
    client_id: UUID = Field(description="The client whose devices this group organises")
    description: str = Field(default="", max_length=10_000)
    parent_group_id: UUID | None = Field(default=None)
    #: WHICH RULE WINS once cycle 2 can author one. Settable now because
    #: migration 0072's whole argument is that the column is free to land while
    #: nothing creates a rule-bearing group, and an operator building a
    #: hierarchy today should not have to redo the ordering later.
    precedence: int = Field(default=0, ge=-32768, le=32767)
    #: Absent or null for a hand-curated group; a closed document for a
    #: rule-driven one. A group is one or the other and never both (§10.3) --
    #: `evaluate_rules` CLAIMS a device rather than adding one, so a hand
    #: placement inside a rule-bearing group is an edit the next sweep reverts.
    rules: GroupRules | None = Field(
        default=None,
        description="Membership rules, or null for a hand-curated group",
    )


class GroupUpdateRequest(BaseModel):
    """A partial edit. Only the fields present are applied.

    A RENAME NEEDS NO CONFIRMATION HERE, and that is migration 0072's doing.
    While `evaluate_rules` ordered on `name`, renaming a rule-bearing group
    re-ordered the evaluator and moved machines between patch, backup and
    remote policies with no rule edited -- so this route would have owed a
    preview and a `confirm_moves` flag. With an explicit `precedence` column
    the name is a label, and a label change is an audited relabelling.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    parent_group_id: UUID | None = Field(default=None)
    precedence: int | None = Field(default=None, ge=-32768, le=32767)
    #: ABSENT AND NULL MEAN DIFFERENT THINGS HERE, and `exclude_unset` in the
    #: handler is what tells them apart. Absent leaves the document alone;
    #: `null` clears it, which turns a rule-driven group static WITHOUT moving
    #: anybody -- `evaluate_rules` only ever assigns on a match and never
    #: unassigns, so the members stay exactly where they are. §7.2 needs that
    #: property, or the two rules in this spec deadlock a rule-bearing group
    #: into being unarchivable: membership writes are refused on it, so its
    #: devices can only be released after the rules come off.
    rules: GroupRules | None = Field(
        default=None, description="Membership rules; null clears them"
    )


class MembersAddRequest(BaseModel):
    """Devices to place into this group, by id."""

    model_config = ConfigDict(extra="forbid")

    device_ids: list[UUID] = Field(min_length=1, max_length=MAX_MEMBERS_PER_REQUEST)


class MemberResultOut(BaseModel):
    """One device id's outcome. Per id, never a batch failure."""

    device_id: UUID = Field(description="The device this row is about")
    moved: bool = Field(description="True when this call changed the device's group")
    reason: str = Field(
        description=(
            "Why: added, already a member, no such device, or belongs to another client"
        )
    )


class MembersAddResponse(BaseModel):
    """Per-id results, in the order the ids were given."""

    results: list[MemberResultOut] = Field(description="One row per requested device id")


def _out(group: Any, device_count: int) -> DeviceGroupOut:
    return DeviceGroupOut(
        id=group.id,
        name=group.name,
        parent_group_id=group.parent_group_id,
        description=group.description,
        dynamic=group.rules is not None,
        device_count=device_count,
        precedence=group.precedence,
        archived_at=group.archived_at,
    )


@router.post("", response_model=DeviceGroupOut, status_code=status.HTTP_201_CREATED)
async def create_group(
    body: GroupCreateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("devices.manage")),
) -> DeviceGroupOut:
    """Create a device group.

    A duplicate name among this org's LIVE groups is a 409 with a sentence in
    it, never a 500 out of an `IntegrityError`. An archived group's name is
    free, because migration 0072 made the unique index partial -- otherwise an
    archived group would hold its name against every future group forever.

    `device_count` is 0 by construction: nothing is in a group that has just
    been created.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            group = await create_device_group(
                session,
                org_id=ctx.org_id,
                client_id=body.client_id,
                actor_id=ctx.user_id,
                name=body.name,
                description=body.description,
                parent_group_id=body.parent_group_id,
                precedence=body.precedence,
                rules=body.rules.model_dump() if body.rules else None,
            )
        except DeviceGroupError as exc:
            raise _http(exc) from None
        out = _out(group, 0)
    return out


class RulePreviewRequest(BaseModel):
    """A candidate rules document, and the group it is for or its client.

    A PREVIEW RUNS OVER ONE CLIENT'S DEVICES, as the sweep claims them (MSP
    clients spec §1.5): the saved group's client, or `client_id` for a group
    not saved yet -- required then, as it is on `GroupCreateRequest`.
    """

    model_config = ConfigDict(extra="forbid")

    rules: GroupRules = Field(
        description="The document to dry-run. Validated exactly as a write would be"
    )
    #: Optional, and it does one thing: it must resolve in this org. That makes
    #: a preview about a group the caller cannot see a 404 rather than an
    #: answer, and it gives the console's edit flow something to name.
    group_id: UUID | None = Field(
        default=None, description="The group being edited, when there is one"
    )
    client_id: UUID | None = Field(
        default=None,
        description="The client of a group not saved yet; required when group_id is absent",
    )

    @model_validator(mode="after")
    def _names_its_client(self) -> "RulePreviewRequest":
        if self.group_id is None and self.client_id is None:
            raise ValueError(
                tr(
                    "a preview needs the group's client: send client_id for a group that is "
                    "not saved yet"
                )
            )
        return self


class RuleMatchOut(BaseModel):
    """One device the rule would claim, and where it is now.

    THE CURRENT GROUP IS THE POINT. A rule does not add a device to a group, it
    MOVES it -- and `current_group_id: null` is not "no policy", it is the
    organisation default that every patch, backup, remote and health resolver
    reads. A sample that named only hostnames would hide what the rule costs.
    """

    device_id: UUID = Field(description="The device this row is about")
    hostname: str | None = Field(description="Its hostname, or null for an asset row")
    current_group_id: UUID | None = Field(
        description="The group it is in now, or null for the organisation default"
    )
    current_group_name: str | None = Field(
        description="That group's name, or null for the organisation default"
    )


class RulePreviewResponse(BaseModel):
    """What a candidate rule reaches: two exact counts and a bounded sample."""

    matched: int = Field(description="Devices this document matches, exactly")
    devices_without_inventory: int = Field(
        description="Active devices with no inventory document, which no rule can match"
    )
    sample: list[RuleMatchOut] = Field(
        description=f"At most {PREVIEW_SAMPLE} of the matched devices, by name"
    )
    sample_truncated: bool = Field(
        description="True when more devices matched than the sample names"
    )


# REGISTERED BEFORE `/{group_id}`, DELIBERATELY. FastAPI matches
# first-registered-wins, so a future `POST /device-groups/{group_id}` declared
# above this line would swallow `preview` as a group id and answer 404 or 422
# for a route that exists. Nothing shadows it today -- the POSTs on this router
# are `""`, `/{group_id}/unarchive` and `/{group_id}/members`, none of which is
# a bare `/{group_id}` -- and this comment is what keeps that true.
@router.post("/preview", response_model=RulePreviewResponse)
async def preview_group_rules(
    body: RulePreviewRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("devices.manage")),
) -> RulePreviewResponse:
    """Dry-run a membership rules document: what would it claim, and what
    cannot be claimed at all. Writes nothing.

    WHY THIS IS A WRITE PERMISSION ON A READ (§8.1). Every other read here
    takes `devices.read`, because a group is a name for a set of devices and a
    caller entitled to see the fleet is entitled to see how it is carved up.
    This one is different in kind: it enumerates the fleet against an ARBITRARY
    CALLER-SUPPLIED PREDICATE and answers "which machines would this claim",
    which is the authoring act rather than a reading of what exists.

    WHY IT EXISTS. A group is what a `target_type="group"` deployment resolves
    to, so a rule matching more machines than its author intended is a way to
    aim a signed job at machines nobody chose. §5.3 put re-evaluation in a
    background sweep so that a form submit is never a fleet-wide policy change;
    without a preview that would merely move the same gamble five minutes
    later. The matched set is inspectable before it is used, or it is not safe.

    `devices_without_inventory` IS NOT A FOOTNOTE. The evaluator reads
    inventory DOCUMENTS and never device rows, so a manually-created asset, a
    discovered switch and an enrolled machine that has not yet collected are
    all unmatchable. An operator who writes a rule for a fleet, receives half
    of it, and is not told why concludes the rule is wrong.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            preview = await preview_rules(
                session,
                org_id=ctx.org_id,
                rules=body.rules.model_dump(),
                group_id=body.group_id,
                client_id=body.client_id,
            )
        except DeviceGroupError as exc:
            raise _http(exc) from None
    return RulePreviewResponse(
        matched=preview.matched,
        devices_without_inventory=preview.devices_without_inventory,
        sample=[
            RuleMatchOut(
                device_id=match.device_id,
                hostname=match.hostname,
                current_group_id=match.current_group_id,
                current_group_name=match.current_group_name,
            )
            for match in preview.sample
        ],
        sample_truncated=preview.sample_truncated,
    )


@router.get("/{group_id}", response_model=DeviceGroupDetailOut)
async def get_group(
    group_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("devices.read")),
) -> DeviceGroupDetailOut:
    """One group, live or archived, with every live reference to it counted.

    `devices.read`, the same code as the list: a group is a name for a set of
    devices, and a caller entitled to see the fleet is entitled to see how it
    is carved up. The reference counts are what they already are on the list
    route -- shapes of the fleet, not device data.

    An archived group stays readable by id forever. That is the point of a soft
    delete: the hash-chained audit log carries group ids in its metadata, and a
    `user.role_assigned` event naming a group must still resolve to a name in
    five years.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            group = await get_device_group(session, ctx.org_id, group_id)
        except DeviceGroupError as exc:
            raise _http(exc) from None
        references = await live_references(session, ctx.org_id, group_id)
        out = DeviceGroupDetailOut(
            group=_out(group, references["devices"]),
            # Validated on the way OUT as well as on the way in. A row written
            # before this model existed -- or by hand with SQL -- would fail
            # here loudly rather than be served as an open key space, which is
            # the property `787461f` kept the document off the list route for.
            rules=GroupRules(**group.rules) if group.rules else None,
            references=LiveReferencesOut(**references),
        )
    return out


@router.patch("/{group_id}", response_model=DeviceGroupOut)
async def update_group(
    group_id: UUID,
    body: GroupUpdateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("devices.manage")),
) -> DeviceGroupOut:
    """Rename, re-describe, reparent or reorder a group.

    THE HIERARCHY CHECKS ARE HERE BECAUSE THE SCHEMA HAS NONE.
    `parent_group_id` is a bare self-FK: no cycle constraint, no depth bound,
    no org predicate. Self-parent, a cycle, a parent belonging to another org
    and a move that would push a leaf past health's 16-level walk are each a
    422. `health/resolution.py` survives a cycle -- bounded walk,
    `logger.error`, fall through to the org default -- but surviving it means
    every device beneath the cycle silently resolves to the wrong profile.

    A REPARENT RE-PUSHES HEALTH POLICY AND NOTHING ELSE DOES. See
    `_reconcile_health`: health is the only module that pushes a group binding
    to an endpoint; patch, backup and remote resolve at point of use.
    """
    # `exclude_unset` IS THE WHOLE MECHANISM for `rules` here: absent leaves
    # the document alone, an explicit `null` clears it. Dumped in PYTHON mode,
    # not `mode="json"`, because `parent_group_id` has to stay a `UUID` for the
    # hierarchy checks and the column -- and `RuleCondition`'s own fields are
    # `str | int | float | bool` either way, so the document lands in the jsonb
    # column JSON-native regardless.
    fields = body.model_dump(exclude_unset=True)
    envelopes: list[Any] = []
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            group, reparented = await update_device_group(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                group_id=group_id,
                changes=fields,
            )
        except DeviceGroupError as exc:
            raise _http(exc) from None
        references = await live_references(session, ctx.org_id, group_id)
        out = _out(group, references["devices"])
        if reparented:
            envelopes = await _reconcile_health(request, session, ctx.org_id)
    await _publish(request, envelopes)
    return out


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_group(
    group_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("devices.manage")),
) -> Response:
    """Archive a group, or refuse with everything that is holding it.

    SOFT, NOT HARD, on `15ed5de`'s argument and `devices.py:427`'s: a delete
    would take the group's job history, its documentation links and its audit
    trail's target with it, and eight `NO ACTION` foreign keys mean it would
    not even succeed. Retirement leaves all of that answerable.

    REFUSED WHILE OCCUPIED, with a `blocked_by` body, because `NULL` is not an
    absence. `patch/policy.py`, `backup/policy.py`, `remote/policy.py` and
    `health/resolution.py` all read `device_group_id IS NULL` as the ORG
    DEFAULT, so re-homing a 300-device group's members would be a 300-device
    patch, backup and remote policy change wearing a Delete button. The
    operator can still do it -- by emptying the group first, in an act that
    says what it is.

    NO health reconcile: the refusal guarantees the group holds no devices and
    no health assignment, so no device's effective profile can move.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await archive_device_group(
                session, org_id=ctx.org_id, actor_id=ctx.user_id, group_id=group_id
            )
        except DeviceGroupError as exc:
            if exc.blocked_by is None:
                raise _http(exc) from None
            # A JSONResponse rather than an HTTPException, so `blocked_by` sits
            # beside `detail` instead of nested inside it. The console reads
            # this body to render each blocker as a link; a refusal the
            # operator cannot act on is a dead end, and this one is a to-do
            # list.
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail, "blocked_by": exc.blocked_by},
            )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{group_id}/unarchive", response_model=DeviceGroupOut)
async def unarchive_group(
    group_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("devices.manage")),
) -> DeviceGroupOut:
    """Bring an archived group back.

    `bd04957`'s reason, quoted in the UX closeout for exactly this shape: a
    one-way state change with no way back is the defect (B76). Same permission
    as the archive -- undoing a state change is not a lesser privilege than
    making it -- and its own audit event.

    Refused when a live group has taken the name in the meantime, which the
    partial unique index makes possible, and refused when the parent was
    archived after this group was, which is the one way an orphan could appear.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            group = await unarchive_device_group(
                session, org_id=ctx.org_id, actor_id=ctx.user_id, group_id=group_id
            )
        except DeviceGroupError as exc:
            raise _http(exc) from None
        references = await live_references(session, ctx.org_id, group_id)
        out = _out(group, references["devices"])
    return out


@router.post("/{group_id}/members", response_model=MembersAddResponse)
async def add_members(
    group_id: UUID,
    body: MembersAddRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("devices.manage")),
) -> MembersAddResponse:
    """Put devices into this group, answering per id.

    PER-ID RESULTS RATHER THAN A BATCH FAILURE, in the shape
    `POST /devices/candidates/promote` already uses: one stale entry in a
    selection must not silently discard the rest, and "already a member" is not
    an error -- two operators looking at the same screen is the normal case.

    Bounded at 500 ids so one audit event's `device_ids` list stays a readable
    record rather than an unbounded blob.

    NO health reconcile, and that is measured rather than assumed:
    `resolve_profile_for_device` starts from `device.device_group_id`, so a
    membership change CAN move a device's effective profile -- but the reconcile
    that would push it is a fleet-wide sweep, and running one per membership
    edit on a screen whose whole job is bulk is the job storm §12 of the health
    spec exists to avoid. The device's next tick picks it up.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            results = await add_group_members(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                group_id=group_id,
                device_ids=body.device_ids,
            )
        except DeviceGroupError as exc:
            raise _http(exc) from None
        out = MembersAddResponse(
            results=[
                MemberResultOut(
                    device_id=r.device_id, moved=r.moved, reason=r.reason
                )
                for r in results
            ]
        )
    return out


@router.delete(
    "/{group_id}/members/{device_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_member(
    group_id: UUID,
    device_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("devices.manage")),
) -> None:
    """Take one device out of this group.

    IT DOES NOT LOSE POLICY, IT CHANGES POLICY -- to whatever the org default
    says, because every policy resolver reads `device_group_id IS NULL` as the
    org-wide fallback. The console says so before it happens; the API records
    it either way.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await remove_group_member(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                group_id=group_id,
                device_id=device_id,
            )
        except DeviceGroupError as exc:
            raise _http(exc) from None
