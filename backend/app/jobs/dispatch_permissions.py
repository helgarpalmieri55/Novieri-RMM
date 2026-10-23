"""Which permission the OWNING MODULE'S OWN dispatch route requires, per job type.

docs/modules/ai-automation.md §0.11, §9.2, §10.2.

WHY THIS EXISTS. Confirming an AI-proposed action requires two grants, and the
second is the load-bearing one: the confirming human must hold the permission
they would need to run that job type BY HAND, through the module's own route.
Without it a technician holding only `ai.act.confirm` could fire `patch.apply`
through the AI layer while being refused it on `POST /api/v1/patch/apply` --
privilege escalation, and invariant #6 violated.

That mapping already existed. It existed as ENGLISH PROSE, inside
`app/jobs/direct_dispatch.py::MODULE_GOVERNED_REASONS`, where each refusal
message ends by naming the correct route and its permission ("Use POST
/api/v1/patch/apply (patch.apply)"). Prose is unreadable by code, so a check
that needs it has to either re-type it -- a second truth that can disagree with
the first -- or go without. This module is that prose as data, and
`tests/test_dispatch_permissions.py` asserts the two agree, entry by entry, so
they cannot drift.

THIS IS THE THIRD ALLOWLIST OF ITS KIND, and it answers a different question
from the other two.

  * `app.core.approved_actions.APPROVABLE_JOB_TYPES` -- may a MACHINE dispatch
    this at all (an approved action fired by the health sweep)?
  * `app.jobs.direct_dispatch.DIRECTLY_DISPATCHABLE_JOB_TYPES` -- may a human
    dispatch this THROUGH THE KERNEL ROUTE, bypassing the module's own record?
  * this map -- WHICH PERMISSION does the module's own route require?

All three are DENY BY DEFAULT and all three assert a PARTITION against the live
registry, so a module registering a new job type breaks a test rather than
picking a side by accident. `dispatch_permission_for` returns `None` for a job
type in neither map, and `None` MEANS REFUSE at every call site -- an
unclassified job type is not confirmable through the AI layer.

WHAT `NO_HUMAN_DISPATCH_PATH` MEANS, precisely. Not "dangerous" and not
"forbidden": it means there is NO ROUTE anywhere in this platform that a human
can call to dispatch this job type, so there is no permission to name. All four
entries are minted by a state machine, a reconciliation sweep, or another
route's own bookkeeping, from state the kernel itself minted. A job type here
can never be confirmed through the AI layer, because the question "does this
human hold what the route requires" has no route to ask about.

TWO CALL SITES, AND THEY ASK THE SAME QUESTION.
`app/core/ai/act.py::confirm_action` was the first; the second is
`app/modules/health/actions.py::_require_dispatch_grant`, feeding
`dispatch_manual_remediation` and `approve_action`. §0.11 recorded that health's
console path was gated on `health.remediate` alone, so its holder could fire a
`patch.apply` remediation without holding `patch.apply`; that gap is now closed
(endpoint-health spec §20, `tests/test_health_remediation_authz.py`). It was left
open when this module was written for a reason that has been honoured rather than
overtaken: the fix turns a currently-succeeding call into a 403 and edits
health's own test suite, so it belonged in health's own cycle where somebody
reviews it as a security change, not inside a sixteen-task refactor of another
layer. This docstring previously read "NOTHING CONSUMES THIS MAP YET"; it does
now, twice, and a third module adding a dispatch path of its own should reach for
this rather than re-type it.
"""

from collections.abc import Mapping

from app.core.i18n import N_

__all__ = [
    "DISPATCH_PERMISSIONS",
    "NO_HUMAN_DISPATCH_PATH",
    "dispatch_permission_for",
]

