"""`is_destructive` and `render_params_template` -- the two pure functions of
the shared approved-action registry (docs/modules/endpoint-health-alerting/
spec.md §8.2, §8.4).

No database. That is the point: `is_destructive` takes the exemption ROW rather
than looking it up, so this module and Phase 17's AI layer cannot develop two
answers to "may this run without a human".
"""

from typing import NamedTuple
from uuid import UUID, uuid4

import pytest

from app.core.approved_actions import (
    _DESTRUCTIVE_JOB_TYPES,
    APPROVABLE_JOB_TYPES,
    DESTRUCTIVE_PARAM_KEYS,
    NON_APPROVABLE_REASONS,
    SUBSTITUTABLE_PARAM_KEYS,
    ApprovedActionTemplateError,
    _params_make_destructive,
    is_destructive,
    non_approvable_reason,
    render_params_template,
)


class _Exemption(NamedTuple):
    """A stand-in for an `approved_action_script_exemption` row. Satisfies
    `ScriptExemptionPin` structurally, which is why that Protocol exists."""

    script_id: UUID
    script_version: int


# ---------------------------------------------------------------------------
# Clause 1 -- destructive by job type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "job_type",
    [
        "backup.restore",
        "provision.enroll",
        "app.deploy",
        "patch.apply",
        "health.policy.apply",
        "net.sweep",
    ],
)
def test_destructive_job_types(job_type: str) -> None:
    assert is_destructive(job_type, {}, None) is True


@pytest.mark.parametrize(
    "job_type", ["inventory.collect", "patch.scan", "logs.ship", "backup.run", "hello"]
)
def test_non_destructive_job_types(job_type: str) -> None:
    """The other half. Without it, `is_destructive` could `return True`
    unconditionally and every test above would still pass."""
    assert is_destructive(job_type, {}, None) is False


def test_destructive_job_types_are_all_really_registered() -> None:
    """Pinned against the LIVE registry, not against a literal list.

    A renamed or retired job type must break this test rather than silently
    disarming the gate: `_DESTRUCTIVE_JOB_TYPES` matching by string means a
    `patch.apply` renamed to `patch.install` would quietly become
    non-destructive and therefore eligible for `autonomous`.
    """
    from app.jobs import registry
    from app.worker import register_worker_job_types

    register_worker_job_types()
    assert _DESTRUCTIVE_JOB_TYPES <= set(registry.registered_names())
    assert set(SUBSTITUTABLE_PARAM_KEYS) <= set(registry.registered_names())


# ---------------------------------------------------------------------------
# The approvable allowlist -- which job types an approved action may name
# ---------------------------------------------------------------------------


def test_every_registered_job_type_has_a_decision_and_the_partition_is_total() -> None:
    """THE PROPERTY THAT MAKES THIS AN ALLOWLIST RATHER THAN "THE TWO WE THOUGHT
    OF".

    Registering a job type and forgetting to decide whether a machine may
    dispatch it must BREAK A TEST, not default to either answer. So the two sets
    are asserted to partition the live registry exactly: nothing registered is
    missing from both, and nothing in either is unregistered or in both.

    If this fails because a module registered a new job type, the fix is to read
    `APPROVABLE_JOB_TYPES`' two conditions and put the name in one set or the
    other -- not to widen the assertion. It has already earned its keep once:
    `remote.session.open` is registered by `remote.register(app)` rather than by
    `register_worker_job_types()`, so it was invisible to this module in
    isolation and this assertion is what surfaced it.

    THE REGISTRY IS PROCESS-GLOBAL, so `registered_names()` depends on which
    modules some earlier test happened to import. Both registration paths are
    therefore called explicitly here: the answer must not change with test
    ordering, and a partition assertion that passed only when run alone would be
    exactly the silence it exists to break.
    """
    from app.jobs import registry
    from app.modules.remote.jobtypes import register_job_type as register_remote_job_type
    from app.worker import register_worker_job_types

    register_worker_job_types()
    register_remote_job_type()
    registered = set(registry.registered_names())
    decided = APPROVABLE_JOB_TYPES | set(NON_APPROVABLE_REASONS)

    assert registered - decided == set(), (
        "these registered job types have no approvability decision; deny-by-default "
        "already refuses them, but the reason an operator is shown would be the generic one"
    )
    from app.jobs.direct_dispatch import RESERVED_JOB_TYPES

    assert decided - registered == RESERVED_JOB_TYPES, (
        "a named job type is neither registered nor reserved. See `RESERVED_JOB_TYPES`' own "
        "comment: the two push-install types are refused by both kernel gates before they "
        "exist, because closing that hole after the job type exists is what went wrong for "
        "`net.sweep`. Anything else here is a typo in one of the two sets"
    )
    assert APPROVABLE_JOB_TYPES & set(NON_APPROVABLE_REASONS) == set()


