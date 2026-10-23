"""The shape of a suggested match (asset-inventory spec §4).

WHAT THIS TABLE IS FOR, asserted rather than remembered: it is the annoyance
the spec deliberately chose over a bad merge. An enrolling agent links only on
a physical MAC; on a shared hostname, a shared address, or two records matching
at once it creates the device and files one of these instead, because
auto-linking on a hostname merges two real machines that share a name across
sites and nothing afterwards recovers which data belonged to which.
"""

from app.models import SuggestedAssetMatch


def test_it_records_why_it_was_a_suggestion() -> None:
    """`basis` is the first thing an operator resolving this needs, and it is
    not derivable after the fact: by the time they look, the hostname may have
    changed and the lease may have moved."""
    assert "basis" in SuggestedAssetMatch.__table__.columns


def test_it_names_both_rows_and_neither_is_optional() -> None:
    columns = SuggestedAssetMatch.__table__.columns
    assert columns["device_id"].nullable is False
    assert columns["candidate_device_id"].nullable is False


def test_it_is_tenant_scoped() -> None:
    """Every query for a suggestion crosses two device rows, and both must be
    in the caller's org. `org_id` here is what the RLS policy in 0063 reads."""
    assert "org_id" in SuggestedAssetMatch.__table__.columns
    assert SuggestedAssetMatch.__table__.columns["org_id"].nullable is False


def test_an_open_pair_is_unique_but_a_resolved_one_is_not() -> None:
    """THE PARTIAL INDEX, and it is load-bearing in both directions.

    Without the uniqueness, a machine that re-enrols stacks a second identical
    open question and an operator facing forty copies stops reading the queue.
    Without the partiality, a pair rejected in March could never be raised
    again in June when the same evidence recurs -- and the second time may be
    the time it matters.
    """
    indexes = {index.name: index for index in SuggestedAssetMatch.__table__.indexes}
    assert "uq_sam_open_pair" in indexes, sorted(indexes)
    index = indexes["uq_sam_open_pair"]
    assert index.unique is True
    assert index.dialect_options["postgresql"]["where"] is not None


def test_it_has_no_updated_at() -> None:
    """A suggestion is not edited, it is RESOLVED. `resolved_at` is its only
    state change, and a second timestamp that nothing writes is a column a
    reader will eventually trust."""
    assert "updated_at" not in SuggestedAssetMatch.__table__.columns
    assert "resolved_at" in SuggestedAssetMatch.__table__.columns