# job type -> the permission code the owning module's own dispatch route is
# gated on. EVERY VALUE HERE WAS READ OUT OF `MODULE_GOVERNED_REASONS`' OWN
# MESSAGE or, for the three kernel-route types, out of the route itself; none
# was typed from memory. `tests/test_dispatch_permissions.py` re-parses those
# messages and compares, and separately asserts that each value both exists in
# `app.core.rbac.PERMISSIONS` and actually gates a live route -- a plausible
# code that gates nothing is the failure this map would otherwise hide.
DISPATCH_PERMISSIONS: Mapping[str, str] = {
    # `DIRECTLY_DISPATCHABLE_JOB_TYPES`: no module governs these, so the kernel
    # route IS the owning route and `jobs.dispatch` IS the permission.
    "hello": "jobs.dispatch",
    "inventory.collect": "jobs.dispatch",
    "logs.ship": "jobs.dispatch",
    # Module-governed. The route named in each `MODULE_GOVERNED_REASONS`
    # message is the one whose gate this is.
    "net.sweep": "ipam.sweep.run",
    "site.wake": "site.wake",
    "health.check.now": "health.alert.manage",
    "patch.scan": "patch.scan",
    "patch.apply": "patch.apply",
    "app.deploy": "deploy.execute",
    "script.run": "deploy.script.run",
    "backup.run": "backup.run",
    "backup.restore": "backup.restore",
    "provision.enroll": "provisioning.run",
    "remote.session.open": "remote.control",
    "remote.screen.open": "remote.control",
    # Agent push install (spec-push-install.md §8). Both push types have a human
    # dispatch route -- `POST /api/v1/agent-push/requests`, gated on
    # `agent.push.run` -- so this map names it, exactly as it names every other
    # module route. This does NOT make a push AI-confirmable: `CLAUDE.md`
    # invariant 2a clause (e) forbids any automated actor, and
    # `NON_APPROVABLE_REASONS` refuses the type upstream, so an AI can never
    # PROPOSE one and `confirm_action` is never reached for it. What this entry
    # answers is the question this map exists for -- which permission the owning
    # route requires -- and the answer is the same for both, because the probe
    # and the install are dispatched through the one route.
    "agent.push_install": "agent.push.run",
    "agent.push_probe": "agent.push.run",
    # Self-update rollouts (spec-self-update.md §8.5). Named for the same
    # reason as the push types; NON_APPROVABLE_REASONS still refuses it
    # upstream of every automated path.
    "agent.update": "agent.update.rollout",
    # Reject (install-link spec §1.5). Named for the same reason as the push
    # types; NON_APPROVABLE_REASONS refuses it upstream of every automated path.
    "agent.uninstall": "devices.review",
}

# job type -> why no human dispatch route exists for it, so a refusal says
# something an operator can act on rather than "not allowed".
NO_HUMAN_DISPATCH_PATH: Mapping[str, str] = {
    "health.policy.apply": N_(
        "owned by health's own policy push and reconciliation; there is no route for it by "
        "design -- assign a health profile (health.admin) and the push is queued from the "
        "effective policy. `checks: []` is a legal policy meaning 'run nothing', so an "
        "operator-authored one would be the platform's own monitoring kill switch"
    ),
    "job.cancel": N_(
        "minted by the kernel's cancel route from a job the platform already signed, never "
        "by a human naming a job id. The authorization for stopping a job is the TARGET "
        "job type's own dispatch permission, checked on that route before this one is "
        "created -- so a `job.cancel` an operator could dispatch directly would be a cancel "
        "that skipped the only check that governs it"
    ),
    "agent.cert.renew": N_(
        "one leg of automatic certificate rotation, dispatched by the kernel's own state "
        "machine at connect, which holds a device to one rotation at a time and pairs the CSR "
        "it produces with the signing leg. There is no route for it by design: rotation needs "
        "no operator action and retries on every connect"
    ),
    "patch.reboot": N_(
        "dispatched only by the patch module's restart sweep, for a device that owes a "
        "restart -- after patching, or booked by an operator from the device page -- and only "
        "inside its maintenance window. There is no route for it by design: an "
        "operator-chosen restart time would bypass the window"
    ),
    "agent.cert.install": N_(
        "REPLACES AN ENDPOINT'S mTLS IDENTITY, and its params carry a certificate the kernel "
        "minted from that device's own CSR. There is no route for it by design: a route would "
        "take a certificate PEM straight from a request body -- any certificate, for any "
        "endpoint -- which is the exact attack automatic renewal closes"
    ),
}


def dispatch_permission_for(job_type: str) -> str | None:
    """The permission `job_type`'s own dispatch route requires, or `None`.

    DENY BY DEFAULT, AND `None` IS A REFUSAL AT EVERY CALL SITE. `None` is
    returned both for a job type that has no human dispatch path at all and for
    one nobody has classified. A caller that treats `None` as "no permission
    needed" has inverted the map: the two cases are "there is no route, so
    there is no grant to check" and "nobody has said which grant applies", and
    neither is a licence to dispatch.
    """
    return DISPATCH_PERMISSIONS.get(job_type)
