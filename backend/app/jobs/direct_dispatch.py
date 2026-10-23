"""Which job types the KERNEL's generic dispatch route may create.

`POST /api/v1/jobs` is the one route in this platform that takes a job TYPE and
a params BLOB from an operator and hands both to `JobEngine.create`. It is gated
on `jobs.dispatch`, which the seeded `technician` role holds
(`migrations/versions/0003_rbac_seed.py`). Every business module also registers
its own job types into `app.jobs.registry`, which means that without this
allowlist the kernel route can create ANY module's job with the operator's own
params -- reaching the agent through the module's capability handler while
skipping everything the module does around its own dispatch.

WHAT "EVERYTHING THE MODULE DOES" MEANS, concretely, because the bypass is not a
missing permission -- it is a missing RECORD. `POST /api/v1/ipam/subnets/{id}/sweep`
does not merely check `ipam.sweep.run`; it intersects the requested targets with
the org's `ipam_scan_authorization` rows, writes an `ipam_sweep_run` the result
is later checked against, emits `ipam.sweep_dispatched`, and clamps the TTL. The
kernel route reproduces none of that, so a `net.sweep` created here puts packets
on a customer's wire with no authorization row, no run row, no audit event and
no TTL clamp -- and the result handler has no run to check the returned scope
against. The same shape holds for every other entry in
`MODULE_GOVERNED_REASONS`: the module's route is where the authorization, the
record and the bounds live, and the permission is only the first of them.

DENY BY DEFAULT, and this is the SECOND allowlist of its kind. The first,
`app.core.approved_actions.APPROVABLE_JOB_TYPES`, bounds what a MACHINE may
dispatch (an approved action fired by the health sweep). This one bounds what a
HUMAN holding `jobs.dispatch` may dispatch THROUGH THE KERNEL ROUTE. The
audiences differ and so do the sets -- `patch.apply` and `script.run` are
approvable (a pinned script body, a reviewed registry row) yet are NOT directly
dispatchable (the kernel route would take an arbitrary body and an arbitrary
reboot mode with no `script_run`/`patch_run` record behind it) -- but the
DIRECTION is identical: a job type nobody has classified is refused, so a module
registering a new one gets no kernel-route dispatch until somebody says so in a
reviewed change.

THE TEST ASSERTS A PARTITION, not membership. `tests/test_direct_dispatch.py`
requires `DIRECTLY_DISPATCHABLE_JOB_TYPES | MODULE_GOVERNED_REASONS` to equal
the live registry exactly -- nothing registered missing from both, nothing named
here unregistered, nothing in both. A membership-only test would let the next
registered job type pick a side by accident, which is precisely how the first
version of this hole was born.

WHAT A TECHNICIAN LOSES, and why that is right. The seeded `technician` role
already holds `patch.scan`, `patch.apply`, `deploy.script.run`, `deploy.execute`,
`backup.run`, `backup.restore`, `provisioning.run`, `remote.control`,
`ipam.sweep.run` and `health.alert.manage`. Every capability they could reach
through the kernel route, they keep -- through the owning module's route, which
records what they did. The single capability that genuinely goes away is
`health.policy.apply`: it is admin-tier (`health.admin`), a technician was never
granted it, and it is the platform's own monitoring kill switch (`checks: []` is
a legal policy meaning "run nothing"). Reaching it through `jobs.dispatch` was
not a capability, it was the bypass.

THE REFUSAL IS A 403, NOT A 422. It is an authorization decision about this
route -- the caller holds `jobs.dispatch` and the job type is perfectly valid --
so it must not be confused with "your params are malformed". An unregistered job
type stays a 422; that is a different fact and an operator acts on it
differently.
"""

from app.core.i18n import N_, tr

__all__ = [
    "DIRECTLY_DISPATCHABLE_JOB_TYPES",
    "MODULE_GOVERNED_REASONS",
    "RESERVED_JOB_TYPES",
    "module_governed_reason",
]

