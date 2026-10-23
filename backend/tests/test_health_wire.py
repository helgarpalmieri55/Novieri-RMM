"""The `health_report` agent->server wire kind (docs/modules/endpoint-health-
alerting/spec.md §5.1).

Pure parse-layer tests: no database, no Redis.

TASK 2's INTERIM BRANCH IS GONE, and its two tests with it. This file used to
carry `test_health_report_ingest_is_still_interim`, which imported
`app.gateway.dispatch.HEALTH_REPORT_INGEST_IS_INTERIM` so that ingestion could
not land while a test still asserted reports were dropped, plus
`test_a_health_report_is_dropped_without_touching_jobs`, which pinned the drop
as values. Task 9 replaced the branch with a real `_handle_health_report`, so
both were deleted deliberately and visibly -- the forcing function did its job
and a forcing function nobody removes is just a stale test. The gateway
branch's behaviour is now pinned in `tests/test_health_ingest.py`.

The wire model is the security boundary here. Everything below `parse_agent_
message` -- ingestion, the alert lifecycle, remediation dispatch -- runs on
whatever this model let through, so each malformation below is tested as a
`ValidationError`, not as "the parser coped".
"""

import json
from typing import Any

import pytest
from pydantic import ValidationError

from app.gateway.protocol import HealthReportLine, HealthReportMsg, parse_agent_message


def _line(**overrides: Any) -> dict[str, Any]:
    line = {
        "check_id": "3f0b2b8e-0b6e-4a1a-9f4a-1c2d3e4f5a6b",
        "selector": "C:",
        "kind": "breach",
        "state": "breaching",
        "value": "3.1",
        "observed_at": "2026-08-02T09:14:00Z",
    }
    line.update(overrides)
    return line


def _report(**overrides: Any) -> str:
    msg: dict[str, Any] = {
        "v": 1,
        "kind": "health_report",
        "policy_version": 7,
        "reports": [_line()],
    }
    msg.update(overrides)
    return json.dumps(msg)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_parse_health_report_with_breach_recovery_and_summary() -> None:
    msg = parse_agent_message(
        _report(
            reports=[
                _line(kind="breach", state="breaching", value="3.1"),
                _line(kind="recovery", state="ok", selector="Spooler", value="running"),
                _line(kind="summary", state="ok", selector="", value="41.0"),
            ]
        )
    )
    assert isinstance(msg, HealthReportMsg)
    assert msg.kind == "health_report"
    assert msg.policy_version == 7
    assert [line.kind for line in msg.reports] == ["breach", "recovery", "summary"]
    assert [line.state for line in msg.reports] == ["breaching", "ok", "ok"]
    assert msg.reports[1].selector == "Spooler"
    assert msg.reports[2].value == "41.0"


def test_selector_and_value_default_to_empty() -> None:
    """A summary line for a check with no selector omits both; the model must
    supply `""` rather than making the agent send two empty strings."""
    msg = parse_agent_message(
        _report(
            reports=[
                {
                    "check_id": "c1",
                    "kind": "summary",
                    "state": "ok",
                    "observed_at": "2026-08-02T09:14:00Z",
                }
            ]
        )
    )
    assert isinstance(msg, HealthReportMsg)
    assert msg.reports[0].selector == ""
    assert msg.reports[0].value == ""


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


def test_empty_reports_list_rejected() -> None:
    """`min_length=1`. An empty batch is a network write on a tick with nothing
    to say -- spec §0.5's falsifier 4 -- so the wire refuses to carry one."""
    with pytest.raises(ValidationError):
        parse_agent_message(_report(reports=[]))


def test_sixty_four_report_lines_accepted_and_sixty_five_rejected() -> None:
    """`max_length=64`. Both halves: 64 must pass, or the bound could be any
    number at all and the test would not notice."""
    ok = parse_agent_message(_report(reports=[_line() for _ in range(64)]))
    assert isinstance(ok, HealthReportMsg)
    assert len(ok.reports) == 64
    with pytest.raises(ValidationError):
        parse_agent_message(_report(reports=[_line() for _ in range(65)]))


def test_sixty_four_character_value_accepted_and_sixty_five_rejected() -> None:
    ok = parse_agent_message(_report(reports=[_line(value="v" * 64)]))
    assert isinstance(ok, HealthReportMsg)
    assert len(ok.reports[0].value) == 64
    with pytest.raises(ValidationError):
        parse_agent_message(_report(reports=[_line(value="v" * 65)]))


def test_negative_policy_version_rejected() -> None:
    """`ge=0`. A missing ack reads as `policy_version = 0` (spec §2.11), so a
    negative version would sort below "never acknowledged" and make the
    reconciliation sweep's comparison meaningless."""
    with pytest.raises(ValidationError):
        parse_agent_message(_report(policy_version=-1))


# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------


def test_unknown_line_kind_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_agent_message(_report(reports=[_line(kind="flatline")]))


def test_unknown_line_state_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_agent_message(_report(reports=[_line(state="degraded")]))


def test_unavailable_is_a_legal_state() -> None:
    """Spec §0.7: absent is neither healthy nor breached. `unavailable` is a
    real third state, so the rejection test above must not be passing merely
    because the parser rejects everything it has not seen before."""
    msg = parse_agent_message(_report(reports=[_line(state="unavailable", value="")]))
    assert isinstance(msg, HealthReportMsg)
    assert msg.reports[0].state == "unavailable"


def test_extra_top_level_field_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_agent_message(_report(samples=[1, 2, 3]))


def test_extra_per_line_field_rejected() -> None:
    """The one that matters most: `extra="forbid"` on the LINE is what stops an
    agent build from smuggling a sample array in beside a legitimate edge."""
    with pytest.raises(ValidationError):
        parse_agent_message(_report(reports=[_line(history=[1.0, 2.0, 3.0])]))


# ---------------------------------------------------------------------------
# Identity comes from the certificate, never from the message (spec §5.1)
# ---------------------------------------------------------------------------


def test_no_field_in_the_payload_names_a_device() -> None:
    """`handle_agent_message` resolves `device_id`/`org_id` from the mTLS
    binding. A `device` field on this message would be a second, forgeable
    source of the same fact -- the wire-rule form of invariant #2. Asserted
    against the generated JSON schema, which is the artefact the agent in `agent/`
    actually builds its Go structs from."""
    for model in (HealthReportMsg, HealthReportLine):
        properties = model.model_json_schema()["properties"]
        assert "device" not in properties
        assert "device_id" not in properties
        assert "hostname" not in properties
        assert "org_id" not in properties


def test_health_report_carries_no_sample_series() -> None:
    """Spec §0.12 as a wire rule: no field on this message may let a consumer
    reconstruct a time series. The line model is exactly six scalars -- one
    observation, not a window of them."""
    assert set(HealthReportLine.model_json_schema()["properties"]) == {
        "check_id",
        "selector",
        "kind",
        "state",
        "value",
        "observed_at",
    }
    assert set(HealthReportMsg.model_json_schema()["properties"]) == {
        "v",
        "kind",
        "policy_version",
        "reports",
    }
