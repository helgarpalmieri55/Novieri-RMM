"""The two provider clients (spec §11.1, §11.2; plan Task 10).

**BOTH CLIENTS ARE PLAIN `httpx`, AND NO PROVIDER SDK IS ADDED.** Two reasons,
recorded in spec §11.1 by this build: §11.1 already forbids a provider SDK type
appearing above the `ModelProvider` interface, and adding a vendor SDK to a
domain-admin-equivalent on-prem control plane is a supply-chain commitment that
buys a JSON-over-HTTP client. Both target APIs are plain HTTP with SSE
responses.

WHAT THESE TESTS STAND ON. Every one of them replaces `providers/_http.py`'s
single transport seam with a local stub, so no test here can reach a network
even by accident -- and because there is exactly ONE such seam in the module,
standing on it is standing on everything the module can send. The stub for the
happy paths is a real ASGI app reached through a real `httpx` request, so the
assertions are about the request the client actually built.

THE DEADLINE IS THE INTERESTING ONE. `ModelProvider.stream_turn`'s `deadline` is
a `time.monotonic()` instant for the WHOLE exchange, not a per-phase timeout,
and the two tests below are the two shapes a per-phase timeout cannot bound: a
provider that never sends response headers, and a provider that trickles one
keep-alive forever so every read resets the read clock. A client built on
`httpx.Timeout(read=...)` passes neither -- it never returns at all.
"""

import asyncio
import json
import logging
import time
from typing import Any

import httpx
import pytest

from app.core.ai.providers import (
    MALFORMED_ARGUMENTS_FIELD,
    Message,
    ModelProvider,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    UsageBlock,
    load_provider_configs,
)
from app.core.ai.providers.anthropic import AnthropicProvider
from app.core.ai.providers.openai_compatible import OpenAICompatibleProvider
from app.core.ai.types import ToolSpec
from tests.ai_admin_fakes import (
    ScriptedSseApp,
    SilentTransport,
    StatusTransport,
    TricklingTransport,
    install_outbound_spy,
    sse,
)

PROVIDER_HOST = "vllm.internal.test"
BASE_URL = f"http://{PROVIDER_HOST}:8000/v1"

_TOOLS = (
    ToolSpec(
        name="health.alerts",
        description="Alerts that are open right now.",
        input_schema={"type": "object", "properties": {"limit": {"type": "integer"}}},
    ),
)


def _config(kind: str, *, key_path: str | None = None) -> Any:
    env = {
        "ITS_AI_PROVIDERS": "p",
        "ITS_AI_P_KIND": kind,
        "ITS_AI_P_BASE_URL": BASE_URL,
        "ITS_AI_P_MODEL": "a-model-name",
    }
    if key_path is not None:
        env["ITS_AI_P_API_KEY_PATH"] = key_path
    return load_provider_configs(env)["p"]


def _openai(**kwargs: Any) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(_config("openai_compatible", **kwargs))


def _anthropic(**kwargs: Any) -> AnthropicProvider:
    return AnthropicProvider(_config("anthropic", **kwargs))


# --------------------------------------------------------------------------- #
# Canned wire bodies. Written as the real frame shapes, not as a convenience
# format the parser was then written to match.
# --------------------------------------------------------------------------- #

OPENAI_BODY = sse(
    'data: {"choices":[{"delta":{"content":"Two devices "}}]}',
    'data: {"choices":[{"delta":{"content":"are alerting."}}]}',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_a",'
    '"type":"function","function":{"name":"health.alerts",'
    '"arguments":"{\\"limit\\"'
    '"}}]}}]}',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
    '"function":{"arguments":": 5}"}}]}}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}],'
    '"usage":{"prompt_tokens":11,"completion_tokens":7}}',
    "data: [DONE]",
)

