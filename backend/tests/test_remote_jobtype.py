import pytest
from pydantic import ValidationError

from app.jobs.registry import get_job_type
from app.modules.remote.jobtypes import RemoteSessionOpenParams, register_job_type


def test_params_validate_and_reject_extra() -> None:
    p = RemoteSessionOpenParams(
        session_id="s", protocol="rdp", local_port=3389,
        relay_url="wss://gw/relay", relay_token="t", consent_mode="unattended",
    )
    assert p.protocol == "rdp"
    with pytest.raises(ValidationError):
        RemoteSessionOpenParams(
            session_id="s", protocol="telnet", local_port=3389,  # bad protocol
            relay_url="x", relay_token="t", consent_mode="unattended",
        )


def test_job_type_registered() -> None:
    register_job_type()
    jt = get_job_type("remote.session.open")
    assert jt.params_model is RemoteSessionOpenParams and jt.result_handler is None
