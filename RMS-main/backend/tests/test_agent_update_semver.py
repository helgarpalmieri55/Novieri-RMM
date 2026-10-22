"""The shared semver vectors (spec-self-update.md §4.3).

The Go agent reads the same file in `agent/internal/update/semver_test.go`,
so the two comparators cannot drift apart silently.
"""

import json
from pathlib import Path

import pytest

from app.modules.agent_update.semver import parse_version

VECTORS = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "kernel"
        / "wire-protocol"
        / "testdata"
        / "semver-order.json"
    ).read_text()
)


@pytest.mark.parametrize("case", VECTORS["valid"], ids=lambda c: c["input"])
def test_valid_versions_parse_to_their_canonical_form(case: dict[str, str]) -> None:
    assert str(parse_version(case["input"])) == case["canonical"]


@pytest.mark.parametrize("text", VECTORS["invalid"])
def test_invalid_versions_are_refused(text: str) -> None:
    with pytest.raises(ValueError):
        parse_version(text)


def test_the_ascending_list_is_strictly_ordered_pairwise() -> None:
    parsed = [parse_version(s) for s in VECTORS["ascending"]]
    for i, a in enumerate(parsed):
        for j, b in enumerate(parsed):
            assert (a < b) == (i < j), (VECTORS["ascending"][i], VECTORS["ascending"][j])
            assert (a == b) == (i == j)


@pytest.mark.parametrize("pair", VECTORS["equal"])
def test_equal_pairs_compare_equal(pair: list[str]) -> None:
    assert parse_version(pair[0]) == parse_version(pair[1])