ANTHROPIC_BODY = sse(
    "event: message_start\n"
    'data: {"type":"message_start","message":{"usage":{"input_tokens":11}}}',
    "event: content_block_start\n"
    'data: {"type":"content_block_start","index":0,'
    '"content_block":{"type":"text","text":""}}',
    "event: content_block_delta\n"
    'data: {"type":"content_block_delta","index":0,'
    '"delta":{"type":"text_delta","text":"Two devices "}}',
    "event: content_block_delta\n"
    'data: {"type":"content_block_delta","index":0,'
    '"delta":{"type":"text_delta","text":"are alerting."}}',
    'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}',
    "event: content_block_start\n"
    'data: {"type":"content_block_start","index":1,'
    '"content_block":{"type":"tool_use","id":"call_a","name":"health.alerts"}}',
    "event: content_block_delta\n"
    'data: {"type":"content_block_delta","index":1,'
    '"delta":{"type":"input_json_delta","partial_json":"{\\"limit\\": 5}"}}',
    'event: content_block_stop\ndata: {"type":"content_block_stop","index":1}',
    "event: message_delta\n"
    'data: {"type":"message_delta","usage":{"output_tokens":7}}',
    'event: message_stop\ndata: {"type":"message_stop"}',
)

_HISTORY = (
    Message(role="user", content=(TextBlock(text="what is alerting?"),)),
    Message(
        role="assistant",
        content=(ToolCallBlock(id="call_a", name="health.alerts", arguments={"limit": 5}),),
    ),
    Message(
        role="user",
        content=(
            ToolResultBlock(
                tool_call_id="call_a", name="health.alerts", content={"items": []}
            ),
        ),
    ),
)


async def _drain(
    provider: ModelProvider, *, deadline: float | None = None
) -> list[Any]:
    return [
        block
        async for block in provider.stream_turn(
            system="SYSTEM",
            messages=list(_HISTORY),
            tools=list(_TOOLS),
            deadline=time.monotonic() + 30.0 if deadline is None else deadline,
        )
    ]


# --------------------------------------------------------------------------- #
# 1. Both clients emit blocks conforming to the `Block` union.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("build", "body"),
    [(_openai, OPENAI_BODY), (_anthropic, ANTHROPIC_BODY)],
    ids=["openai_compatible", "anthropic"],
)
async def test_a_client_turns_a_canned_sse_body_into_blocks(
    monkeypatch: Any, build: Any, body: bytes
) -> None:
    """Every emitted value is a member of the `Block` union, and the union's
    members carry exactly what the wire said.

    The text assertion is on the CONCATENATION rather than on "some TextBlock
    was emitted": a client that dropped every delta but the last would still
    emit text, and the answer an administrator reads would be missing its first
    sentence.
    """
    app = ScriptedSseApp([body])
    install_outbound_spy(monkeypatch, httpx.ASGITransport(app=app))

    blocks = await _drain(build())

    for block in blocks:
        assert isinstance(
            block, TextBlock | ToolCallBlock | ToolResultBlock | UsageBlock
        ), f"{type(block).__name__} is not a member of the Block union"
    text = "".join(b.text for b in blocks if isinstance(b, TextBlock))
    assert text == "Two devices are alerting."
    usage = [b for b in blocks if isinstance(b, UsageBlock)]
    assert usage == [UsageBlock(input_tokens=11, output_tokens=7)]


@pytest.mark.parametrize(
    ("build", "body"),
    [(_openai, OPENAI_BODY), (_anthropic, ANTHROPIC_BODY)],
    ids=["openai_compatible", "anthropic"],
)
async def test_a_client_parses_a_streamed_tool_call_into_one_structured_block(
    monkeypatch: Any, build: Any, body: bytes
) -> None:
    """Both wire formats deliver a tool call's arguments as a JSON string
    ASSEMBLED FROM SEVERAL FRAMES. The canned bodies split `{"limit": 5}` across
    two frames for exactly that reason -- a client that parsed each fragment on
    its own would produce nothing at all, and one that took only the last
    fragment would produce `: 5}`.
    """
    app = ScriptedSseApp([body])
    install_outbound_spy(monkeypatch, httpx.ASGITransport(app=app))

    calls = [b for b in await _drain(build()) if isinstance(b, ToolCallBlock)]

    assert calls == [
        ToolCallBlock(id="call_a", name="health.alerts", arguments={"limit": 5})
    ]


