"""Which of the three action kinds a check declares
(`docs/modules/endpoint-health-alerting/spec.md` §0.10, §7.1, §7.3).

§0.10: "a check may declare up to one of each action kind: `remediate`,
`open_ticket`, `notify`". Until migration 0044 the schema carried no such
declaration -- §2.2's column list ended at `retired_at` and §2's nine tables
held nothing relating a check to an action -- so this module derived one, and a
constant plus a forcing test recorded that it was interim. Both are gone: the
declaration is now three columns on `health_check` (§2.2's amendment) and the
functions below read them.

WHAT EACH KIND MEANS HERE, AND WHY ONE OF THEM IS STILL CONDITIONAL:

  - `notify` is `check.declares_notify`, nothing else. False is §7.3's
    silent-by-design check: the alert row, its action rows, its audit records
    and its UI event are written exactly as for any other alert, and NO
    `health.alert_raised`/`alert_cleared`/`alert_escalated` event is published.
    Silence is the absence of an event, never a flag on one --
    `notification/routing.py::resolve_recipients` selects rules on
    `(org_id, event_type, active)` narrowed only by `queue_id`, so a
    `"notify": false` payload key would have decided nothing.

  - `open_ticket` is `check.declares_open_ticket` AND the org having configured
    BOTH `default_alert_queue_id` and `default_ticket_category_id`. The second
    half is not a policy choice and does not belong to the operator:
    `tickets.create_ticket` takes `TicketCreate.category_id` as a REQUIRED UUID
    and resolves a queue or raises, so without both settings the action cannot
    execute at all and declaring it would mint a row whose only outcome is
    `dead`. A declaration the org cannot honour is dropped here rather than
    turned into a failed row at 03:00.

  - `remediate` is `check.remediate_approved_action_id IS NOT NULL`. There is
    no boolean: the declaration IS the action id, because
    `actions.py::_execute_remediate` resolves `health_alert_action.
    approved_action_id` and marks the row `skipped` when it is NULL. NULL is
    the column default, so REMEDIATION IS OFF unless a human named an action --
    and naming one is only the first of five gates. The others are unchanged by
    this module: `_refuse_reason` (inactive action, unregistered job type,
    missing `health` persona, drifted `script.run` version), `_fuse_breach`
    (cooldown, per-device/day, per-org/hour), §8.3's autonomy dial
    (`suggest` -> `skipped`, `require_approval` -> `awaiting_approval`), and
    `ck_approved_action_autonomous_is_bounded`, which makes an autonomous
    destructive or high-risk action unstorable at the database.

THE ORDER IS NOT THE CALLER'S. Both callers iterate what these functions
return, and returning the kinds in §0.10's fixed order is what makes the
`health_alert_action` rows come out in it.
"""

from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.modules.health.models import HealthCheck
    from app.modules.health.settings import EffectiveHealthSettings

__all__ = [
    "ACTION_KINDS",
    "approved_action_id_for",
    "declared_action_kinds",
]

# §0.10's fixed order: remediate -> open_ticket -> notify. Stated once, here,
# because the ORDER is a design decision (remediation's job id is context the
# other two carry; the ticket must exist before the mail can name a number that
# resolves; notify is last because it is the only irreversible external side
# effect) and three copies of it is three chances to reorder one of them.
ACTION_KINDS: tuple[str, ...] = ("remediate", "open_ticket", "notify")


def declared_action_kinds(
    check: "HealthCheck", settings: "EffectiveHealthSettings"
) -> tuple[str, ...]:
    """The action kinds this check declares, in §0.10's fixed order.

    Returns a subset of `ACTION_KINDS` and never reorders it: the caller writes
    one `health_alert_action` row per element and the worker executes them in
    the order they are returned.
    """
    kinds: list[str] = []
    if check.remediate_approved_action_id is not None:
        kinds.append("remediate")
    if (
        check.declares_open_ticket
        and settings.default_alert_queue_id is not None
        and settings.default_ticket_category_id is not None
    ):
        kinds.append("open_ticket")
    if check.declares_notify:
        kinds.append("notify")
    return tuple(k for k in ACTION_KINDS if k in kinds)


def approved_action_id_for(check: "HealthCheck", kind: str) -> UUID | None:
    """The `health_alert_action.approved_action_id` for a row of this kind.

    One function rather than a conditional at each site that writes an action
    row: a `remediate` row minted without the id its check names is a row
    `_execute_remediate` can only mark `skipped`, and the two writers (§6's
    ingestion and §12's offline sweep) would otherwise each have to remember.
    `open_ticket` and `notify` carry no approved action and get NULL.
    """
    return check.remediate_approved_action_id if kind == "remediate" else None
