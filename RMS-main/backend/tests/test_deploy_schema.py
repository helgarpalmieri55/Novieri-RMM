"""Wire-contract tests for `app.deploy` / `script.run` (docs/modules/app-deploy/
spec.md §5) and the shared `InstallOutcome` contract (AD-03).

Mirrors `tests/test_patch_apply.py`'s style: construct each model from a dict,
assert `extra="forbid"` rejects an unknown key, assert invalid literals are
rejected, and assert required fields are enforced.
"""

import pytest
from pydantic import ValidationError

from app.core.wire import InstallOutcome
from app.modules.deploy.schema import (
    AppDeployParams,
    AppDeployResult,
    ScriptRunParams,
    ScriptRunResult,
)


class TestInstallOutcome:
    def test_construct_minimal(self) -> None:
        outcome = InstallOutcome(identifier="pkg-1", state="installed")
        assert outcome.identifier == "pkg-1"
        assert outcome.state == "installed"
        assert outcome.exit_code is None
        assert outcome.log == ""
        assert outcome.log_ref is None
        assert outcome.detected_after is None

    def test_construct_full(self) -> None:
        outcome = InstallOutcome(
            identifier="pkg-1",
            state="succeeded",
            exit_code=0,
            log="ok",
            log_ref="s3://bucket/key",
            detected_after=True,
        )
        assert outcome.exit_code == 0
        assert outcome.detected_after is True

    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            InstallOutcome(identifier="pkg-1", state="installed", bogus="x")

    def test_rejects_invalid_state(self) -> None:
        with pytest.raises(ValidationError):
            InstallOutcome(identifier="pkg-1", state="bogus")

    def test_requires_identifier_and_state(self) -> None:
        with pytest.raises(ValidationError):
            InstallOutcome(state="installed")
        with pytest.raises(ValidationError):
            InstallOutcome(identifier="pkg-1")


class TestAppDeployParams:
    def test_construct_minimal(self) -> None:
        params = AppDeployParams(action="install", installer_type="msi")
        assert params.action == "install"
        assert params.installer_type == "msi"
        assert params.artifact_ref is None
        assert params.artifact_sha256 is None
        assert params.install_args is None
        assert params.detection_rule == {}
        assert params.uninstall_rule == {}
        assert params.deferral_hours == 24

    def test_construct_full(self) -> None:
        params = AppDeployParams(
            action="uninstall",
            installer_type="deb",
            artifact_ref="s3://bucket/pkg.deb",
            artifact_sha256="a" * 64,
            install_args="--quiet",
            detection_rule={"path": "/usr/bin/foo"},
            uninstall_rule={"cmd": "apt remove foo"},
            deferral_hours=48,
        )
        assert params.deferral_hours == 48
        assert params.detection_rule == {"path": "/usr/bin/foo"}

    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            AppDeployParams(action="install", installer_type="msi", bogus="x")

    def test_rejects_invalid_action(self) -> None:
        with pytest.raises(ValidationError):
            AppDeployParams(action="reinstall", installer_type="msi")

    def test_rejects_invalid_installer_type(self) -> None:
        with pytest.raises(ValidationError):
            AppDeployParams(action="install", installer_type="bogus")

    def test_requires_action_and_installer_type(self) -> None:
        with pytest.raises(ValidationError):
            AppDeployParams(installer_type="msi")
        with pytest.raises(ValidationError):
            AppDeployParams(action="install")


class TestAppDeployResult:
    def test_round_trips_outcomes_list(self) -> None:
        result = AppDeployResult(
            outcomes=[
                InstallOutcome(identifier="pkg-1", state="installed", exit_code=0),
            ]
        )
        assert len(result.outcomes) == 1
        assert isinstance(result.outcomes[0], InstallOutcome)
        assert result.outcomes[0].identifier == "pkg-1"

        dumped = result.model_dump()
        rebuilt = AppDeployResult(**dumped)
        assert rebuilt == result

    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            AppDeployResult(outcomes=[], bogus="x")


class TestScriptRunParams:
    def test_construct_minimal(self) -> None:
        params = ScriptRunParams(
            interpreter="bash",
            body="echo hi",
            script_id="0198c1f4-0000-7000-8000-000000000000",
            script_version=1,
        )
        assert params.interpreter == "bash"
        assert params.timeout_s == 3600

    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            ScriptRunParams(
                interpreter="bash",
                body="echo hi",
                script_id="x",
                script_version=1,
                bogus="x",
            )

    def test_rejects_invalid_interpreter(self) -> None:
        with pytest.raises(ValidationError):
            ScriptRunParams(
                interpreter="ruby",
                body="echo hi",
                script_id="x",
                script_version=1,
            )


class TestScriptRunResult:
    def test_construct(self) -> None:
        result = ScriptRunResult(
            outcome=InstallOutcome(identifier="script-1", state="succeeded", exit_code=0)
        )
        assert result.outcome.state == "succeeded"

    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            ScriptRunResult(
                outcome=InstallOutcome(identifier="script-1", state="succeeded"),
                bogus="x",
            )
