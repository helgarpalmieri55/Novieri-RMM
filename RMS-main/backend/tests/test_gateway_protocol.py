import json

import pytest
from pydantic import ValidationError

from app.gateway.protocol import (
    READ_BATCH_MAX,
    BackupReadQueryMsg,
    HeartbeatMsg,
    JobLogMsg,
    JobResultMsg,
    JobStatusMsg,
    parse_agent_message,
)


def test_parse_job_status() -> None:
    msg = parse_agent_message('{"v":1,"kind":"job_status","job_id":"j1","state":"running"}')
    assert isinstance(msg, JobStatusMsg)
    assert msg.state == "running"


def test_parse_job_log() -> None:
    msg = parse_agent_message(
        '{"v":1,"kind":"job_log","job_id":"j1","level":"info","msg":"working"}'
    )
    assert isinstance(msg, JobLogMsg)
    assert msg.msg == "working"


def test_parse_job_result_with_artifact() -> None:
    msg = parse_agent_message(
        '{"v":1,"kind":"job_result","job_id":"j1","state":"succeeded",'
        '"result":{"ok":true},"artifact_ref":"s3://b/x"}'
    )
    assert isinstance(msg, JobResultMsg)
    assert msg.result == {"ok": True}
    assert msg.artifact_ref == "s3://b/x"


def test_parse_heartbeat() -> None:
    msg = parse_agent_message('{"v":1,"kind":"heartbeat","ts":"2026-01-01T00:00:00Z"}')
    assert isinstance(msg, HeartbeatMsg)


def test_unknown_kind_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_agent_message('{"v":1,"kind":"exec_shell","cmd":"rm -rf /"}')


def test_wrong_version_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_agent_message('{"v":2,"kind":"heartbeat","ts":"2026-01-01T00:00:00Z"}')


def test_garbage_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_agent_message("not json at all")


def test_parse_security_events_batch() -> None:
    from app.gateway.protocol import SecurityEventsMsg

    msg = parse_agent_message(
        json.dumps(
            {
                "v": 1,
                "kind": "security_events",
                "events": [
                    {
                        "event_uid": "u1",
                        "ts": "2026-07-18T09:12:03Z",
                        "event_type": "logon_success",
                        "username": "TOESHEE\\jsmith",
                        "source": "win:4624",
                        "outcome": "success",
                        "raw": {"logon_type": 2},
                    },
                    {
                        "event_uid": "u2",
                        "ts": "2026-07-18T09:13:00Z",
                        "event_type": "logon_failure",
                        "outcome": "failure",
                    },
                ],
            }
        )
    )
    assert isinstance(msg, SecurityEventsMsg)
    assert len(msg.events) == 2
    assert msg.events[1].username == ""  # defaults apply


def test_security_events_batch_over_500_rejected() -> None:
    events = [
        {"event_uid": f"u{n}", "ts": "2026-07-18T00:00:00Z", "event_type": "logoff"}
        for n in range(501)
    ]
    with pytest.raises(ValidationError):
        parse_agent_message(
            json.dumps({"v": 1, "kind": "security_events", "events": events})
        )


def test_security_event_unknown_type_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_agent_message(
            json.dumps(
                {
                    "v": 1,
                    "kind": "security_events",
                    "events": [
                        {"event_uid": "u1", "ts": "2026-07-18T00:00:00Z",
                         "event_type": "ransomware_detected"}
                    ],
                }
            )
        )


def test_logs_ship_job_type_registered() -> None:
    from app.jobs.registry import get_job_type

    assert get_job_type("logs.ship").name == "logs.ship"
    assert get_job_type("logs.ship").result_handler is None


def _frame(suffixes: list[str]) -> str:
    return json.dumps(
        {"v": 1, "kind": "backup_read_query", "job_id": "j1", "query_id": "q1",
         "suffixes": suffixes}
    )


def test_a_read_query_parses_into_its_own_type() -> None:
    msg = parse_agent_message(_frame(["chunks/ab/abcd", "manifests/m1"]))
    assert isinstance(msg, BackupReadQueryMsg)
    assert msg.suffixes == ["chunks/ab/abcd", "manifests/m1"]


@pytest.mark.parametrize(
    "bad",
    [
        "../../etc/passwd",
        "chunks/../../../secrets",
        "/absolute/key",
        "chunks\\ab\\abcd",
        "chunks//ab",
        "chunks/ab/",
        "chunks/ab/ab\x00cd",
        "chunks/ab/ab\tcd",
        "",
    ],
)
def test_an_unsafe_suffix_is_rejected_at_the_wire(bad: str) -> None:
    """The FIRST of three layers, not the only one.

    `presign_backup_read` validates again with `_safe_object_suffix`. Rejecting
    here means a hostile suffix never reaches a handler at all, so the refusal
    is the same silence every other unauthorized query gets.
    """
    with pytest.raises(ValidationError):
        parse_agent_message(_frame(["chunks/ab/abcd", bad]))


def test_a_batch_over_the_cap_is_rejected_at_the_wire() -> None:
    over = [f"chunks/ab/{i:04x}" for i in range(READ_BATCH_MAX + 1)]
    with pytest.raises(ValidationError):
        parse_agent_message(_frame(over))
    # ... and the cap itself parses, so the boundary is the stated one.
    assert len(parse_agent_message(_frame(over[:READ_BATCH_MAX])).suffixes) == READ_BATCH_MAX
