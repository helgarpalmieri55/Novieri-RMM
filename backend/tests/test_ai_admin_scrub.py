"""SECURITY GATE 3: the fail-closed response scanner (spec §0.5, §7.3).

READ THIS BEFORE ADDING A TEST HERE.

1. THE UNIT TESTS BELOW ARE HOW YOU LOCALIZE A FAILURE. The test that MATTERS is
   `test_a_blocked_value_never_reaches_the_provider`, which drives the whole
   executor path against a recording fake provider and asserts the secret VALUE
   appears nowhere in what the provider was handed. Everything above it exists
   so that when that one goes red you know which clause broke.

2. THERE ARE TWO RULES NOW, AND A TEST MUST SAY WHICH ONE IT IS EXERCISING.
   The declared response contract blocks any key `registry.py` does not name;
   the key-name pattern blocks secret-shaped names, declared or not. An
   undeclared secret-shaped key is blocked TWICE, so `isinstance(outcome,
   Blocked)` alone can no longer tell you the pattern works -- delete the
   pattern and half these tests would stay green. Assert `outcome.reason`, or
   assert `looks_like_secret_key` directly.

3. NEGATIVE CASES ARE MANDATORY, NOT OPTIONAL. `token` matching `token` proves
   nothing on its own -- a plain substring match passes it. Only `tokenizer` NOT
   matching tells a whole-word rule from a substring one, and a substring rule
   blocks ordinary fields until someone turns the whole gate off.

4. THE PASS-THROUGH TEST ASSERTS DEEP EQUALITY, and it is what stops "block
   everything" from passing this file. A gate that returned `Blocked` for every
   payload satisfies every other assertion here.
"""

import inspect
import logging
from dataclasses import replace
from typing import Any

import pytest

from app.core.ai.scrub import Blocked, Ok, looks_like_secret_key, scrub
from app.core.ai.tools import REGISTRY
from app.core.ai.types import ReadTool

TOOL = REGISTRY["health.alerts"]


# --------------------------------------------------------------------------- #
# Depth and lists.
# --------------------------------------------------------------------------- #


async def test_a_secret_nested_under_a_list_is_blocked() -> None:
    """Depth AND list traversal in one payload. A dict-only walker passes a
    dict-only test and lets exactly this shape -- which is what a paged response
    looks like -- straight through."""
    outcome = scrub(TOOL, {"a": {"b": [{"secret": "hunter2"}]}})
    assert isinstance(outcome, Blocked)
    assert outcome.key_path == "a.b[0].secret"
    # The value is never carried anywhere, not even into the block record.
    assert "hunter2" not in repr(outcome)


async def test_a_secret_at_the_top_level_is_blocked() -> None:
    outcome = scrub(TOOL, {"secret": "hunter2"})
    assert isinstance(outcome, Blocked) and outcome.key_path == "secret"


async def test_a_secret_inside_a_list_of_lists_is_blocked() -> None:
    """Lists nested in lists: a walker that recursed into dicts found inside a
    list, but not into lists found inside a list, passes the test above."""
    outcome = scrub(TOOL, {"rows": [[{"password": "x"}]]})
    assert isinstance(outcome, Blocked)


# --------------------------------------------------------------------------- #
# Each pattern alternative, and its near-miss.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "key",
    [
        "secret",
        "client_secret",
        "password",
        "passphrase",
        "private_key",
        "ciphertext",
        "wrapped_dek",
        "api_key",
        "token",
        "auth_token",
        "credential_admin_value",
    ],
)
async def test_every_pattern_alternative_blocks(key: str) -> None:
    assert isinstance(scrub(TOOL, {"row": {key: "x"}}), Blocked)


