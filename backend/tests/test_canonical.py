from datetime import UTC, datetime

import pytest

from app.jobs.canonical import canonical_bytes, rfc3339


def test_key_order_independent() -> None:
    assert canonical_bytes({"b": 1, "a": 2}) == canonical_bytes({"a": 2, "b": 1})


def test_compact_sorted_output() -> None:
    assert canonical_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


def test_nested_dicts_sorted() -> None:
    assert canonical_bytes({"z": {"y": 1, "x": 2}, "a": 0}) == b'{"a":0,"z":{"x":2,"y":1}}'


def test_unicode_not_escaped() -> None:
    assert canonical_bytes({"name": "Tóshée"}) == '{"name":"Tóshée"}'.encode()


def test_datetime_rejected() -> None:
    with pytest.raises(TypeError):
        canonical_bytes({"ts": datetime(2026, 1, 1, tzinfo=UTC)})


def test_non_serializable_rejected() -> None:
    with pytest.raises(TypeError):
        canonical_bytes({"x": object()})


def test_rfc3339_utc_z() -> None:
    assert rfc3339(datetime(2026, 1, 1, tzinfo=UTC)) == "2026-01-01T00:00:00Z"


def test_rfc3339_converts_to_utc() -> None:
    from datetime import timedelta, timezone

    dt = datetime(2026, 1, 1, 5, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert rfc3339(dt) == "2026-01-01T00:00:00Z"
