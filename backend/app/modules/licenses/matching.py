"""Licenses/SAM rule-based matching engine (docs/modules/licenses/spec.md §4):
the crux of the module -- it decides which installed-software rows count
against a product's entitlements, which is what the compliance position (and
therefore any audit) is built on.

Rules are operator-authored (RBAC `licenses.manage`) but matched against every
row in the org's inventory during the compliance sweep, so two properties are
non-negotiable:

- **Tenant isolation**: `product_installed_device_ids` carries an explicit
  `InstalledSoftware.org_id == org_id` predicate. Matching itself happens in
  Python (not a DB regex) so the tenant boundary stays in this one query
  rather than being smeared across SQL and application code.
- **Safety**: a pathological or invalid regex must never reach the matching
  loop. `validate_match_rules` rejects bad rules at write time
  (`LicenseValidationError`), so `rule_matches` only ever sees rules that
  already compiled once -- its own `re.error` handling is a defensive
  backstop, not the primary guard.
"""

import re
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import tr
from app.models import InstalledSoftware, SoftwareProduct
from app.modules.licenses.errors import LicenseValidationError

# Cap on a rule's `pattern` length -- both a saneness bound on the stored data
# and a defense against a pathologically long regex being compiled per-row
# during the compliance sweep.
_MAX_PATTERN_LEN = 200

_RULE_TYPES = {"exact", "prefix", "regex"}


def rule_matches(rule: dict[str, Any], name: str, publisher: str) -> bool:
    """True if an installed-software (name, publisher) matches one match rule.

    `exact`/`prefix` compare `name` case-insensitively; `regex` uses an
    anchored `re.fullmatch` (case-insensitive). If `rule["publisher"]` is set
    (non-null, non-empty), the publisher must also match case-insensitively.
    """
    rule_publisher = rule.get("publisher")
    if rule_publisher:
        if publisher.casefold() != str(rule_publisher).casefold():
            return False

    rule_type = rule.get("type")
    pattern = rule.get("pattern", "")
    name_casefold = name.casefold()
    pattern_casefold = str(pattern).casefold()

    if rule_type == "exact":
        return name_casefold == pattern_casefold
    if rule_type == "prefix":
        return name_casefold.startswith(pattern_casefold)
    if rule_type == "regex":
        try:
            return re.fullmatch(str(pattern), name, re.IGNORECASE) is not None
        except re.error:
            # Rules are validated at write time (validate_match_rules); a rule
            # that fails to compile here should never happen, but must never
            # raise into the middle of a compliance sweep.
            return False
    return False


def validate_match_rules(rules: list[dict[str, Any]]) -> None:
    """Raise `LicenseValidationError` if `rules` is not a well-formed list of
    match rules: not a list; a rule missing `type` or with a `type` outside
    `{exact, prefix, regex}`; an empty `pattern`; a `pattern` longer than
    `_MAX_PATTERN_LEN`; or a `regex` rule whose `pattern` fails to compile.
    """
    if not isinstance(rules, list):
        raise LicenseValidationError(tr("match_rules must be a list"))

    for rule in rules:
        rule_type = rule.get("type") if isinstance(rule, dict) else None
        if not isinstance(rule_type, str) or rule_type not in _RULE_TYPES:
            raise LicenseValidationError(
                tr("match rule has invalid type: {rule_type}", rule_type=repr(rule_type))
            )

        pattern = rule.get("pattern")
        if not isinstance(pattern, str) or pattern == "":
            raise LicenseValidationError(tr("match rule pattern must be non-empty"))
        if len(pattern) > _MAX_PATTERN_LEN:
            raise LicenseValidationError(
                tr(
                    "match rule pattern exceeds {max_pattern_len} characters",
                    max_pattern_len=_MAX_PATTERN_LEN,
                )
            )

        if rule_type == "regex":
            try:
                re.compile(pattern)
            except re.error as exc:
                raise LicenseValidationError(
                    tr("match rule regex does not compile: {pattern}", pattern=repr(pattern))
                ) from exc


async def product_installed_device_ids(
    session: AsyncSession, org_id: UUID, product: SoftwareProduct
) -> set[UUID]:
    """Return the set of THIS ORG's device_ids running software that matches
    ANY of `product.match_rules`.

    Loads `InstalledSoftware` explicitly filtered `WHERE org_id == org_id`
    (invariant #5) and matches in Python -- keeps regex out of SQL and the
    tenant boundary in one predicate. A device with multiple matching rows is
    counted once (the result is a set).
    """
    rows = await session.execute(
        select(
            InstalledSoftware.device_id,
            InstalledSoftware.name,
            InstalledSoftware.publisher,
        ).where(InstalledSoftware.org_id == org_id)
    )

    device_ids: set[UUID] = set()
    for device_id, name, publisher in rows:
        if any(rule_matches(rule, name, publisher) for rule in product.match_rules):
            device_ids.add(device_id)
    return device_ids
