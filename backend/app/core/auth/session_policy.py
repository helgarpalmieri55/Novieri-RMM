"""How long a sign-in lasts: Settings → Security → Sessions (P2 design §2.4, §2.6, §3).

One row per organisation in `auth_org_settings` (Task 1), with CHECK bounds;
no row means the defaults below, which are the table's own column defaults
(`tests/test_auth_sessions.py` holds the two together). Read on EVERY request
by `app/core/auth/sessions.py::resolve`, with no cache, so a value saved in the
console applies to the next request: lowering the inactivity limit signs out
whoever has been idle longer, at once.

THE NUMBERS AND THEIR SOURCES. Staff: 30 minutes of inactivity, 12 hours at
most (design §2.4, decision S2). NIST SP 800-63B-4 §2.2.3, AAL2: "The
inactivity timeout SHOULD be no more than 1 hour" and the overall one "SHOULD
be no more than 24 hours" [S1] -- the CHECKs stop at 8 hours and 24 hours.
Contacts: 60 minutes and 7 days, up to 30 days, NIST's AAL1 maximum [S1 §2]
(design §3; used from phase 2). A remembered device: 15 days without the second
factor (decision S2b), and a session started on one lasts at most
`remembered_max_hours`.
"""

from dataclasses import dataclass, fields
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import AuthOrgSettings

CONTACT_KIND = "contact"


@dataclass(frozen=True)
class SessionPolicy:
    staff_idle_minutes: int = 30
    staff_max_hours: int = 12
    remember_device_enabled: bool = True
    remember_device_days: int = 15
    remembered_max_hours: int = 168
    contact_idle_minutes: int = 60
    contact_max_days: int = 7

    def idle_limit(self, kind: str) -> timedelta:
        """How long a session of `kind` may go unused before it stops."""
        minutes = self.contact_idle_minutes if kind == CONTACT_KIND else self.staff_idle_minutes
        return timedelta(minutes=minutes)

    def lifetime(self, kind: str, *, remembered: bool) -> timedelta:
        """A session's maximum length from sign-in. A contact's is its own;
        a technician's is longer only when it started on a remembered device."""
        if kind == CONTACT_KIND:
            return timedelta(days=self.contact_max_days)
        return timedelta(hours=self.remembered_max_hours if remembered else self.staff_max_hours)

    def device_lifetime(self) -> timedelta:
        """How long a remembered device skips the second factor."""
        return timedelta(days=self.remember_device_days)


#: The policy of an organisation that never saved one.
DEFAULT_POLICY = SessionPolicy()

#: The seven settings, in the order the console shows them.
POLICY_FIELDS: tuple[str, ...] = tuple(f.name for f in fields(SessionPolicy))


def policy_of(row: AuthOrgSettings | None) -> SessionPolicy:
    if row is None:
        return DEFAULT_POLICY
    return SessionPolicy(**{name: getattr(row, name) for name in POLICY_FIELDS})


async def read_policy(session: AsyncSession, org_id: UUID) -> SessionPolicy:
    """The organisation's policy, as `session` (pinned to `org_id`) reads it."""
    row = (
        await session.execute(select(AuthOrgSettings).where(AuthOrgSettings.org_id == org_id))
    ).scalar_one_or_none()
    return policy_of(row)