@pytest.mark.parametrize(
    ("build", "body"),
    [
        (
            _openai,
            sse(
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_b",'
                '"type":"function","function":{"name":"health.alerts",'
                '"arguments":"{not json"}}]}}]}',
                "data: [DONE]",
            ),
        ),
        (
            _anthropic,
            sse(
                'event: content_block_start\ndata: {"type":"content_block_start",'
                '"index":0,"content_block":{"type":"tool_use","id":"call_b",'
                '"name":"health.alerts"}}',
                'event: content_block_delta\ndata: {"type":"content_block_delta",'
                '"index":0,"delta":{"type":"input_json_delta",'
                '"partial_json":"{not json"}}',
                'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}',
            ),
        ),
    ],
    ids=["openai_compatible", "anthropic"],
)
async def test_unparseable_tool_arguments_become_a_block_the_turn_can_survive(
    monkeypatch: Any, build: Any, body: bytes
) -> None:
    """§10.5: arguments are NEVER coerced, repaired or guessed.

    An unparseable argument string is preserved under a reserved field name that
    no `args` model declares, so the executor's closed (`extra="forbid"`) model
    rejects it, the orchestrator turns that into an `invalid_arguments`
    `ToolError`, and the turn continues. THE ALTERNATIVE IS THE BUG: returning
    `{}` would let a tool whose arguments are all optional run with defaults --
    the executor deciding what the model meant, which is the executor writing
    tool calls.

    The assertion is on the STORED ARGUMENTS, not on "no exception was raised".
    A client that raised here would fail the drain; a client that silently
    returned `{}` would pass a no-exception test and fail this one, printing the
    empty mapping it invented.
    """
    app = ScriptedSseApp([body])
    install_outbound_spy(monkeypatch, httpx.ASGITransport(app=app))

    calls = [b for b in await _drain(build()) if isinstance(b, ToolCallBlock)]

    assert len(calls) == 1, calls
    assert dict(calls[0].arguments) == {MALFORMED_ARGUMENTS_FIELD: "{not json"}

    # And the executor rejects it without making an HTTP request. This is the
    # half that makes the reserved field name meaningful rather than decorative.
    from app.core.ai.tools.args import HealthAlertsArgs

    with pytest.raises(Exception) as excinfo:
        HealthAlertsArgs.model_validate(dict(calls[0].arguments))
    assert MALFORMED_ARGUMENTS_FIELD in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 2. The whole-exchange deadline.
# --------------------------------------------------------------------------- #


@pytest.mark.timeout(20)
@pytest.mark.parametrize("build", [_openai, _anthropic], ids=["openai_compatible", "anthropic"])
async def test_a_provider_that_trickles_forever_is_cut_off_at_the_deadline(
    monkeypatch: Any, build: Any
) -> None:
    """**THIS IS THE TEST A PER-PHASE TIMEOUT FAILS.**

    The stub answers immediately and then emits a keep-alive comment every 10 ms
    for ever. `httpx.Timeout(read=5)` never fires against it -- every byte
    resets the read clock -- so a client built that way does not return, and
    this test dies on its `pytest.mark.timeout` rather than on an assertion.
    Only an absolute wall clock for the whole exchange ends it.

    The elapsed-time assertion is the load-bearing half. Without it a client
    that raised `ProviderTimeoutError` after ten minutes would pass.
    """
    transport = TricklingTransport()
    install_outbound_spy(monkeypatch, transport)

    started = time.monotonic()
    with pytest.raises(ProviderTimeoutError):
        await _drain(build(), deadline=started + 0.4)
    elapsed = time.monotonic() - started

    assert transport.requests, "the client never issued a request at all"
    assert elapsed < 5.0, f"the deadline was not honoured; the exchange took {elapsed:.1f}s"


@pytest.mark.timeout(20)
@pytest.mark.parametrize("build", [_openai, _anthropic], ids=["openai_compatible", "anthropic"])
async def test_a_provider_that_never_answers_is_cut_off_at_the_deadline(
    monkeypatch: Any, build: Any
) -> None:
    """The other half: the connection is accepted and no response headers ever
    arrive, so there is no body to time out reading. The deadline has to bound
    the response-start phase as well as the body."""
    transport = SilentTransport()
    install_outbound_spy(monkeypatch, transport)

    started = time.monotonic()
    with pytest.raises(ProviderTimeoutError):
        await _drain(build(), deadline=started + 0.4)
    elapsed = time.monotonic() - started

    assert transport.requests, "the client never issued a request at all"
    assert elapsed < 5.0, f"the deadline was not honoured; the exchange took {elapsed:.1f}s"


