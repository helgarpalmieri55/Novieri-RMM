"""The documents that describe the product as it is name Keycloak only as
history (own authentication phase 4, plan 2026-11-15, Task 10).

Specs and plans are dated records and may describe what was decided then;
the runbooks are `test_runbook_without_keycloak.py`'s. A line here may name
the old identity provider only if it says when that stopped being true, by
the runbooks' rule (`test_runbook_without_keycloak._dated`): own
authentication's phase 4 by name, or a date of 2026-11 or later, on it, on
one of the three lines before, or on the next -- and not in the future
tense. Until the final review of phase 4 (C4) this file had a rule of its
own, "phase 4" anywhere in the window, which a future tense and the
roadmap's own Phase 4 both passed.
"""

import re
from pathlib import Path

from tests.test_runbook_without_keycloak import _dated

REPO = Path(__file__).resolve().parents[2]
NAMES = re.compile(r"keycloak", re.IGNORECASE)
LIVING = (
    REPO / "docs" / "01-architecture.md",
    REPO / "docs" / "02-tech-stack.md",
    REPO / "docs" / "04-data-model.md",
    REPO / "docs" / "06-roadmap.md",
    REPO / "docs" / "08-observability.md",
    REPO / "docs" / "product.md",
    REPO / "docs" / "modules" / "ai-admin.md",
    REPO / "DESIGN.md",
    REPO / "HANDOFF.md",
)


def _undated(text: str, name: str = "doc") -> list[str]:
    lines = text.splitlines()
    return [
        f"{name}:{index + 1}: {line.strip()[:100]}"
        for index, line in enumerate(lines)
        if NAMES.search(line) and not _dated(lines, index)
    ]


def test_the_rule_sees_an_undated_name() -> None:
    assert _undated("Auth -- Keycloak as identity broker.") != []
    assert _undated("Keycloak was removed in own\nauthentication phase 4.") == []
    # The runbooks' rule, not a looser one of its own (final review, C4).
    assert _undated("Keycloak signs people in; phase 4 removes it.") != []
    assert _undated("## Phase 4 — Patch, App-deploy\n\nAuth: Keycloak as identity broker.") != []
    assert _undated("Keycloak stays until it is removed in own authentication phase 4.") != []
    assert _undated("Keycloak was removed in own authentication Phase 4.") == []


def test_the_living_documents_name_keycloak_only_as_history() -> None:
    assert [line for path in LIVING for line in _undated(path.read_text(), path.name)] == []
