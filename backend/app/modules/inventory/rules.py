"""Dynamic device-group rules — the vocabulary, the evaluator, and the closed
document the API validates against.

DeviceGroup.rules jsonb: {"all": [{"fact": <path>, "op": <op>, "value": <v>}]}.
The first matching rule-bearing group claims the device; a group with no rules
is hand-curated and is never touched here. Malformed rules are skipped —
grouping must never break ingestion.

PRECEDENCE IS `(precedence, name)`, AND `name` IS ONLY THE TIEBREAK
(migration 0072, `docs/superpowers/specs/2026-08-22-device-group-management-
design.md` §10.1). It used to be `name` alone, which made the alphabet the
precedence list: renaming `z-fallback` to `a-fallback` promoted it above every
other rule group, and every device it matched moved onto a different patch
policy, a different backup policy and a different remote policy — with no rule
edited and nothing on screen saying so. `precedence` defaults to 0 for every
row, so groups that have never been ordered still fall back to exactly the
name order this evaluator produced before.

THE VOCABULARY IS CLOSED, AND IT IS CLOSED HERE (§5.1). `OPS` and
`FLAT_FACTS` are the single lists this module dispatches on AND the ones
`GroupRules` validates against, because two independently maintained lists is
how a rule the API accepts becomes a rule the ingest skips — and the only
evidence would be a `logger.warning` nobody reads. `Op` is spelled out rather
than derived (a `Literal` cannot be built from a runtime tuple) and
`tests/test_inventory_rules.py` holds the two equal in both directions.

WHY A CONSTRAINED VOCABULARY RATHER THAN AN EXPRESSION LANGUAGE. A device
group is an authorization surface: `deploy` resolves a `"group"` target to
`Device.device_group_id == target_id`, so a rule that matches more machines
than its author intended is a way to aim a signed job at machines nobody
chose. A rule is therefore operator INPUT that decides which machines a job
reaches, and the two structural defences are that it cannot say anything
outside this file's lists, and that nothing it contains is ever concatenated
into SQL — `evaluate_rules` selects the groups and compares in Python, and the
preview in `services/device_groups.py` does the same over stored documents.
There is no parser, no `eval`, and no user-supplied identifier reaching a
query.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import tr
from app.models import Device, DeviceGroup
from app.modules.inventory.schema import InventoryDocument

logger = logging.getLogger("its.inventory.rules")

_MISSING = object()

#: The flat fact namespace: every path that is not `facts.<key>` or
#: `software:<name>`. `_fact_value`'s table below is BUILT from this tuple, so
#: the namespace the evaluator can read and the namespace `GroupRules` accepts
#: cannot drift.
FLAT_FACTS: tuple[str, ...] = (
    "hostname",
    "os_family",
    "os_version",
    "architecture",
    "ram_bytes",
    "serial",
)

#: The two open-ended prefixes. Everything after them is a key in the agent's
#: `facts` map or an installed package name, so the remainder is free text —
#: but it is compared, never interpolated.
FACT_PREFIXES: tuple[str, ...] = ("facts.", "software:")


def _fact_value(doc: InventoryDocument, path: str) -> Any:
    """The document's value for one fact path, or `_MISSING`.

    IT READS THE DOCUMENT AND NEVER THE DEVICE ROW. That is why a device with
    no `InventorySnapshot` can never be claimed by a rule (§12) and why the
    re-evaluation sweep replays a stored document rather than reading columns.
    Cycle 1 still took a `Device` parameter it did not use; the preview has no
    device to pass, so the parameter is gone rather than passed `None`.
    """
    if path.startswith("facts."):
        return doc.facts.get(path.removeprefix("facts."), _MISSING)
    if path.startswith("software:"):
        name = path.removeprefix("software:")
        for item in doc.software:
            if item.name == name:
                return item.version
        return _MISSING
    flat = {
        "hostname": doc.hostname,
        "os_family": doc.os_family,
        "os_version": doc.os_version,
        "architecture": doc.architecture,
        "ram_bytes": doc.hardware.ram_bytes,
        "serial": doc.serial,
    }
    return flat.get(path, _MISSING) if path in FLAT_FACTS else _MISSING


def _present(fn: Callable[[Any, Any], bool]) -> Callable[[Any, Any], bool]:
    """Wrap an op so an absent or null fact simply does not match.

    Every op except `ne` behaves this way, and the asymmetry is deliberate
    rather than an oversight: "the OS family is not linux" is true of a machine
    whose OS family was never collected. It is also the single most dangerous
    property of the document, because a TYPO'd fact path under `ne` matches
    every device in the org and reads like a reasonable rule. `GroupRules`
    below is what makes that unreachable over HTTP.
    """

    def guarded(value: Any, expected: Any) -> bool:
        if value is _MISSING or value is None:
            return False
        return fn(value, expected)

    return guarded


#: The op table. `_condition_holds` dispatches on it and `Op` mirrors it.
_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": _present(lambda value, expected: bool(value == expected)),
    #: The one op that holds when the fact is ABSENT — see `_present`.
    "ne": lambda value, expected: value is _MISSING or value != expected,
    "contains": _present(
        lambda value, expected: str(expected).lower() in str(value).lower()
    ),
    "gt": _present(lambda value, expected: float(value) > float(expected)),
    "lt": _present(lambda value, expected: float(value) < float(expected)),
}

#: The op vocabulary, as data. `GroupRules` validates against `Op`; this is
#: what a test holds `Op` equal to.
OPS: tuple[str, ...] = tuple(_OPS)

#: Spelled out because `Literal` cannot be built from a runtime tuple. Held
#: equal to `OPS` in both directions by
#: `tests/test_inventory_rules.py::test_the_validators_op_set_is_the_
#: evaluators_op_set_in_both_directions`.
Op = Literal["eq", "ne", "contains", "gt", "lt"]


def _condition_holds(value: Any, op: str, expected: Any) -> bool:
    handler = _OPS.get(op)
    if handler is None:
        raise ValueError(  # i18n-exempt: claim_group logs it and skips the group
            f"unknown op {op!r}"
        )
    return handler(value, expected)


def matches_document(rules: Mapping[str, Any], doc: InventoryDocument) -> bool:
    """Does this document satisfy every condition in the rules document?

    Public because the preview (§5.3) evaluates the SAME predicate against
    stored snapshots. An author who is shown one answer here and gets another
    at the sweep has been shown a number, not a preview, so there is one
    matcher and both callers use it.

    Raises `ValueError`/`TypeError` on a document this evaluator cannot run.
    The ingest path catches and skips; the API refuses at authorship instead.
    """
    conditions = rules.get("all")
    if not isinstance(conditions, list) or not conditions:
        raise ValueError(  # i18n-exempt: claim_group logs it and skips the group
            "rules must carry a non-empty 'all' list"
        )
    for condition in conditions:
        fact, op = condition.get("fact"), condition.get("op")
        if not fact or not op:
            raise ValueError(  # i18n-exempt: claim_group logs it and skips the group
                f"malformed condition {condition!r}"
            )
        if not _condition_holds(_fact_value(doc, fact), op, condition.get("value")):
            return False
    return True


# --------------------------------------------------------------------------- #
# The closed document (§5.1)
# --------------------------------------------------------------------------- #


class RuleCondition(BaseModel):
    """One condition. `extra="forbid"`, so a misspelled KEY is a 422 rather
    than a silently ignored half of a rule."""

    model_config = ConfigDict(extra="forbid")

    fact: str = Field(
        description="Fact path: a flat name, `facts.<key>`, or `software:<name>`"
    )
    op: Op = Field(description="Comparison: eq, ne, contains, gt or lt")
    value: str | int | float | bool = Field(description="What the fact is compared to")

    @field_validator("fact")
    @classmethod
    def _fact_must_resolve(cls, value: str) -> str:
        """VALIDATOR 1 (§5.1). An unknown fact path is not an error to the
        evaluator: `_fact_value` returns `_MISSING`, and `_condition_holds`
        then answers False for every op EXCEPT `ne`, which answers True.

        So `{"fact": "os_familly", "op": "ne", "value": "windows"}` claims
        every device in the organization and reads like a reasonable rule.
        That is the single most dangerous property of this document, and this
        is the line that makes it unreachable over HTTP.
        """
        for prefix in FACT_PREFIXES:
            if value.startswith(prefix):
                if not value.removeprefix(prefix).strip():
                    raise ValueError(tr("`{prefix}` needs a name after it", prefix=prefix))
                return value
        if value not in FLAT_FACTS:
            raise ValueError(
                tr(
                    "unknown fact `{value}` — use one of "
                    "{facts}, or a `facts.<key>` / "
                    "`software:<name>` path",
                    value=value,
                    facts=", ".join(FLAT_FACTS),
                )
            )
        return value

    @field_validator("value")
    @classmethod
    def _numeric_ops_need_a_number(
        cls, value: str | int | float | bool, info: Any
    ) -> str | int | float | bool:
        """VALIDATOR 2 (§5.1). `gt` and `lt` call `float(expected)`, which
        raises `ValueError` on anything else — caught by the ingest path,
        logged, and the group skipped. A rule that can never be evaluated is
        an authoring error and belongs at the point of authorship.

        VALIDATOR 3 IS THE ABSENCE OF A FOURTH. `contains` on `ram_bytes`, and
        every other numeric-only combination, is PERMITTED: `str(value)` is
        well defined and the operator may genuinely want it. *A rule that
        matches nothing is not an error. A rule that cannot be evaluated is.*
        That is the whole line.
        """
        if info.data.get("op") in ("gt", "lt"):
            try:
                float(value)
            except (TypeError, ValueError):
                raise ValueError(
                    tr(
                        "`greater than` and `less than` compare numbers — "
                        "`{value}` is not one",
                        value=value,
                    )
                ) from None
        return value


class GroupRules(BaseModel):
    """A membership rules document: every condition must hold.

    CLOSED ON PURPOSE. `787461f` kept `rules` off the list route because a
    `dict[str, Any]` field makes a response an open key space; this model is
    what lets the detail route return it and the write routes accept it.
    """

    model_config = ConfigDict(extra="forbid")

    all: list[RuleCondition] = Field(
        min_length=1,
        description="Conditions that must ALL hold for a device to be claimed",
    )


def claim_group(
    groups: Sequence[DeviceGroup], doc: InventoryDocument
) -> DeviceGroup | None:
    """The FIRST group in `groups` whose rules this document satisfies, or None.

    THE ONE DECISION FUNCTION, and there are now three callers that must not
    disagree: the ingest path (`evaluate_rules` below), the console's preview,
    and the re-evaluation sweep. A second implementation anywhere is how an
    author is shown one set and given another, or how the sweep moves a machine
    back and forth between two groups on alternate beats.

    `groups` MUST ALREADY BE IN `(precedence, name)` ORDER AND ALREADY SCOPED.
    This function does no query, so it cannot enforce either -- it is pure
    precisely so the sweep can fetch the group list ONCE per org instead of
    once per device. `evaluate_rules` is the query, and the sweep uses the same
    one.

    A malformed document is SKIPPED with a warning rather than raised on:
    grouping must never break ingestion, and a group whose `rules` jsonb the
    evaluator cannot run must not stop the groups after it from claiming
    anything.
    """
    for group in groups:
        try:
            matched = matches_document(group.rules or {}, doc)
        except (ValueError, TypeError) as exc:
            # I-5: this catches BOTH ValueError and TypeError, so exc_type
            # only narrows it to one of two -- the group's own id, as an
            # explicit extra= field rather than only inside the %-arg
            # message text, is what actually lets an operator find and fix
            # the malformed rules.jsonb row.
            logger.warning(
                "skipping malformed rules on group %s",
                group.name,
                extra={"device_group_id": group.id},
                exc_info=exc,
            )
            continue
        if matched:
            return group
    return None


def rule_bearing_groups_query(org_id: UUID) -> Select[tuple[DeviceGroup]]:
    """The one statement that decides WHICH groups compete, and in what order.

    Shared by the ingest path and the sweep rather than written twice, because
    all four of its predicates are load-bearing and each was a defect on its
    own: `org_id` (a missing tenant predicate cross-assigns devices between
    tenants on the admin engine the sweep's siblings hold), `archived_at IS
    NULL` (§7.1's guarantee that an archived group holds no devices is what
    lets six modules stay ignorant of the column), `(precedence, name)`
    (migration 0072 -- before it the alphabet decided which rule won, so a
    rename re-segmented a fleet), and:

    `jsonb_typeof(rules) = 'object'` RATHER THAN `rules IS NOT NULL`, WHICH IS
    A BUG FIX AND NOT A FLOURISH. SQLAlchemy's JSON types serialize a Python
    `None` into the JSON literal `null` unless told otherwise, so every static
    group cycle 1 created was written with `rules = 'null'::jsonb` -- for which
    `IS NOT NULL` is TRUE. It read back as Python `None`, so every response was
    correct and nothing on any screen was wrong; what was wrong was this query,
    which selected each static group as rule-bearing and then logged a warning
    when it could not run the empty document. `DeviceGroup.rules` now stores
    SQL NULL (`none_as_null=True`), and this predicate is what makes the rows
    ALREADY WRITTEN behave. It asks the question that was always meant: does
    this group hold a rules document. A `'null'`, a `[]`, a `3` and a NULL are
    all "no", by the same rule, without a migration to go and find them.
    """
    return (
        select(DeviceGroup)
        .where(
            DeviceGroup.org_id == org_id,
            func.jsonb_typeof(DeviceGroup.rules) == "object",
            DeviceGroup.archived_at.is_(None),
        )
        .order_by(DeviceGroup.precedence, DeviceGroup.name)
    )


async def evaluate_rules(
    session: AsyncSession,
    device: Device,
    doc: InventoryDocument,
    *,
    org_id: UUID,
) -> UUID | None:
    """Assign the device to the first matching rule-bearing LIVE group of
    `org_id`, in `(precedence, name)` order. Returns the winning group id, or
    None when no rule group matched (a manual placement, if any, is preserved).

    `org_id` IS AN ARGUMENT, NOT `device.org_id` (§8.3.2). Cycle 1 shipped this
    query with no tenant predicate at all, safe only because its one caller ran
    inside `org_session` and RLS scoped the select. This cycle adds a second
    caller in `app/worker.py`, where four sibling sweeps hold a BYPASSRLS admin
    engine — and on that engine a missing predicate cross-assigns devices
    between tenants with nothing turning red, because
    `docs/superpowers/audits/2026-08-21-tenancy-test-efficacy.md` §0's trap is
    that RLS covers for the application everywhere else. Taking the org from
    the CALLER rather than from the row means the caller cannot pass a device
    it did not scope.

    ARCHIVED GROUPS CLAIM NOTHING. §7.1 lets six modules stay ignorant of
    `archived_at` on the strength of one sentence — an archived group holds no
    devices, so an action aimed at one reaches zero of them. An archived
    rule-bearing group is reachable (the archive refusal counts live
    references, and an empty rule group has none), so without this predicate
    the next sweep would repopulate it and falsify that sentence. The spec does
    not say this; it follows from §7.1.

    ONLY THE DEVICE'S OWN CLIENT'S GROUPS COMPETE (MSP clients spec §1.5): a
    group organises one client's devices, and `its_device_client_guard` refuses
    a device in another client's group. A rule of another client that matched
    would otherwise win and fail the ingest on that trigger.
    """
    groups = (
        (
            await session.execute(
                rule_bearing_groups_query(org_id).where(
                    DeviceGroup.client_id == device.client_id
                )
            )
        )
        .scalars()
        .all()
    )
    group = claim_group(groups, doc)
    if group is None:
        return None
    if device.device_group_id != group.id:
        device.device_group_id = group.id
        session.add(device)
    return group.id
