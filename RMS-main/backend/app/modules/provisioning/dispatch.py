"""Provisioning run-start dispatch (docs/modules/provisioning/spec.md §7/§8).

`start_provision_run` is the only path by which a `ProvisionRun` moves from
nothing to `awaiting_enrollment`: it org-scoped-loads the profile and its
image (fail-closed if either is missing, or the profile is disabled), issues
a single-use enrollment token, renders and stores the OS-specific answer file
(skipped for macOS, which enrolls via ADE/MDM binding rather than an answer
file), builds and signs the `provision.prepare` site-server contract envelope
(invariant #1 — the site-server path is signed exactly like an agent job,
even though v1 has no site-server consumer to publish it to), and audits both
the prepare intent and the run start.

`actor_id` is REQUIRED, not optional: `create_token`'s `created_by` is a
non-null FK to `users`, and v1 provisioning runs are operator-initiated only
(the worker's Task-11 timeout sweep only *fails* stale runs — it never starts
one), so there is no system/service-user sentinel to fall back to here.

Invariant #3 (secrets never leak outside the sealed/answer-object path):
the issued token's plaintext secret and the rendered answer bytes (which may
contain resolved domain-join / local-admin credentials) are used only to
build the answer object handed to `object_store.put_provision_object` and,
for the token, threaded straight into that render call. Neither ever appears
in an audit metadata dict, a log line, an API response, or any `ProvisionRun`
column other than `answer_ref` (a storage reference, not the plaintext).
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.rbac import has_permission
from app.core.storage import ObjectStore
from app.core.vault import VaultService
from app.jobs.engine import JobEngine
from app.models import ProvisionRun
from app.modules.provisioning.render import render_answer_file
from app.modules.provisioning.repository import (
    ProvisioningPermissionError,
    ProvisioningValidationError,
    get_image,
    get_profile,
)
from app.modules.provisioning.schema import ProvisionWakeRequest
from app.modules.site_server.dispatch import wake_via_site_server
from app.modules.site_server.errors import SiteServerError
from app.modules.site_server.registry import list_site_servers
from app.services.enrollment import create_token

_ANSWER_FILE_OS_FAMILIES = {"windows", "linux"}


async def _resolve_wake(
    session: AsyncSession,
    org_id: UUID,
    wake: ProvisionWakeRequest,
    *,
    actor_id: UUID,
) -> UUID:
    """Which site server will emit the wake, or a refusal BEFORE the run exists.

    **TWO DELIBERATE BEHAVIOUR CHANGES, BOTH OF WHICH CAN BREAK AN EXISTING
    CALLER, AND BOTH OF WHICH ARE THE POINT.**

    1. **No site server serves the target's segment => refusal, before the run
       is created.** Starting the run anyway and not waking would reproduce
       exactly the defect being fixed, one layer down: a caller who passes
       `wake_on_lan` today gets a run and no wake. After this they may get a
       422 -- which is the difference between "we ignored you" and "we cannot do
       that". (422, not 400: `api.py` maps every `ProvisioningValidationError`
       out of this path to `HTTP_422_UNPROCESSABLE_ENTITY`. This paragraph said
       400 and was wrong about its own route.) Raised here, before
       `ProvisionRun` is constructed, so a refused wake leaves NO run behind
       rather than a half-provisioned one.

    2. **`wake_on_lan` additionally requires `site.wake`**, checked at the
       SERVICE boundary. The route stays gated on `provisioning.run`; letting
       that code confer LAN-wide wake for free would collapse the separation
       `docs/modules/site-server/spec.md` §10.2 chose deliberately. In practice
       `technician` holds both, so no seeded role loses anything -- the case that
       matters is a principal with `provisioning.run` and NOT `site.wake`, who
       gets a run when `wake_on_lan` is absent and a 403 when it is present.

    **THE SELECTION IS SUBJECT TO §0.2 EXACTLY AS `_resolve_broadcast` IS, AND
    THIS LAYER IS WHERE IT WAS BEING LOST.** `_resolve_broadcast` never guesses a
    wire -- but it is only ever asked about the site server this function already
    chose, so choosing wrongly here defeats it entirely and does so SILENTLY: the
    caller gets a 202, the audit log records a dispatched wake, and the magic
    packet lands on a different LAN. Two rules, and neither of them guesses:

    * **`segment` absent is a REFUSAL when the org has more than one enabled site
      server.** Absent means no containment filter, so every enabled server
      matches and any pick is arbitrary -- "whichever row was promoted first" is
      not a topology fact. Defaulted only when there is nothing to choose
      between, which is `_resolve_broadcast`'s own rule for a single declared
      segment, one level up.
    * **`segment` present selects by CONTAINMENT AND THEN BY SPECIFICITY**, both
      in `list_site_servers`. A server declared for `10.30.0.0/16` is not a
      candidate for a target on `10.20.7.5`, and between one declared for
      `10.20.0.0/16` and one declared for `10.20.7.0/24` the /24 is the machine
      actually on that wire.
    """
    if not await has_permission(session, actor_id, "site.wake"):
        raise ProvisioningPermissionError(
            tr(
                "wake_on_lan requires the `site.wake` permission as well as "
                "`provisioning.run`: deciding to put a magic packet on a LAN segment "
                "is a separate act from starting a provisioning run"
            )
        )

    try:
        candidates = await list_site_servers(
            session, org_id, enabled=True, segment=wake.segment
        )
    except SiteServerError as exc:
        raise ProvisioningValidationError(str(exc)) from None

    if not candidates:
        raise ProvisioningValidationError(
            tr(
                "no enabled site server serves {segment}: "
                "a Wake-on-LAN packet must originate on the target's own segment, and "
                "nothing in this org is declared for it. Promote a site server on that "
                "segment, or start the run without wake_on_lan",
                segment=wake.segment or tr("the requested segment"),
            )
        )

    if wake.segment is None and len(candidates) > 1:
        raise ProvisioningValidationError(
            tr(
                "wake_on_lan.segment was not given and this org has {count} "
                "enabled site servers: with no segment there is nothing to filter on, "
                "so {target_mac} cannot be located and picking a server would put "
                "the magic packet on whichever LAN happened to be registered first. "
                "Name the segment the target is on",
                count=len(candidates),
                target_mac=wake.target_mac,
            )
        )

    # `list_site_servers` has already ordered these by the specificity of their
    # matching segment, so `[0]` is the server declared for the target's own
    # wire rather than the oldest row that merely claims a supernet containing
    # it. v1 does not fail over between two site servers equally specific about
    # one segment (spec §6.3) -- that needs a health notion the registry has not
    # got -- so that residual tie is broken by `created_at, id`.
    return candidates[0].id


async def start_provision_run(
    session: AsyncSession,
    object_store: ObjectStore,
    vault: VaultService,
    *,
    org_id: UUID,
    profile_id: UUID,
    target_ref: str | None,
    now: datetime,
    actor_id: UUID,
    actor_type: Literal["user", "agent", "system"] = "user",
    wake_on_lan: ProvisionWakeRequest | None = None,
    job_engine: JobEngine | None = None,
) -> tuple[ProvisionRun, dict[str, Any] | None]:
    """Start a provisioning run for `profile_id`: issue a single-use
    enrollment token, render+store the answer file (skipped for macOS), build
    and sign the `provision.prepare` envelope, and audit both the prepare
    intent and the run start.

    EVERY load (`get_profile`, `get_image`) carries an explicit `org_id`
    predicate (invariant #5) — a profile/image id from another org must
    never be reachable through this path. A missing/disabled profile or a
    profile whose image is missing raises `ProvisioningValidationError`
    (fail-closed) rather than dispatching against a dangling reference.
    """
    wake_plan = (
        await _resolve_wake(session, org_id, wake_on_lan, actor_id=actor_id)
        if wake_on_lan is not None
        else None
    )

    profile = await get_profile(session, org_id, profile_id)
    if profile is None or not profile.enabled:
        raise ProvisioningValidationError(
            tr("profile {profile_id} not found or disabled", profile_id=profile_id)
        )

    image = await get_image(session, org_id, profile.image_id)
    if image is None:
        raise ProvisioningValidationError(
            tr("image {image_id} not found", image_id=profile.image_id)
        )

    run = ProvisionRun(
        org_id=org_id,
        profile_id=profile_id,
        target_ref=target_ref,
        state="pending",
        actor_type=actor_type,
        requested_by=actor_id,
        started_at=now,
    )
    session.add(run)
    await session.flush()  # populate run.id before it's used as the token name / answer key

    issued = await create_token(
        session,
        org_id=org_id,
        created_by=actor_id,
        name=f"provision:{run.id}",
        expires_in_hours=24,
        max_uses=1,
    )
    run.enrollment_token_id = issued.id

    if image.os_family in _ANSWER_FILE_OS_FAMILIES:
        answer_bytes = await render_answer_file(
            session,
            vault,
            org_id=org_id,
            profile=profile,
            image=image,
            token=issued.token,
            now=now,
        )
        run.answer_ref = await object_store.put_provision_object(
            org_id, f"answers/{run.id}", answer_bytes
        )
    else:
        # macOS (`enrollment`-kind image): ADE/MDM enrollment binding only —
        # no answer file is rendered or stored.
        run.answer_ref = None

    # THE `provision.prepare` ENVELOPE IS NO LONGER BUILT OR SIGNED HERE.
    #
    # It used to be: `ProvisionPrepareEnvelope(...)` followed by
    # `await signer.sign(canonical_bytes(envelope.model_dump()))`, whose result
    # was NOT ASSIGNED, not persisted on the run, not returned and not
    # published, because no consumer existed. The discipline was real -- no
    # unsigned path was ever constructed -- but nothing consumed the output, and
    # `tests/test_provisioning_dispatch.py`'s apparently-covering test asserted
    # only audit metadata, so DELETING THE LINE MADE NOTHING GO RED. A
    # discipline no test can see is a comment.
    #
    # `docs/modules/site-server/spec.md` §8.2 wants it deleted rather than
    # preserved: under §0.1 `provision.prepare` becomes an ORDINARY registered
    # job type signed by the kernel, which the spec calls "a DELETION of a
    # parallel signing path". §8.2 schedules that with the PXE slice, but PXE is
    # gated on an invariant amendment that may never be accepted -- so
    # scheduling it there would leave an unguarded second signing path in
    # shipped code indefinitely.
    #
    # `tests/test_signing_boundary.py` now asserts, over every file under
    # `backend/app`, that `app/jobs/engine.py` is the ONLY awaited signer call.
    # `ProvisionPrepareEnvelope` itself is untouched: it stays in the exported
    # wire schema, because removing a field from a published contract is a
    # compatibility decision and not part of this fix.

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action="provisioning.prepare",
        target_type="provision_run",
        target_id=str(run.id),
        metadata={
            "provision_run_id": str(run.id),
            "os_family": image.os_family,
            "image_ref": image.artifact_ref,
            "answer_ref": run.answer_ref,
            "target_ref": target_ref,
        },
    )

    run.state = "awaiting_enrollment"

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action="provisioning.run_started",
        target_type="provision_run",
        target_id=str(run.id),
        metadata={
            "profile_id": str(profile_id),
            "image_id": str(image.id),
            "target_ref": target_ref,
        },
    )

    wake_envelope: dict[str, Any] | None = None
    if wake_plan is not None:
        assert wake_on_lan is not None
        if job_engine is None:
            raise ProvisioningValidationError(
                tr(
                    "wake_on_lan was requested but no job engine was supplied to "
                    "start_provision_run; a wake is a signed job like any other"
                )
            )
        _job, wake_envelope = await wake_via_site_server(
            session,
            job_engine,
            org_id=org_id,
            site_server_id=wake_plan,
            target_mac=wake_on_lan.target_mac,
            segment=wake_on_lan.segment,
            actor_id=actor_id,
        )

    await session.flush()
    return run, wake_envelope
