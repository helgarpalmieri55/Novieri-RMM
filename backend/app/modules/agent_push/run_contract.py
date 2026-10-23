"""The seam between the pacer and the one file that signs
(docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md §1.4).

`runs.py` decides WHICH confirmed target goes next and records what came of
it. `dispatch.py` is the only file in this package that may sign a job or
decrypt a credential (`tests/test_agent_push_dispatches_nothing.py`). So the
pacer never holds a signer or a vault: it is handed a `RunTargetDispatcher`
and gets back a `RunTargetDispatch`. Both are defined here, in a file that
imports neither side, so `dispatch.py` and `runs.py` never import each other
at module load.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.agent_push.preflight import TargetDecision

__all__ = ["Envelope", "RunTargetDispatch", "RunTargetDispatchError", "RunTargetDispatcher"]

#: (site server device id, signed envelope), published only after the
#: transaction that created the job has committed.
Envelope = tuple[UUID, dict[str, Any]]


class RunTargetDispatchError(Exception):
    """Dispatching one target failed AFTER its credential was checked out of
    the vault, for a reason that is not a named refusal (a signer outage, the
    object store). The pacer keeps what the pass already dispatched and audits
    this attempt as a checkout (review item 9).

    IT CARRIES THE CAUSE'S TYPE, NEVER ITS MESSAGE. A cause raised while the
    signed parameters are built may quote them, and they hold the secret."""

    def __init__(self, cause: BaseException) -> None:
        self.cause_type = type(cause).__name__
        super().__init__(  # i18n-exempt: the pacer audits cause_type; no response carries it
            f"push run dispatch failed after the vault checkout ({self.cause_type})"
        )


@dataclass(frozen=True)
class RunTargetDispatch:
    """What dispatching ONE confirmed target came to: refused, with every
    named reason, or one signed job. Never both."""

    refusals: tuple[str, ...] = ()
    extras: dict[str, str] = field(default_factory=dict)
    job_id: UUID | None = None
    envelope: dict[str, Any] | None = None
    #: WHICH stored version of the secret the job carries: the dispatcher's
    #: `credential_fingerprint` of the credential it decrypted, set only on a
    #: dispatch. Opaque; the pacer compares it and never shows it.
    credential_fingerprint: str | None = None

    @property
    def dispatched(self) -> bool:
        return self.job_id is not None and self.envelope is not None


class RunTargetDispatcher(Protocol):
    """What the pacer is handed: `dispatch.RunDispatcher`.

    `credential_fingerprint` names which stored version of a credential's
    secret is current, without this side ever reading the stored secret. The
    lockout guard counts a proven sign-in only for the version it proved
    (spec §1.4a), so a secret changed mid-run is proven again, one first."""

    def credential_fingerprint(self, credential: Any) -> str: ...

    async def dispatch_target(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        run_id: UUID,
        on_behalf_of: UUID,
        credential: Any,
        decision: TargetDecision,
    ) -> RunTargetDispatch: ...
