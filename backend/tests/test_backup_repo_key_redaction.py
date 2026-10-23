"""The repository encryption key never leaves the job engine in the clear.

`backup.run` and `backup.restore` sign the repository key into their params
so the agent can encrypt and decrypt chunks. That makes the key bearer
material: it must not reach a `jobs.read` holder through the API, and it must
not stay in `jobs.params` once the job is finished (invariant 3).

A CANARY is planted as the key so each assertion is about a value that would
be plainly visible if it leaked.
"""

import json
from typing import Any

import pytest

from app.jobs.registry import get_job_type
from app.modules.backup.handler import register_job_types, strip_repo_key

register_job_types()

CANARY = "CANARY-7c2e91-backup-repo-key-must-not-leak"

RUN_PARAMS: dict[str, Any] = {
    "repository_id": "repo-1",
    "storage_ref": "org/repo-1",
    "sources": {"paths": ["C:\\Data"]},
    "dedup_params": {},
    "repo_key": CANARY,
    "snapshot": "vss",
}

RESTORE_PARAMS: dict[str, Any] = {
    "repository_id": "repo-1",
    "storage_ref": "org/repo-1",
    "manifest_ref": "m-1",
    "selection": ["C:\\Data\\a.txt"],
    "target": {"kind": "original"},
    "mode": "restore",
    "repo_key": CANARY,
}


@pytest.mark.parametrize(
    ("job_type", "params"),
    [("backup.run", RUN_PARAMS), ("backup.restore", RESTORE_PARAMS)],
)
def test_the_api_never_shows_the_repository_key(job_type: str, params: dict[str, Any]) -> None:
    redactor = get_job_type(job_type).params_redactor
    assert redactor is not None, f"{job_type} declares no params_redactor"

    out = redactor(params)

    assert CANARY not in json.dumps(out)
    assert "repo_key" not in out
    # Redaction is not a blanket: the operator still sees what the job was.
    assert out["repository_id"] == "repo-1"
    assert out["storage_ref"] == "org/repo-1"


@pytest.mark.parametrize(
    ("job_type", "params"),
    [("backup.run", RUN_PARAMS), ("backup.restore", RESTORE_PARAMS)],
)
def test_a_finished_job_keeps_no_repository_key(job_type: str, params: dict[str, Any]) -> None:
    scrubber = get_job_type(job_type).terminal_params_scrubber
    assert scrubber is not None, f"{job_type} declares no terminal_params_scrubber"

    out = scrubber(params)

    assert CANARY not in json.dumps(out)
    assert out["repository_id"] == "repo-1"


def test_stripping_does_not_mutate_the_params_the_agent_is_sent() -> None:
    """The engine signs and sends the stored params; a redactor that changed
    them in place would break the job it was only meant to hide."""
    params = dict(RUN_PARAMS)

    strip_repo_key(params)

    assert params["repo_key"] == CANARY
