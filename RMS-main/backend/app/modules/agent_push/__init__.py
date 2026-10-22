"""Agent push install (docs/modules/agent-distribution/spec-push-install.md),
under `CLAUDE.md` invariant 2a (approved by the operator 2026-08-26).

**CYCLE B1 REFUSED EVERYTHING AND DISPATCHED NOTHING. CYCLE B2 DISPATCHES,
UNDER SIGNATURE, THROUGH THIS MODULE'S OWN ROUTE AND NO OTHER.** Spec §14 split
Part B into two cycles that "must not be merged": B1 built the two authorization
tables, the dedicated credential type, the two permission codes, the batch and
refusal ladder, both kernel gate entries, the audit events and the console
screens, and left the job types RESERVED. B2 registers the two job types
(`jobtypes.py`), decrypts the credential at dispatch and signs the whole
instruction (`dispatch.py`), redacts and scrubs it (`jobtypes.py`), pins and
confirms the host key (`host_key.py`), and ships the Go capability
(`agent/internal/capabilities/pushinstall`).

**THE SIGNING PATH IS ALLOWED HERE NOW; THE UNSIGNED CONNECTION PATH NEVER IS.**
`tests/test_agent_push_dispatches_nothing.py` was reshaped, not deleted: it
still asserts from the AST that NOTHING in this package opens a socket, an SSH
session or a WinRM session -- the control plane never connects to a target
(invariant #2, 2a clause (a)); that connection is the agent's, always. What it
now permits, and only in `dispatch.py` and `jobtypes.py`, is the signing and
point-of-use decryption that B2 requires. Every other file in the package stays
as pure as it was in B1.
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    """Mount this module's API routes and register its two job types.

    `register_job_types()` is called here (API process) and from the worker
    (`app/worker.py::register_worker_job_types`) and the gateway
    (`app/gateway/main.py`), each an idempotent dict assignment, on the same
    precedent as `site_server.register`. Registration puts the types in the
    live registry; it does NOT open the generic-dispatch or approvable-action
    door -- both kernel gates still refuse them by name (clause (e)).
    """
    from app.modules.agent_push.api import router
    from app.modules.agent_push.jobtypes import register_job_types

    register_job_types()
    app.include_router(router, prefix="/api/v1")