@pytest.mark.parametrize(
    "key", ["Secret", "SECRET", "sEcReT", "Password", "API_KEY", "aPiKeY", "AuthToken"]
)
async def test_matching_is_case_insensitive(key: str) -> None:
    """Case must not decide the verdict, and this is the test that found the way
    it could.

    Splitting `apiKey` into words means READING the case, and a rule that reads
    the case can be defeated by nonsense case: `sEcReT` splits into `s`, `ec`,
    `re`, `t`. `_decompositions` therefore also flattens the key with the case
    thrown away, and this parametrization covers both directions -- `aPiKeY`
    needs the flattened form, `AuthToken` needs the case-split one.

    ASSERTED ON THE PREDICATE. These keys are undeclared too, so `scrub` would
    return `Blocked` for every one of them with the pattern deleted.
    """
    assert looks_like_secret_key(key) is True


@pytest.mark.parametrize("key", ["tokenizer", "tokenised_body", "detokenize"])
async def test_token_is_matched_as_a_whole_word(key: str) -> None:
    """The negative half. A plain `token` substring match passes every positive
    case above and fails here -- this is the ONLY test that can tell a whole-word
    rule from a substring one, and a substring rule blocks ordinary fields until
    somebody turns the whole gate off.

    ASSERTED ON THE PREDICATE, NOT ON `scrub`'s OUTCOME. These keys are also
    undeclared, so `scrub` blocks them for the OTHER reason -- which is checked
    here too, because a test that only looked at `Blocked` could no longer tell
    the two rules apart and would pass against a substring match.
    """
    assert looks_like_secret_key(key) is False
    outcome = scrub(TOOL, {key: "x"})
    assert isinstance(outcome, Blocked)
    assert outcome.reason == "key is not declared by the tool's response contract"


# --------------------------------------------------------------------------- #
# The measured gap (§7.3): 35 of 48 realistic secret key names used to pass.
# --------------------------------------------------------------------------- #

# Every name in the adversarial review's list, plus the four the old pattern
# named. `ALLOWED (35)` is what this file used to let through -- `apiKey`,
# `pwd`, `session_key`, `psk`, `dsn`, and the two credentials the IPAM and
# network modules actually traffic in.
SECRET_KEY_NAMES = [
    "password", "secret", "api_key", "auth_token", "refresh_token", "private_key",
    "client_secret", "clientSecret", "smtp_password", "SecretValue", "Token", "TOKEN",
    "x_token", "apiKey", "apikey", "privateKey", "accessKey", "access_key", "pwd",
    "passwd", "pass", "session_key", "shared_key", "signing_key", "encryption_key",
    "bearer", "otp", "pin", "seed", "recovery_code", "totp_seed", "tokens",
    "auth_tokens", "token2", "vault_key", "key_material", "dek", "kek", "salt",
    "nonce", "signature", "cert_key", "keypair", "psk", "wpa_key", "community_string",
    "connection_string", "dsn", "passphrase", "ciphertext", "wrapped_dek",
    "credential_admin_value",
]


@pytest.mark.parametrize("key", SECRET_KEY_NAMES)
async def test_a_realistic_secret_key_name_is_secret_shaped(key: str) -> None:
    """The DEPTH layer, on the vocabulary it was measured against.

    This asserts the PREDICATE, not `scrub`'s outcome: an undeclared key blocks
    anyway, so a test that only asserted `Blocked` here would stay green with the
    pattern deleted entirely and would tell you nothing about which layer worked.
    """
    assert looks_like_secret_key(key) is True


@pytest.mark.parametrize("key", SECRET_KEY_NAMES)
async def test_a_realistic_secret_key_name_blocks_even_when_declared(key: str) -> None:
    """AND THE DECLARATION CANNOT UNBLOCK IT. `response_fields` is a
    declaration, not an exemption: a registry entry regenerated from a model
    that has grown `password` widens the allowlist, and this is what stops that
    from widening the gate with it.
    """
    tool = replace(TOOL, response_fields=TOOL.response_fields | {key})
    outcome = scrub(tool, {"id": "a1", key: "x"})
    assert isinstance(outcome, Blocked), f"declaring {key!r} let it through: {outcome!r}"
    assert outcome.reason == "key name matches the secret-material pattern"


