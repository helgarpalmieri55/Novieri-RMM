"""Wire-contract tests for `provision.enroll` and the `provision.prepare`
site-server envelope (docs/modules/provisioning/spec.md).

Mirrors `tests/test_backup_schema.py`'s style: construct each model from a
dict, assert `extra="forbid"` rejects an unknown key, assert invalid
literals are rejected, and assert required fields are enforced.
"""

import pytest
from pydantic import ValidationError

from app.modules.provisioning.schema import (
    ProvisionEnrollParams,
    ProvisionEnrollResult,
    ProvisionPrepareEnvelope,
)


class TestProvisionEnrollParams:
    def test_construct(self) -> None:
        params = ProvisionEnrollParams(
            provision_run_id="0198c1f4-0000-7000-8000-000000000000",
            os_family="windows",
            hostname="ws-0001",
            post_config={"domain": "corp.local", "ou": "OU=Workstations,DC=corp,DC=local"},
        )
        assert params.os_family == "windows"
        assert params.expected_agent_version is None

    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            ProvisionEnrollParams(
                provision_run_id="run-1",
                os_family="windows",
                hostname="ws-0001",
                post_config={},
                bogus="x",
            )

    def test_rejects_invalid_os_family(self) -> None:
        with pytest.raises(ValidationError):
            ProvisionEnrollParams(
                provision_run_id="run-1",
                os_family="freebsd",
                hostname="ws-0001",
                post_config={},
            )


class TestProvisionEnrollResult:
    def test_construct_defaults(self) -> None:
        result = ProvisionEnrollResult(state="success", hostname_set=True, agent_healthy=True)
        assert result.domain_joined is None
        assert result.error is None

    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            ProvisionEnrollResult(
                state="success",
                hostname_set=True,
                agent_healthy=True,
                bogus="x",
            )

    def test_rejects_invalid_state(self) -> None:
        with pytest.raises(ValidationError):
            ProvisionEnrollResult(state="partial", hostname_set=True, agent_healthy=True)

    def test_requires_state(self) -> None:
        with pytest.raises(ValidationError):
            ProvisionEnrollResult(hostname_set=True, agent_healthy=True)  # type: ignore[call-arg]

    def test_requires_hostname_set(self) -> None:
        with pytest.raises(ValidationError):
            ProvisionEnrollResult(state="success", agent_healthy=True)  # type: ignore[call-arg]

    def test_requires_agent_healthy(self) -> None:
        with pytest.raises(ValidationError):
            ProvisionEnrollResult(state="success", hostname_set=True)  # type: ignore[call-arg]


class TestProvisionPrepareEnvelope:
    def test_construct(self) -> None:
        envelope = ProvisionPrepareEnvelope(
            provision_run_id="run-1",
            os_family="linux",
            image_ref="org/images/ubuntu-24.04",
            image_sha256="a" * 64,
            answer_ref="org/answers/run-1",
            target_ref="00:11:22:33:44:55",
            wake_on_lan={"mac": "00:11:22:33:44:55"},
        )
        assert envelope.os_family == "linux"
        assert envelope.wake_on_lan == {"mac": "00:11:22:33:44:55"}

    def test_allows_null_optional_refs(self) -> None:
        envelope = ProvisionPrepareEnvelope(
            provision_run_id="run-1",
            os_family="macos",
            image_ref=None,
            image_sha256=None,
            answer_ref=None,
            target_ref=None,
            wake_on_lan={},
        )
        assert envelope.image_ref is None

    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            ProvisionPrepareEnvelope(
                provision_run_id="run-1",
                os_family="linux",
                image_ref=None,
                image_sha256=None,
                answer_ref=None,
                target_ref=None,
                wake_on_lan={},
                bogus="x",
            )

    def test_rejects_invalid_os_family(self) -> None:
        with pytest.raises(ValidationError):
            ProvisionPrepareEnvelope(
                provision_run_id="run-1",
                os_family="freebsd",
                image_ref=None,
                image_sha256=None,
                answer_ref=None,
                target_ref=None,
                wake_on_lan={},
            )
