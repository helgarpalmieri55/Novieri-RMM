"""Unit tests for the property predicate (spec §6.3).

The CLOSED case is the one that matters: a predicate that flags everything
would make the sweep's numbers meaningless while looking like it worked.
"""

from typing import Any

from pydantic import BaseModel, Field

from tests.tool_surface import model_openness


class Closed(BaseModel):
    """A model whose key space is finite and derivable."""

    device_id: str = Field(description="d")
    hostname: str = Field(description="h")


class Nested(BaseModel):
    """Holds a closed model."""

    inner: Closed = Field(description="i")


class OpenInt(BaseModel):
    """`dict[str, int]` -- keys are data."""

    totals: dict[str, int] = Field(description="t")


class OpenAny(BaseModel):
    """`dict[str, Any]` -- a JSON blob."""

    params_schema: dict[str, Any] = Field(description="p")


class OpenModelValued(BaseModel):
    """`dict[str, Closed]` -- typed VALUES, data KEYS. Still open."""

    by_host: dict[str, Closed] = Field(description="b")


class DeeplyOpen(BaseModel):
    """The open dict is two models down, where a shallow walk would miss it."""

    nested: Nested = Field(description="n")
    also: OpenInt = Field(description="a")


class SecretNamed(BaseModel):
    """`report_key` splits to `report` + `key`; `key` is a secret word."""

    report_key: str = Field(description="r")


class ListOfOpen(BaseModel):
    """The open dict is inside a list element."""

    rows: list[OpenInt] = Field(description="r")


def test_a_closed_model_reports_nothing() -> None:
    assert model_openness(Closed) == []
    assert model_openness(Nested) == []


def test_an_open_dict_is_reported() -> None:
    assert any("open dict" in r for r in model_openness(OpenInt))
    assert any("open dict" in r for r in model_openness(OpenAny))


def test_a_model_valued_dict_is_still_open() -> None:
    """No carve-out: `{"<hostname>": {...}}` is as unregisterable as
    `{"<hostname>": 3}`, because `scrub` walks KEYS."""
    assert any("open dict" in r for r in model_openness(OpenModelValued))


def test_openness_is_found_at_depth() -> None:
    reasons = model_openness(DeeplyOpen)
    assert any("open dict" in r for r in reasons), reasons


def test_openness_is_found_inside_a_list() -> None:
    reasons = model_openness(ListOfOpen)
    assert any("open dict" in r for r in reasons), reasons


def test_a_secret_shaped_field_name_is_reported() -> None:
    reasons = model_openness(SecretNamed)
    assert any("secret-shaped" in r and "report_key" in r for r in reasons), reasons


def test_reasons_name_the_path_so_a_failure_is_actionable() -> None:
    (reason,) = model_openness(OpenInt)
    assert "totals" in reason