@pytest.mark.parametrize(
    ("key", "why"),
    [
        ("ｐassword", "fullwidth latin, folded by NFKC"),
        ("pass‍word", "zero-width joiner inside the word"),
        ("Api​Key", "zero-width space inside a camelCase name"),
    ],
)
async def test_an_invisible_or_compatibility_spelling_is_still_secret_shaped(
    key: str, why: str
) -> None:
    """All three walked past the old pattern. Normalization is what closes
    them, and it closes them for the DEPTH layer -- see the homoglyph test for
    the case normalization deliberately does not close."""
    assert looks_like_secret_key(key) is True, why


async def test_a_homoglyph_spelling_is_blocked_by_the_declared_contract() -> None:
    """THE HONEST DIVISION OF LABOUR, asserted rather than asserted-about.

    A Cyrillic `а` in `password` is NOT folded by NFKC and this module does not
    claim to catch it with the pattern -- confusable folding is a table that is
    itself a denylist. It is blocked because it is not a name `AlertOut`
    declares, which is the whole argument for making the declaration the
    control. The reason string is asserted so the test fails if the block ever
    starts coming from somewhere else.
    """
    key = "pаssword"
    assert looks_like_secret_key(key) is False
    outcome = scrub(TOOL, {"id": "a1", key: "hunter2"})
    assert isinstance(outcome, Blocked)
    assert outcome.reason == "key is not declared by the tool's response contract"
    assert outcome.key_path.endswith("ssword")


# --------------------------------------------------------------------------- #
# The declared response contract IS the control (§7.3).
# --------------------------------------------------------------------------- #


def _alert_row(**extra: Any) -> dict[str, Any]:
    """A row shaped like the `AlertOut` this tool actually returns."""
    return {
        "id": "a1",
        "device_id": "d1",
        "device_hostname": "SRV-02",
        "check_type": "disk_free",
        "selector": "C:",
        "severity": "warning",
        "state": "open",
        "observed_value": "8%",
        "threshold_display": "< 10%",
        "flapping": False,
        "cleared_reason": None,
        **extra,
    }


async def test_an_ordinary_payload_passes_through_unchanged() -> None:
    """Deep equality, so a gate that silently mangles nested data is visible.
    `Ok` alone would not see it."""
    payload = [_alert_row(), _alert_row(id="a2", device_hostname="SRV-03")]
    outcome = scrub(TOOL, payload)
    assert isinstance(outcome, Ok)
    assert outcome.payload == payload


async def test_a_key_the_tool_does_not_declare_blocks_the_whole_result() -> None:
    """THE PROPERTY THE OLD DENYLIST COULD NOT HAVE. `recovery_hint` is not
    secret-shaped by any vocabulary; it is blocked because `AlertOut` does not
    declare it, which is what "a module added a field" looks like from here.

    The assertion is on the OUTCOME AND THE PATH: a gate that blocked everything
    would satisfy `isinstance(outcome, Blocked)`, and the pass-through test above
    is what stops that from passing.
    """
    outcome = scrub(TOOL, [_alert_row(recovery_hint="ask the vendor")])
    assert isinstance(outcome, Blocked)
    assert outcome.key_path == "[0].recovery_hint"
    assert outcome.reason == "key is not declared by the tool's response contract"


async def test_an_undeclared_key_at_depth_blocks_too() -> None:
    """Nested models are declared flat, so depth is not a hole: a field added to
    `AlertOut` inside `DeviceHealthOut.open_alerts` is as blocked as one at the
    top."""
    tool = REGISTRY["devices.health"]
    payload = {
        "device_id": "d1",
        "hostname": "SRV-02",
        "open_alerts": [{"id": "a1", "vendor_note": "call us"}],
    }
    outcome = scrub(tool, payload)
    assert isinstance(outcome, Blocked)
    assert outcome.key_path == "open_alerts[0].vendor_note"