# Job types this repository REFUSES BEFORE IT REGISTERS THEM.
#
# WHY A JOB TYPE WOULD BE REFUSED BEFORE IT EXISTS. `agent.push_install` and
# `agent.push_probe` are cycle B2 of
# `docs/modules/agent-distribution/spec-push-install.md`; cycle B1 -- which is
# what is in the tree -- builds the authorization tables, the credential
# scoping, the permission codes and the refusal ladder, and deliberately
# dispatches nothing (spec §14). Spec §2.3 requires both kernel gates to name
# these two types anyway, "because it is cheaper to close before the job type
# exists than after", and names the precedent: the same structural hole was
# live for `net.sweep`, and packets went out with zero `ipam_scan_authorization`
# rows in the org. Deny-by-default already refuses an unregistered type; what
# the named entries add is a refusal an operator can ACT on, and a reviewed
# record that the decision was made rather than deferred.
#
# THIS SET IS A FORCING FUNCTION, NOT A HOLE. Both partition tests --
# `tests/test_direct_dispatch.py` and `tests/test_approved_actions_destructive.py`
# -- assert that the names decided-but-not-registered are EXACTLY this set. So:
# a typo in either map still fails (the typo is not in here); a third
# unregistered name still fails; and on the day B2 registers these two, both
# tests go red until this set is emptied in the same commit. The partition is
# not weakened, it is stated in one more place.
#
# EMPTYING THIS SET IS PART OF CYCLE B2, and the tests said so. It is now empty:
# cycle B2 registers `agent.push_install` and `agent.push_probe`
# (`app/modules/agent_push/jobtypes.py`), so they are no longer
# decided-but-not-registered. Both remain named in `MODULE_GOVERNED_REASONS`
# below and in `approved_actions.NON_APPROVABLE_REASONS`, so the human
# generic-dispatch route and every machine actor still refuse them by name --
# the module's own `POST /api/v1/agent-push/requests` is their only dispatch
# path (spec §2.3, clause (e)). The partition tests (`test_direct_dispatch.py`,
# `test_approved_actions_destructive.py`) assert `decided - registered ==
# RESERVED_JOB_TYPES`, which now means `decided - registered` is empty: every
# named-but-refused type is registered, and nothing is reserved. A future
# reservation goes back in here.
RESERVED_JOB_TYPES: frozenset[str] = frozenset()

# Job types NO module governs: the kernel route is their only dispatch path, and
# creating one here is identical to creating it anywhere else. Each takes either
# no params at all or a single inert string, and each is bounded by the device
# the job is signed for.
#
# `logs.ship` and `inventory.collect` are also dispatched on a schedule by the
# worker (`security_logs/schedule.py`, `inventory/schedule.py`), but that is a
# system actor on a fixed cadence; this route stays the only way a person can
# ask for one now, so refusing them here would take that away.
DIRECTLY_DISPATCHABLE_JOB_TYPES: frozenset[str] = frozenset(
    {
        "hello",
        "inventory.collect",
        "logs.ship",
    }
)