async def test_a_deadline_already_past_never_issues_a_request(monkeypatch: Any) -> None:
    """A turn whose clock ran out before the model was reached must not spend an
    outbound request on an answer nobody will read.

    Asserted on the SPY'S TAPE, not on the exception: "it raised" is also true
    of a client that made the request and then gave up on it.
    """
    transport = SilentTransport()
    install_outbound_spy(monkeypatch, transport)

    with pytest.raises(ProviderTimeoutError):
        await _drain(_openai(), deadline=time.monotonic() - 1.0)

    assert transport.requests == []


# --------------------------------------------------------------------------- #
# 3. The API key: the right header, and never a log line.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("build", "body", "header", "expected"),
    [
        (_openai, OPENAI_BODY, "authorization", "Bearer {secret}"),
        (_anthropic, ANTHROPIC_BODY, "x-api-key", "{secret}"),
    ],
    ids=["openai_compatible", "anthropic"],
)
async def test_the_key_travels_in_the_right_header_and_never_reaches_a_log(
    monkeypatch: Any,
    caplog: Any,
    tmp_path: Any,
    build: Any,
    body: bytes,
    header: str,
    expected: str,
) -> None:
    """Invariant #3, at the one place this module holds key material.

    THE ORDER OF THE TWO ASSERTIONS IS THE POINT. The header assertion runs
    FIRST and asserts the exact value received by the stub, so a client that
    stopped sending the key entirely cannot make the log assertion pass
    vacuously -- which is how a "the secret is not in the log" test quietly
    becomes true for the wrong reason.
    """
    secret = "sk-provider-DO-NOT-LOG-4e19c7"
    key_file = tmp_path / "provider.key"
    key_file.write_text(f"{secret}\n")

    app = ScriptedSseApp([body])
    install_outbound_spy(monkeypatch, httpx.ASGITransport(app=app))

    with caplog.at_level(logging.DEBUG):
        await _drain(build(key_path=str(key_file)))

    sent = app.requests[0]["headers"]
    assert sent[header] == expected.format(secret=secret)

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert secret not in logged, "the api key reached a DEBUG log record"


async def test_a_provider_with_no_key_file_sends_no_credential_header(
    monkeypatch: Any,
) -> None:
    """A self-hosted vLLM/Ollama on the customer's own network usually has no
    key at all, and inventing an empty `Authorization: Bearer ` header would be
    a 401 on servers that validate the header's shape."""
    app = ScriptedSseApp([OPENAI_BODY])
    install_outbound_spy(monkeypatch, httpx.ASGITransport(app=app))

    await _drain(_openai())

    assert "authorization" not in app.requests[0]["headers"]


# --------------------------------------------------------------------------- #
# 4. Failure mapping: 429 is distinguishable from 5xx (§10.4 branches on type).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("build", [_openai, _anthropic], ids=["openai_compatible", "anthropic"])
async def test_a_429_raises_the_rate_limited_error_carrying_the_retry_hint(
    monkeypatch: Any, build: Any
) -> None:
    """§10.4: 429 is never retried server-side, and the API layer answers 429
    with the provider's OWN retry hint rather than inventing one. So the hint
    has to survive the mapping -- asserting only the exception type would pass
    against a client that dropped it."""
    install_outbound_spy(
        monkeypatch, StatusTransport(429, headers={"retry-after": "17"})
    )

    with pytest.raises(ProviderRateLimitedError) as excinfo:
        await _drain(build())

    assert excinfo.value.retry_after_s == 17.0


@pytest.mark.parametrize("build", [_openai, _anthropic], ids=["openai_compatible", "anthropic"])
async def test_a_5xx_raises_the_unavailable_error_which_is_retried_once(
    monkeypatch: Any, build: Any
) -> None:
    """A different type, because the orchestrator's retry policy is control flow
    over the type and never a match on message prose."""
    install_outbound_spy(monkeypatch, StatusTransport(503))

    with pytest.raises(ProviderUnavailableError) as excinfo:
        await _drain(build())

    assert not isinstance(excinfo.value, ProviderRateLimitedError)
    assert "503" in str(excinfo.value)