async def test_a_tool_that_declares_nothing_returns_nothing() -> None:
    """THE DEFAULT IS "BLOCK", NOT "ALLOW". A tool added to the registry without
    a `response_fields` declaration must go dark rather than open -- a reviewer
    who forgot the field is the case this default exists for."""
    tool = replace(TOOL, response_fields=frozenset())
    outcome = scrub(tool, [_alert_row()])
    assert isinstance(outcome, Blocked)
    assert outcome.reason == "key is not declared by the tool's response contract"


# --------------------------------------------------------------------------- #
# One mechanism, one outcome: there is no per-tool exemption.
# --------------------------------------------------------------------------- #


async def test_a_bypass_collection_on_the_tool_does_not_let_a_secret_through() -> None:
    """§7.3 CLAUSE 3, ASSERTED WHERE IT CAN ACTUALLY BE OBSERVED.

    `ReadTool` carried `sensitive_fields`, and `scrub` checked it BEFORE the
    pattern and skipped the key. On this exact payload that made the difference
    between `Blocked` and `Ok, payload={'id': '1'}` -- a fail-closed block turned
    into a silent drop by one word in `registry.py`, with no change to `scrub`'s
    signature and therefore nothing for the signature guard below to see.

    The frozen dataclass has no such field now, so this test ATTACHES ONE ANYWAY
    (`object.__setattr__` reaches past `frozen=True`) under each name the hatch
    has taken or would plausibly take. Re-adding the read turns this red with an
    observable value: `Ok(payload={'id': '1'})` where `Blocked` is required.
    """
    for attribute in ("sensitive_fields", "drop_fields", "allowed_secret_keys"):
        tool = replace(TOOL, response_fields=TOOL.response_fields | {"password"})
        object.__setattr__(tool, attribute, frozenset({"password"}))

        outcome = scrub(tool, {"id": "1", "password": "hunter2"})

        assert isinstance(outcome, Blocked), (
            f"{attribute!r} on the tool exempted a secret-shaped key: {outcome!r}"
        )
        assert outcome.key_path == "password"


async def test_read_tool_declares_no_field_that_could_exempt_a_key() -> None:
    """The other half: the hatch is gone from the TYPE, not only unread.

    A field on `ReadTool` is where this mechanism lives naturally -- it never
    touches `scrub`'s signature, so `test_scrub_takes_exactly_two_parameters`
    stays green while a registry entry quietly names the keys it is allowed to
    return.
    """
    import dataclasses

    names = {field.name for field in dataclasses.fields(ReadTool)}
    suspicious = {
        name
        for name in names
        if any(
            word in name.lower()
            for word in ("sensitive", "exempt", "allow", "skip", "ignore", "drop", "redact")
        )
    }
    assert suspicious == set(), f"ReadTool declares a per-tool exemption: {suspicious}"


async def test_the_scanner_never_edits_the_payload() -> None:
    """"All of it or none of it" (§7.3), asserted by IDENTITY. A scanner that
    rebuilds dicts as it walks is one `continue` away from being a redactor
    again, and a redactor is the exemption mechanism under another name."""
    payload = [_alert_row()]
    outcome = scrub(TOOL, payload)
    assert isinstance(outcome, Ok)
    assert outcome.payload is payload


# --------------------------------------------------------------------------- #
# The operator signal.
# --------------------------------------------------------------------------- #


async def test_a_block_writes_a_warning_naming_the_tool_and_the_key_path(
    caplog: Any,
) -> None:
    """"And continue the turn" (§7.3) means this log line is the ONLY
    operator-visible signal that a tool result was dropped. If it stops being
    written, the failure mode is silent degradation of answers."""
    with caplog.at_level(logging.WARNING, logger="app.core.ai.scrub"):
        scrub(TOOL, {"a": [{"private_key": "-----BEGIN"}]})
    records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(records) == 1
    message = records[0].getMessage()
    assert TOOL.name in message
    assert "a[0].private_key" in message
    # The value is never logged.
    assert "BEGIN" not in message


# --------------------------------------------------------------------------- #
# Clause 3, made mechanical.
# --------------------------------------------------------------------------- #


