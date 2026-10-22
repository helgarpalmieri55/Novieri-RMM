"""Flap suppression: bound the ACTIONS, never the evidence
(`docs/modules/endpoint-health-alerting/spec.md` §0.9, §2.7).

WHAT THIS DECIDES, AND WHAT IT DOES NOT. `note_open` answers exactly one
question: may this key's ACTIONS fire? The alert row is written either way, with
`flapping = true` and the count on it. §0.9 is explicit about why: a design that
suppresses the RECORD turns "this device flapped 40 times overnight" into "it
was fine", which is the story an operator will believe because nothing
contradicts it. `flapping` and `flap_count` are columns on `health_alert`
precisely so the console can sort by them -- a flapping device is a FINDING, and
often a more important one than any single breach.

Rejected alternatives, both named in §0.9: backoff on NOTIFICATION only (the
remediation job still fires every cycle, so the restart loop continues and only
the human stops hearing about it), and per-check hysteresis bands (double the
config surface for per-check tuning nobody performs, when `for_ticks` already
supplies the primary hysteresis at zero config cost).

THE ALGORITHM, AND THE ONE STEP §0.9 DOES NOT SPELL OUT. §0.9 gives:

    if now - window_start > flap_window_s:   window_start = now; open_count = 0
    open_count += 1
    if open_count > flap_threshold:          suppressed_until = now + cooldown

plus "refuse further opens for this key until `suppressed_until`". Ordering the
cooldown against the window is left open, and the wrong order is a real defect:
if the cooldown lapses while `open_count` is still above the threshold, the very
next open re-suppresses immediately and the "cooldown" becomes a permanent
silence nobody chose. So an EXPIRED cooldown clears the flag AND restarts the
window, which is what "the key is armed again" has to mean. Recorded here rather
than discovered later, and pinned by
`test_the_key_is_armed_again_once_the_cooldown_expires`.

ONE ROW PER KEY, UPSERTED (§0.12 clause 3). The row is created with
`ON CONFLICT DO NOTHING` and then re-read `FOR UPDATE`, so two concurrent opens
for one key serialise on the row rather than both reading the same count and
both writing it back.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.health.models import HealthFlapState
from app.modules.health.settings import get_settings

__all__ = ["AlertKey", "FlapDecision", "note_open"]

logger = logging.getLogger("its.health.flap")


@dataclass(frozen=True)
class AlertKey:
    """§0.8's dedup key, minus the org (which every service takes separately).

    A dataclass rather than three positional arguments because every caller
    passes the same triple and a transposed `check_id`/`device_id` pair would be
    a silent cross-check suppression.
    """

    device_id: UUID
    check_id: UUID
    selector: str = ""


@dataclass(frozen=True)
class FlapDecision:
    """The answer, and the evidence that goes on the alert row.

    `flap_count` is returned even when `suppressed` is False: it is what
    `health_alert.flap_count` stores, and the console sorts by it.
    """

    suppressed: bool
    flap_count: int
    suppressed_until: datetime | None


async def note_open(
    session: AsyncSession, org_id: UUID, key: AlertKey, *, now: datetime
) -> FlapDecision:
    """Count a candidate alert open for `key` and decide whether its actions
    fire.

    `now` is injected rather than read from the clock so the caller's whole
    ingest transaction shares one instant -- two report lines in one batch must
    not straddle a window boundary because the second one was evaluated a
    millisecond later.
    """
    settings = await get_settings(session, org_id)

    statement = pg_insert(HealthFlapState).values(
        org_id=org_id,
        device_id=key.device_id,
        check_id=key.check_id,
        selector=key.selector,
        window_start=now,
        open_count=0,
        suppressed_until=None,
        updated_at=now,
    )
    await session.execute(
        statement.on_conflict_do_nothing(
            index_elements=["org_id", "device_id", "check_id", "selector"]
        )
    )
    row = (
        await session.execute(
            select(HealthFlapState)
            .where(
                HealthFlapState.org_id == org_id,
                HealthFlapState.device_id == key.device_id,
                HealthFlapState.check_id == key.check_id,
                HealthFlapState.selector == key.selector,
            )
            .with_for_update()
        )
    ).scalar_one()

    cooling_down = row.suppressed_until is not None and now < row.suppressed_until

    if row.suppressed_until is not None and not cooling_down:
        # The cooldown lapsed. Clear the flag AND restart the window: leaving
        # `open_count` above the threshold would re-suppress on the very next
        # open, which is a permanent silence wearing a cooldown's name.
        row.suppressed_until = None
        row.window_start = now
        row.open_count = 0
    elif not cooling_down and (now - row.window_start) > timedelta(
        seconds=settings.flap_window_s
    ):
        # §0.9's first line, strictly `>`: an open at exactly the window length
        # is still inside the window.
        row.window_start = now
        row.open_count = 0

    row.open_count += 1
    row.updated_at = now

    if cooling_down:
        # Still refused, and still counted: suppression removes the actions, not
        # the evidence.
        await session.flush()
        return FlapDecision(
            suppressed=True, flap_count=row.open_count, suppressed_until=row.suppressed_until
        )

    if row.open_count > settings.flap_threshold:
        row.suppressed_until = now + timedelta(seconds=settings.flap_cooldown_s)
        logger.info(
            "health: key (device=%s check=%s selector=%r) opened %d times in %ds; "
            "suppressing its actions until %s",
            key.device_id,
            key.check_id,
            key.selector,
            row.open_count,
            settings.flap_window_s,
            row.suppressed_until,
        )
        await session.flush()
        return FlapDecision(
            suppressed=True, flap_count=row.open_count, suppressed_until=row.suppressed_until
        )

    await session.flush()
    return FlapDecision(suppressed=False, flap_count=row.open_count, suppressed_until=None)