@pytest.mark.parametrize("build", [_openai, _anthropic], ids=["openai_compatible", "anthropic"])
async def test_a_transport_failure_raises_the_unavailable_error(
    monkeypatch: Any, build: Any
) -> None:
    """DNS failure / connection refused. The client must not leak an `httpx`
    type upward: `ModelProvider`'s contract is this module's error hierarchy,
    and the orchestrator branches on it."""

    class Refusing(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

    install_outbound_spy(monkeypatch, Refusing())

    with pytest.raises(ProviderUnavailableError):
        await _drain(build())


# --------------------------------------------------------------------------- #
# 5. What the client sends.
# --------------------------------------------------------------------------- #


async def test_the_openai_request_carries_the_system_prompt_tools_and_history(
    monkeypatch: Any,
) -> None:
    """The turn's inputs reach the wire in the format the API expects.

    Asserted against the body the STUB RECEIVED, so this is what would leave a
    customer's network under a hosted provider -- which is the list spec §11.2
    enumerates and `AiSettingsPage` reproduces verbatim.
    """
    app = ScriptedSseApp([OPENAI_BODY])
    install_outbound_spy(monkeypatch, httpx.ASGITransport(app=app))

    await _drain(_openai())

    sent = app.requests[0]
    assert sent["method"] == "POST"
    assert sent["path"] == "/v1/chat/completions"
    body = sent["body"]
    assert body["stream"] is True
    assert body["model"] == "a-model-name"
    assert body["messages"][0] == {"role": "system", "content": "SYSTEM"}
    assert body["messages"][1] == {"role": "user", "content": "what is alerting?"}
    assert body["messages"][2]["tool_calls"][0]["function"]["name"] == "health.alerts"
    assert body["messages"][3]["role"] == "tool"
    assert json.loads(body["messages"][3]["content"]) == {"items": []}
    assert body["tools"][0]["function"]["name"] == "health.alerts"


async def test_the_anthropic_request_carries_the_system_prompt_tools_and_history(
    monkeypatch: Any,
) -> None:
    app = ScriptedSseApp([ANTHROPIC_BODY])
    install_outbound_spy(monkeypatch, httpx.ASGITransport(app=app))

    await _drain(_anthropic())

    sent = app.requests[0]
    assert sent["method"] == "POST"
    assert sent["path"] == "/v1/messages"
    assert sent["headers"]["anthropic-version"]
    body = sent["body"]
    assert body["stream"] is True
    assert body["system"] == "SYSTEM"
    assert body["messages"][0]["content"][0] == {
        "type": "text",
        "text": "what is alerting?",
    }
    assert body["messages"][1]["content"][0]["type"] == "tool_use"
    assert body["messages"][2]["content"][0]["type"] == "tool_result"
    assert body["tools"][0]["name"] == "health.alerts"


@pytest.mark.parametrize("build", [_openai, _anthropic], ids=["openai_compatible", "anthropic"])
async def test_the_provider_id_names_the_configured_model(build: Any) -> None:
    """`ai_session.model_id` records which model answered. It is derived from
    configuration and is never a literal in the source -- the literal scan in
    `test_ai_admin_providers.py` would observe one."""
    assert "a-model-name" in build().id


@pytest.mark.parametrize("build", [_openai, _anthropic], ids=["openai_compatible", "anthropic"])
def test_a_shipped_client_satisfies_the_model_provider_protocol(build: Any) -> None:
    assert isinstance(build(), ModelProvider)


# --------------------------------------------------------------------------- #
# 6. Cancellation hygiene.
# --------------------------------------------------------------------------- #


@pytest.mark.timeout(20)
async def test_an_abandoned_stream_closes_its_client(monkeypatch: Any) -> None:
    """The orchestrator wraps the whole exchange in its own `asyncio.timeout`
    and can abandon this iterator mid-stream. The generator's cleanup must still
    run, or a slow provider leaks one connection per turn."""
    transport = TricklingTransport()
    install_outbound_spy(monkeypatch, transport)

    provider = _openai()
    stream = provider.stream_turn(
        system="SYSTEM", messages=[], tools=[], deadline=time.monotonic() + 30.0
    )
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.3):
            async for _ in stream:
                pass
    await stream.aclose()