@pytest.mark.parametrize(
    "job_type",
    [
        "net.sweep",
        "health.policy.apply",
        "backup.run",
        "backup.restore",
        "provision.enroll",
        "remote.session.open",
    ],
)
def test_the_non_approvable_job_types_are_refused_with_a_reason(job_type: str) -> None:
    """Each refusal SEPARATELY, and each asserted to name the authorization it
    is protecting -- a single "it is refused" test cannot tell a deliberate
    entry from a typo in the set, and a reason an operator cannot act on sends
    them looking for a way round it.

    `net.sweep` is the one that was verified live: an approved action naming it
    reached `JobEngine.create` with the operator's own template, so packets went
    out with zero `ipam_scan_authorization` rows in the org, no `ipam_sweep_run`
    record and no `ipam.sweep_dispatched` audit event.
    """
    reason = non_approvable_reason(job_type)
    assert reason is not None, f"{job_type} must not be nameable by an approved action"
    assert len(reason) > 40


def test_an_unknown_job_type_is_refused_by_default() -> None:
    """DENY BY DEFAULT is the whole design. A job type nobody has classified --
    a module's new one, a typo -- is refused, and the refusal explains the two
    conditions rather than saying "no"."""
    reason = non_approvable_reason("service.restart")
    assert reason is not None
    assert "allowlist" in reason


def test_the_approvable_types_are_approvable() -> None:
    """The other half: without it, `non_approvable_reason` could return a string
    unconditionally and every test above would still pass, with the whole
    remediation facility dead."""
    assert [t for t in sorted(APPROVABLE_JOB_TYPES) if non_approvable_reason(t) is not None] == []


# ---------------------------------------------------------------------------
# Clause 2 -- arbitrary code is destructive by default
# ---------------------------------------------------------------------------


def test_script_run_without_an_exemption_is_destructive() -> None:
    """§8.4 clause 2. The server cannot know what a PowerShell body does, and
    pattern-matching bodies for dangerous verbs is theatre that fails on the
    first base64 blob."""
    script_id = uuid4()
    assert (
        is_destructive("script.run", {}, None, script_id=script_id, script_version=3) is True
    )


def test_script_run_with_an_exactly_matching_exemption_is_not_destructive() -> None:
    script_id = uuid4()
    exemption = _Exemption(script_id=script_id, script_version=3)
    assert (
        is_destructive("script.run", {}, exemption, script_id=script_id, script_version=3)
        is False
    )


def test_an_exemption_for_another_version_does_not_follow_the_script() -> None:
    """§2.10's load-bearing property. `update_script` bumps `version` IN PLACE
    on the same row and the previous body is not retained anywhere, so a
    script's id is not a version identifier: an exemption keyed on the id alone
    would silently follow every future edit -- exactly the "approve whatever
    that script says tomorrow" failure the pinning exists to prevent."""
    script_id = uuid4()
    stale = _Exemption(script_id=script_id, script_version=2)
    assert (
        is_destructive("script.run", {}, stale, script_id=script_id, script_version=3) is True
    )


def test_an_exemption_for_another_script_does_not_transfer() -> None:
    other = _Exemption(script_id=uuid4(), script_version=3)
    assert (
        is_destructive("script.run", {}, other, script_id=uuid4(), script_version=3) is True
    )


def test_script_run_with_no_pin_at_all_fails_closed() -> None:
    """A `script.run` action with no pinned pair can match no exemption. The
    database CHECK makes such a row unrepresentable; if one ever existed the
    safe answer is "needs a human", not "run it"."""
    exemption = _Exemption(script_id=uuid4(), script_version=1)
    assert is_destructive("script.run", {}, exemption) is True
    assert is_destructive("script.run", {}, None) is True


# ---------------------------------------------------------------------------
# Clause 3 -- destructive by params
# ---------------------------------------------------------------------------


def test_destructive_param_keys_map_values_not_key_names() -> None:
    """§8.4's amendment, pinned as data. A map of key NAMES would mark every
    `patch.apply` action destructive INCLUDING `reboot: "never"`, and an
    over-broad gate is one that gets argued away. `PatchApplyParams.reboot` is
    `Literal["never","if_required","forced"]`, not a boolean."""
    assert DESTRUCTIVE_PARAM_KEYS["patch.apply"]["reboot"] == frozenset(
        {"if_required", "forced"}
    )
    assert "never" not in DESTRUCTIVE_PARAM_KEYS["patch.apply"]["reboot"]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"reboot": "forced"}, True),
        ({"reboot": "if_required"}, True),
        ({"reboot": "never"}, False),
        ({}, False),
    ],
)
def test_params_clause_is_pinned_on_its_own(params: dict[str, str], expected: bool) -> None:
    """DELIBERATELY NOT THROUGH `is_destructive`.

    `patch.apply` is the only job type in `DESTRUCTIVE_PARAM_KEYS` and it is
    ALSO in `_DESTRUCTIVE_JOB_TYPES`, so `is_destructive("patch.apply",
    {"reboot": "never"}, None)` is `True` by clause 1 whatever clause 3 does --
    a test written through the public function would be satisfied by clause 1
    and would pass with this entire clause deleted. Exercising
    `_params_make_destructive` directly is what makes the two paths produce
    different observable values.
    """
    assert _params_make_destructive("patch.apply", params) is expected