async def test_scrub_takes_exactly_two_parameters() -> None:
    """§7.3 CLAUSE 3 HAS NO RUNTIME OBSERVATION -- "there is no exemption list"
    cannot be asserted by calling anything, because the thing that would prove
    it broken is a parameter nobody has added yet.

    This test is the mechanism. Adding `allow=frozenset({"secret"})`, or
    `skip_scan=True`, or any other "just this once" hatch to `scrub` turns it
    red at the moment the hatch is written -- which is the only point at which
    anyone is thinking about it.
    """
    parameters = list(inspect.signature(scrub).parameters)
    assert parameters == ["tool", "payload"]


async def test_the_module_declares_no_exemption_collection() -> None:
    """The other shape the same hatch could take: a module-level allowlist that
    `scrub` consults without changing its signature.

    Asserted over the AST, not the text, for the same reason the registry's
    approved-action test is: `scrub.py`'s own docstring explains at length why
    there is no exemption parameter, and a grep-the-source guard would be red on
    its own explanation. What this looks for is a real declaration -- a binding
    whose name reads as an exemption -- or a call passing one.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(
        (
            Path(__file__).resolve().parent.parent / "app" / "core" / "ai" / "scrub.py"
        ).read_text()
    )
    suspicious = ("exempt", "allowlist", "allowed_secret", "skip_scan", "whitelist")
    offenders: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        elif isinstance(node, ast.arg):
            names = [node.arg]
        elif isinstance(node, ast.keyword) and node.arg:
            names = [node.arg]
        offenders += [n for n in names if any(s in n.lower() for s in suspicious)]
    assert offenders == [], f"scrub.py declares an exemption hatch: {offenders}"


# --------------------------------------------------------------------------- #
# THE TEST THAT MATTERS (§13 test 9).
# --------------------------------------------------------------------------- #


async def test_a_blocked_value_never_reaches_the_provider(api_env: Any, monkeypatch: Any) -> None:
    """§13 test 9, end to end through the real executor.

    A route that has quietly grown a `secret` field is simulated at the
    transport, the executor runs its normal path, and the outcome is handed to a
    RECORDING FAKE PROVIDER that captures its complete input. The assertion is
    that the secret VALUE appears nowhere in what the provider was given --
    which is the actual property, and is strictly stronger than "the outcome was
    a ToolError".

    The fake provider lives in `tests/ai_admin_fakes.py` so the provider task
    adopts it rather than writing a second one.
    """
    import json
    import time

    from app.core.ai.executor_staff import ToolError, invoke_tool
    from tests.ai_admin_fakes import (
        RecordingProvider,
        StaticJsonTransport,
        ai_user,
        install_spy,
    )
    from tests.health_fixtures import seed_org

    org = await seed_org(api_env, "aiscrub")
    _, sub = await ai_user(api_env, org.org_id, "ai.use", "health.read")
    bearer = (await api_env.headers(org.org_id, sub))["Authorization"]

    leaked = "S3CR3T-VALUE-THAT-MUST-NOT-TRAVEL"
    install_spy(
        monkeypatch,
        StaticJsonTransport(200, [{"id": "1", "detail": {"secret": leaked}}]),
    )

    outcome = await invoke_tool(
        registry=REGISTRY,
        tool_name="health.alerts",
        raw_args={},
        app=api_env.app,
        bearer=bearer,
        deadline=time.monotonic() + 30.0,
    )
    assert isinstance(outcome, ToolError)
    assert outcome.kind == "blocked"

    provider = RecordingProvider()
    # `stream_turn` is an async iterator (the shipped `ModelProvider` protocol),
    # so the capture only happens if the stream is actually consumed.
    assert [
        block
        async for block in provider.stream_turn(
            system="",
            messages=[{"role": "tool", "content": outcome.message}],
            tools=[],
            deadline=0.0,
        )
    ] == []
    assert leaked not in json.dumps(provider.seen)
    assert leaked not in outcome.message
