"""Which job types a device waiting for review may receive.

Install-link spec 2026-09-28 §1.5. A machine that joined through the install
link is VISIBLE and HELD BACK: it reports inventory and health, keeps its
certificate alive, and can be told to uninstall itself -- and nothing else. No
software, script, patch, backup, remote session, sweep, push or policy reaches
it until a person approves it.

THE GATE IS IN `JobEngine.create`, the one place every job is born, so no module
can forget it -- and a job type registered tomorrow is refused by default,
because the lists below are ALLOW-lists. `tests/test_review_gate.py` enumerates
the live registry to prove it.
"""

from uuid import UUID

ALLOWED_WHILE_PENDING: frozenset[str] = frozenset(
    {
        # Q4: the machine is visible.
        "inventory.collect",
        # Q4: health is reported. Monitoring only -- remediation is a separate
        # job type (an approved action's) and is refused.
        "health.policy.apply",
        "health.check.now",
        # The certificate must stay alive until someone decides.
        "agent.cert.renew",
        "agent.cert.install",
        # Reject.
        "agent.uninstall",
        # Stopping one of the jobs above. `job.cancel` is itself a signed job
        # born in `JobEngine.create`, and cancelling only ever REDUCES what
        # runs on the machine -- refusing it would leave an allowed job on a
        # pending machine impossible to stop (review fix M5, operator-confirmed
        # 2026-09-28).
        "job.cancel",
    }
)

#: Once rejected, the only thing left to say to the machine is "uninstall".
ALLOWED_WHILE_REJECTED: frozenset[str] = frozenset({"agent.uninstall"})

WAITING_FOR_REVIEW = "waiting for review"


class DeviceAwaitingReview(Exception):
    """A job this device may not receive until a person approves it."""

    def __init__(self, device_id: UUID, job_type: str, review_state: str) -> None:
        self.device_id = device_id
        self.job_type = job_type
        self.review_state = review_state
        super().__init__(  # i18n-exempt: internal text; main.py translates the 409 body
            f"{WAITING_FOR_REVIEW}: device {device_id} is {review_state} and may not "
            f"receive {job_type}"
        )


def refused(review_state: str | None, job_type: str) -> bool:
    """`None` is "no such device in this org", which is not this gate's
    question: the engine has always created a job for whatever id it is given,
    and the callers that care check existence themselves."""
    if review_state is None or review_state == "approved":
        return False
    if review_state == "pending":
        return job_type not in ALLOWED_WHILE_PENDING
    return job_type not in ALLOWED_WHILE_REJECTED