def test_params_clause_is_inert_for_job_types_not_in_the_map() -> None:
    assert _params_make_destructive("backup.run", {"reboot": "forced"}) is False


# ---------------------------------------------------------------------------
# render_params_template (§8.2)
# ---------------------------------------------------------------------------


def test_every_substitutable_entry_is_empty_in_v1() -> None:
    """A finding about the code, not an omission (§8.2): no registered job type
    has a params key a device id or a selector may be written into.
    `ScriptRunParams` is (interpreter, body, script_id, script_version,
    timeout_s) -- there is NO arguments field."""
    assert all(keys == frozenset() for keys in SUBSTITUTABLE_PARAM_KEYS.values())


def test_a_template_with_no_tokens_renders_unchanged() -> None:
    template = {"reboot": "never", "patches": ["KB1", "KB2"], "deferral_hours": 4}
    rendered = render_params_template(
        "patch.apply", template, device_id=uuid4(), selector="C:"
    )
    assert rendered == template


def test_a_token_in_a_key_that_does_not_permit_it_raises() -> None:
    """Every entry is empty in v1, so every token raises -- at WRITE time, which
    is the whole point: the API returns 400 rather than a `KeyError` at 03:00
    with an alert already open."""
    with pytest.raises(ApprovedActionTemplateError):
        render_params_template(
            "script.run", {"timeout_s": "{device_id}"}, device_id=uuid4(), selector="C:"
        )


def test_a_token_for_an_unmapped_job_type_raises() -> None:
    with pytest.raises(ApprovedActionTemplateError):
        render_params_template(
            "remote.session.open", {"x": "{selector}"}, device_id=uuid4(), selector="C:"
        )


def test_an_unrecognised_brace_token_raises_rather_than_formatting() -> None:
    """`str.format` is forbidden here for three separate reasons, and this is
    the one an attacker uses: `format` resolves attribute access, so
    `"{0.__class__.__init__.__globals__}"` in an org-admin-authored template
    walks whatever object is passed into a process holding the job signing key.
    The structural walk never interprets it -- it refuses it."""
    with pytest.raises(ApprovedActionTemplateError):
        render_params_template(
            "patch.apply",
            {"reboot": "{0.__class__.__init__.__globals__}"},
            device_id=uuid4(),
            selector="C:",
        )


def test_braces_are_refused_at_every_depth() -> None:
    """The walk recurses. A token hidden inside a nested list must not slip
    through because the top level looked clean."""
    with pytest.raises(ApprovedActionTemplateError):
        render_params_template(
            "patch.apply",
            {"patches": ["KB1", {"id": "{device_id}"}]},
            device_id=uuid4(),
            selector="C:",
        )


def test_a_selector_containing_braces_is_data_not_a_template() -> None:
    """A Windows volume path is `\\\\?\\Volume{GUID}\\` and `disk.free_pct` takes
    a volume as its selector. Under `str.format` that selector is a formatter
    exception; here it is a SUBSTITUTED VALUE and is never re-scanned.

    Demonstrated on a temporarily-permissive map rather than by weakening the
    real one -- v1 permits no substitutions at all, and this behaviour must be
    built and tested before the first job type needs it (§8.2)."""
    device_id = uuid4()
    original = SUBSTITUTABLE_PARAM_KEYS["patch.apply"]
    SUBSTITUTABLE_PARAM_KEYS["patch.apply"] = frozenset({"deferral_hours"})
    try:
        rendered = render_params_template(
            "patch.apply",
            {"reboot": "never", "deferral_hours": "{selector}"},
            device_id=device_id,
            selector="\\\\?\\Volume{2f3c}\\",
        )
    finally:
        SUBSTITUTABLE_PARAM_KEYS["patch.apply"] = original
    assert rendered == {"reboot": "never", "deferral_hours": "\\\\?\\Volume{2f3c}\\"}


def test_device_id_substitutes_only_where_permitted() -> None:
    device_id = uuid4()
    original = SUBSTITUTABLE_PARAM_KEYS["patch.apply"]
    SUBSTITUTABLE_PARAM_KEYS["patch.apply"] = frozenset({"deferral_hours"})
    try:
        rendered = render_params_template(
            "patch.apply",
            {"deferral_hours": "{device_id}"},
            device_id=device_id,
            selector="C:",
        )
        with pytest.raises(ApprovedActionTemplateError):
            render_params_template(
                "patch.apply",
                {"reboot": "{device_id}"},
                device_id=device_id,
                selector="C:",
            )
    finally:
        SUBSTITUTABLE_PARAM_KEYS["patch.apply"] = original
    assert rendered == {"deferral_hours": str(device_id)}
