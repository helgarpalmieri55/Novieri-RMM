"""Remote-control job types.

* ``remote.session.open``: the frozen guacd path (docs/modules/remote-control/spec.md).
  No agent implements it; kept so rows and schemas that reference it stay valid.
* ``remote.screen.open``: share the endpoint's own console over the rsp/1 relay
  (docs/modules/remote-control/spec-screen-share.md §2.3, §6). A new type rather
  than a new ``protocol`` value, so an old agent refuses it as ``unknown_type``.

The screen result handler only matters when the relay never ended the session
itself (the agent refused, never reached the relay, or found no user): it
writes the outcome onto a session that is still open.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.jobs.registry import JobType, register

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import Job

logger = logging.getLogger(__name__)


class RemoteSessionOpenParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    protocol: Literal["rdp", "vnc", "ssh"]
    local_port: int
    relay_url: str
    relay_token: str
    consent_mode: Literal["attended", "unattended"]
    consent_prompt: dict[str, str] | None = None


class ScreenConsent(BaseModel):
    """What the endpoint user is shown before they allow a session."""

    model_config = ConfigDict(extra="forbid")
    technician: str
    org: str
    reason: str
    timeout_s: int = Field(ge=15, le=120)


class RemoteScreenOpenParams(BaseModel):
    """Signed params of ``remote.screen.open``.

    ``relay_token`` is a one-shot secret valid until ``connect_by``; the agent
    presents it in the AUTH frame on ``relay_url``. ``desktop`` is ``user`` in
    phase 1; phase 2 adds ``secure``. New members must be optional with a
    default: the agent ignores members it does not know.
    """

    model_config = ConfigDict(extra="forbid")
    session_id: str
    relay_url: str
    relay_token: str
    connect_by: str
    consent_mode: Literal["attended", "unattended"]
    consent: ScreenConsent
    max_duration_s: int = Field(ge=60, le=86400)
    allow_input: bool = True
    record: bool = True
    desktop: Literal["user"] = "user"


ScreenOutcome = Literal[
    "ended",
    "denied",
    "consent_timeout",
    "no_user_session",
    "capture_unavailable",
    "permission_missing",
    "unsupported_display",
    "relay_refused",
    "expired",
    "error",
]


class RemoteScreenOpenResult(BaseModel):
    """What the agent reports when a screen session is over, for any reason.

    The agent reports the job ``succeeded`` for every outcome it can name;
    ``failed`` means the handler could not run at all. ``outcome`` is read
    leniently by the server: an unknown value is stored as ``error``.
    """

    outcome: ScreenOutcome
    duration_s: int = 0
    detail: str = ""
    platform_state: dict[str, Any] | None = None


# outcome -> remote_sessions.end_reason (String(20)).
_END_REASON = {
    "ended": "completed",
    "denied": "denied_consent",
    "consent_timeout": "consent_timeout",
    "no_user_session": "no_user_session",
    "capture_unavailable": "capture_unavailable",
    "permission_missing": "permission_missing",
    "unsupported_display": "unsupported_display",
    "relay_refused": "relay_refused",
    "expired": "expired",
    "error": "error",
}


def end_reason_for_outcome(outcome: object) -> str:
    reason = _END_REASON.get(outcome) if isinstance(outcome, str) else None
    if reason is None:
        logger.warning("remote.screen.open: unknown outcome %r stored as error", outcome)
        return "error"
    return reason


async def handle_screen_result(session: AsyncSession, job: Job, result: dict[str, Any]) -> None:
    from app.modules.remote.service import end_session_from_result

    session_id = job.params.get("session_id") if isinstance(job.params, dict) else None
    if not isinstance(session_id, str):
        return
    await end_session_from_result(
        session,
        session_id=session_id,
        device_id=job.target_id,
        reason=end_reason_for_outcome(result.get("outcome")),
    )


def _redact_screen_params(params: dict[str, Any]) -> dict[str, Any]:
    # The relay token is a bearer secret until connect_by: never shown by the
    # jobs API, and not kept once the job is over.
    return {k: ("<redacted>" if k == "relay_token" else v) for k, v in params.items()}


def register_job_type() -> None:
    register(JobType("remote.session.open", RemoteSessionOpenParams))
    register(
        JobType(
            "remote.screen.open",
            RemoteScreenOpenParams,
            result_handler=handle_screen_result,
            # Not cancellable until an agent capability exists that honours
            # ctx (phase 1); the flag is a claim about the endpoint's code.
            params_redactor=_redact_screen_params,
            terminal_params_scrubber=_redact_screen_params,
        )
    )