# Why each module-governed job type is refused HERE, and the route that owns it.
# The route is named in the message on purpose: a refusal an operator cannot act
# on is a refusal they will look for a way around, and the way around this one
# is the correct path.
MODULE_GOVERNED_REASONS: dict[str, str] = {
    "site.wake": N_(
        "site.wake is governed by the site-server registry: the broadcast ADDRESS is computed "
        "server-side from the operator-declared `site_server.segments` entry containing the "
        "target, so the endpoint never decides what it may touch, and the act is gated on "
        "`site.wake` separately from `site.manage`. Through the kernel route an arbitrary "
        "broadcast address in `params` would put a datagram on any wire in reach of any promoted "
        "device -- with no registry row consulted, no `site.wake_dispatched` audit event, and the "
        "300s TTL replaced by the kernel default, so a wake could fire an hour after the window "
        "it belonged to. Use POST /api/v1/site-servers/{site_server_id}/wake (site.wake)"
    ),
    "net.sweep": N_(
        "net.sweep is governed by ipam's scan authorization: the targets are intersected with "
        "the org's active `ipam_scan_authorization` rows (deny by default), an `ipam_sweep_run` "
        "row is written for the result to be checked against, `ipam.sweep_dispatched` is "
        "audited and the TTL is clamped. None of that happens here, so a sweep dispatched "
        "through the kernel route puts packets on somebody else's wire unauthorized and "
        "unrecorded. Use POST /api/v1/ipam/subnets/{subnet_id}/sweep (ipam.sweep.run)"
    ),
    "health.policy.apply": N_(
        "health.policy.apply delivers a device's WHOLE monitoring policy and is owned by "
        "health's policy push and reconciliation. `checks: []` is a legal policy meaning 'run "
        "nothing', so an operator-authored one is the platform's own monitoring kill switch, "
        "and `policy_version` is reconciliation state that an operator-chosen value forges into "
        "'this device has converged'. There is no route for it by design: assign a health "
        "profile (health.admin) and the push is queued from the effective policy"
    ),
    "health.check.now": N_(
        "health.check.now is dispatched against a device's EFFECTIVE health policy and is "
        "rate-limited per device by health's own route, which also refuses to pile a second "
        "one on an in-flight job. Use POST /api/v1/devices/{device_id}/health/check-now "
        "(health.alert.manage)"
    ),
    "patch.scan": N_(
        "patch.scan is owned by the patch module, which resolves the device set, audits "
        "`patch.scan_dispatched` per device and is the same path the scheduled scan sweep "
        "takes. Use POST /api/v1/patch/scan (patch.scan)"
    ),
    "patch.apply": N_(
        "patch.apply installs software and may reboot. The patch module decides it against the "
        "org's APPROVAL state for each KB and the device's maintenance WINDOW, and records the "
        "run; the kernel route takes an operator's own `kbs` and `reboot` with neither. Use "
        "POST /api/v1/patch/apply (patch.apply)"
    ),
    "patch.reboot": N_(
        "patch.reboot restarts an endpoint. The patch module dispatches it only for a device "
        "that owes a restart -- after patching, or booked by an operator from the device "
        "page (patch.apply) -- only inside its maintenance window, and tracks it to "
        "completion; there is no route for it by design"
    ),
    "app.deploy": N_(
        "app.deploy installs a package on an endpoint. The deploy module builds the params from "
        "the stored `package` row -- artifact ref, hash, install arguments -- and records a "
        "`deployment_target`; the kernel route would take an operator-authored artifact "
        "reference instead. Use POST /api/v1/deploy/deployments/{id}/execute (deploy.execute)"
    ),
    "script.run": N_(
        "script.run executes arbitrary code as SYSTEM/root. The deploy module builds the params "
        "from the PINNED `scripts` row and writes a `script_run` plus a `script.dispatched` "
        "audit event, so what ran is identifiable afterwards; the kernel route would take an "
        "interpreter and a body straight from the request with no script row, no run record and "
        "no audit trail naming what was executed. Use POST /api/v1/deploy/scripts/{id}/run "
        "(deploy.script.run)"
    ),
    "backup.run": N_(
        "backup.run's params carry `repo_key`, the repository encryption key, which the backup "
        "module decrypts from the vault at point of use (invariant #3) along with the "
        "repository and source set from the device's policy. A kernel-route caller would have "
        "to put a key in a request body. Use POST /api/v1/backup/devices/{device_id}/run "
        "(backup.run)"
    ),
    "backup.restore": N_(
        "backup.restore's params carry `repo_key` and a `manifest_ref` naming one snapshot -- "
        "state the backup module mints per restore from the recovery point, having checked it "
        "belongs to this org and this device. Use POST /api/v1/backup/points/{point_id}/restore "
        "(backup.restore)"
    ),
    "provision.enroll": N_(
        "provision.enroll re-images a machine. Its `provision_run_id` must resolve to a "
        "provisioning run that authorized doing so, and the module mints that row from the "
        "profile and image; a kernel-route caller can only name a stale one or invent one. Use "
        "POST /api/v1/provisioning/profiles/{profile_id}/run (provisioning.run)"
    ),
    "agent.cert.renew": N_(
        "agent.cert.renew is one leg of automatic certificate rotation and is dispatched by the "
        "kernel's own state machine at connect (docs/modules/agent/cert-renewal.md §3.1), which "
        "holds it to one rotation per device at a time and pairs it with the CSR-signing leg. An "
        "operator-created one would produce a CSR the state machine never asked for and can no "
        "longer distinguish from the one it is waiting on. There is no route for it by design: "
        "rotation needs no operator action, and a device whose window has passed is re-deployed"
    ),
    "agent.cert.install": N_(
        "agent.cert.install REPLACES AN ENDPOINT'S mTLS IDENTITY. Its params carry a certificate "
        "that the kernel minted from a CSR that device produced, for that device's own device_id "
        "and org_id, from the internal CA. An operator-authored one would take a certificate "
        "PEM straight from a request body -- any certificate, for any endpoint -- which is the "
        "exact attack automatic renewal otherwise closes. There is no route for it by design"
    ),
    "job.cancel": N_(
        "job.cancel is minted by the kernel's own cancel route from a job this platform "
        "already signed: its single parameter is the id of that job, and the whole point of "
        "the type is that the target is the SERVER'S OWN statement rather than anything a "
        "caller typed. Through the kernel route an operator could name any job id at all, "
        "including one on another device, with no cancel request recorded against the target, "
        "no `jobs.cancel_requested` audit event and no permission check against the TARGET "
        "job's type -- which is where the authorization for stopping it actually lives. "
        "There is no route for it by design: cancel a job with POST "
        "/api/v1/jobs/{job_id}/cancel and the platform mints the signed command itself"
    ),
    "agent.push_install": N_(
        "agent.push_install causes a machine NOBODY HAS ENROLLED to run this platform's own "
        "agent installer, and it is governed by four independent bounds that exist nowhere "
        "near the kernel route (`CLAUDE.md` invariant 2a): the target must be a promoted asset "
        "row in this org, an active `push_authorization` must cover its address, the chosen "
        "credential must carry a `push_credential_scope` covering it, and the originator must "
        "be a device the operator declared a site server for that machine's segment -- with no "
        "fallback originator, because the originator holds a plaintext administrative "
        "credential in process memory for the job's duration. The module's own route refuses "
        "on the near side of the signing boundary, records which bound failed, and enforces "
        "the batch ceiling; the kernel route reproduces none of that, so a push created here "
        "would present a domain-capable secret to any address in a request body. The module's "
        "route additionally requires `credentials.checkout`, checked at its service boundary. "
        "Use POST /api/v1/agent-push/requests (agent.push.run)"
    ),
    "agent.push_probe": N_(
        "agent.push_probe opens a transport to an UNENROLLED machine to learn its host key "
        "before any credential is presented, and it is bounded by the same four checks as "
        "agent.push_install for the same reason: it still puts a connection on somebody's wire, "
        "from a declared site server and nowhere else, against one address a human named. It "
        "carries no credential and its params model has no field that could hold one -- which "
        "is what makes it safe to run BEFORE the operator confirms the fingerprint, not what "
        "makes it dispatchable by hand. Like the push itself it additionally requires "
        "`credentials.checkout` at the service boundary. Use POST "
        "/api/v1/agent-push/requests (agent.push.run)"
    ),
    "agent.update": N_(
        "agent.update REPLACES THE AGENT ON AN ENDPOINT. The self-update module builds its "
        "params from the `agent_releases` row the rollout names -- the server-computed digest "
        "and size, a presigned URL and the offline release signature -- dispatches in batches "
        "only for a ring a person started, pauses on the failure threshold, and records every "
        "outcome against a rollout target. The kernel route would take an operator-typed "
        "artifact URL and digest with none of that. Use POST /api/v1/agent/update-rollouts "
        "(agent.update.rollout)"
    ),
    "agent.uninstall": N_(
        "agent.uninstall REMOVES THE AGENT FROM A COMPUTER. It is the second half of rejecting "
        "a machine that joined through the install link: the device review records who "
        "rejected it and why, holds the machine to this one job type, and revokes its "
        "certificate when the agent confirms or 24 hours later. Through the kernel route it "
        "would uninstall an approved, working agent with none of that, and nothing would "
        "revoke the certificate it leaves behind. Use POST "
        "/api/v1/devices/{device_id}/review/reject (devices.review)"
    ),
    "remote.session.open": N_(
        "remote.session.open's params carry a `relay_token` the remote module mints from the "
        "vault per session, plus a `relay_url` and a `session_id` that must resolve to a "
        "`remote_session` row carrying the end user's CONSENT decision. A remote-control "
        "session opened without that row is a session nobody consented to and nobody recorded. "
        "Use POST /api/v1/remote/sessions (remote.control)"
    ),
    "remote.screen.open": N_(
        "remote.screen.open shares and controls a person's screen. Its relay token and "
        "session_id must match a Redis registration and a remote_session row that the remote "
        "module writes with the consent mode, policy bounds and audit record. A job created "
        "without them cannot connect, and one that could would be an unrecorded session. "
        "Use POST /api/v1/remote/sessions (remote.control)"
    ),
}


def module_governed_reason(job_type: str) -> str | None:
    """Why `job_type` may not be dispatched through `POST /api/v1/jobs`, or `None`.

    DENY BY DEFAULT. A job type in neither map is refused with a generic reason
    naming the two maps -- a module's newly registered job type is not reachable
    from the kernel route until somebody decides it should be.
    """
    if job_type in DIRECTLY_DISPATCHABLE_JOB_TYPES:
        return None
    stated = MODULE_GOVERNED_REASONS.get(job_type)
    if stated is not None:
        return stated
    return tr(
        "{job_type} is not dispatchable through the generic jobs route. That route may only "
        "create job types no module governs; every other type is dispatched through its "
        "module's own endpoint, which is where the authorization, the record of what was "
        "requested and the bounds on it live. Adding one is a reviewed change to "
        "DIRECTLY_DISPATCHABLE_JOB_TYPES",
        job_type=job_type,
    )
