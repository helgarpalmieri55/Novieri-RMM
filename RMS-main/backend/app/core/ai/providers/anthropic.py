"""Provider kind 2: the hosted Anthropic Messages API.

docs/modules/ai-admin/spec.md §11.2 option 2 -- **permitted, and explicitly
configured.** A deployment that must not egress simply does not configure a
provider of this kind: there is no key to leave in place by accident, because
none ships (§0.10).

**WHAT LEAVES THE CUSTOMER'S NETWORK WHEN THIS PROVIDER IS SELECTED**, enumerated
so nobody has to guess, and reproduced verbatim by the console before an
administrator can select it (`frontend/src/modules/ai-admin/AiSettingsPage.tsx`,
and enforced server-side by `SettingsUpdate.egress_acknowledged`):

  * the system prompt (a constant);
  * the tool schemas -- module API *shapes*, structural, not customer data;
  * **the administrator's question text**;
  * **tool results: real customer data.** Hostnames, ticket subjects and bodies,
    software inventory, alert text, license and vendor names, spend figures,
    audit action names. **This is the honest answer and it is a lot.**

What never leaves, under either provider kind: vault material (§7), attachment
bytes, recordings, agent certificates, and anything belonging to another org.

**Not enforceable in code, stated as a deployment requirement:** no-training /
no-retention terms with a hosted provider are contractual, and this module does
not pretend otherwise. `deploy/README.md` documents it.

NO VENDOR SDK. This is a JSON-over-HTTP client over the shared `_http.post_sse`,
for the reasons in that file's header. The base URL is server environment, so
this file names no host and the literal scan in `test_ai_admin_providers.py`
would observe one if it did.
"""

import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from app.core.ai.providers import (
    Block,
    Message,
    ProviderConfig,
    ProviderUnavailableError,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    UsageBlock,
    parse_tool_arguments,
)
from app.core.ai.providers._http import post_sse
from app.core.ai.types import ToolSpec

logger = logging.getLogger("its.ai_admin.providers")

_PATH = "/messages"
_API_VERSION = "2023-06-01"

# The Messages API requires a token cap on every request. The orchestrator's own
# `max_output_tokens` budget is `None` by default (§10.1: "the provider's own
# limit governs"), and this API has no "provider's own limit" to fall back on --
# so a value is required here. It is deliberately generous: it is a REQUEST
# FIELD, not a cost control. The cost controls are the per-org daily token budget
# and the turn budgets, both enforced server-side and both independent of this.
_MAX_OUTPUT_TOKENS = 4096


def _tools_payload(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }
        for tool in tools
    ]


def _content_payload(message: Message) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            if block.text:
                content.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolCallBlock):
            content.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": dict(block.arguments),
                }
            )
        elif isinstance(block, ToolResultBlock):
            content.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.tool_call_id,
                    "content": json.dumps(block.content, default=str),
                    "is_error": block.is_error,
                }
            )
    return content


def _messages_payload(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """The transcript in the Messages shape.

    Tool results ride as `tool_result` content blocks inside a `user` message --
    a STRUCTURALLY SEPARATE channel from the administrator's own words (§8.2),
    which is the same property `Message` models internally. Empty messages are
    dropped: this API rejects them, and an empty turn carries nothing anyway.
    """
    payload: list[dict[str, Any]] = []
    for message in messages:
        content = _content_payload(message)
        if content:
            payload.append({"role": message.role, "content": content})
    return payload


class AnthropicProvider:
    """A `ModelProvider` over `POST {base_url}/messages`."""

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config
        self.id = f"{config.kind}:{config.model}"

    def _headers(self) -> dict[str, str]:
        """Read at POINT OF USE (invariant #3), never held on the object."""
        headers = {
            "accept": "text/event-stream",
            "anthropic-version": _API_VERSION,
        }
        key = self._config.read_api_key()
        if key:
            headers["x-api-key"] = key
        return headers

    def stream_turn(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        deadline: float,
    ) -> AsyncIterator[Block]:
        return self._run(system, messages, tools, deadline)

    async def _run(
        self,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        deadline: float,
    ) -> AsyncIterator[Block]:
        payload: dict[str, Any] = {
            "model": self._config.model,
            "stream": True,
            "max_tokens": _MAX_OUTPUT_TOKENS,
            "system": system,
            "messages": _messages_payload(messages),
        }
        if tools:
            payload["tools"] = _tools_payload(tools)

        # One slot per streamed `tool_use` block, keyed by its content index.
        # The input arrives as `partial_json` fragments and is parsed ONCE, at
        # `content_block_stop`, because a call is not complete until its last
        # fragment has arrived.
        pending: dict[int, dict[str, str]] = {}
        input_tokens = 0
        output_tokens = 0

        async for event in post_sse(
            config=self._config,
            path=_PATH,
            payload=payload,
            headers=self._headers(),
            deadline=deadline,
        ):
            try:
                frame = json.loads(event.data)
            except ValueError:
                logger.debug("ai provider emitted an unparseable frame; skipped")
                continue
            if not isinstance(frame, dict):
                continue
            kind = str(frame.get("type") or event.event)

            if kind == "error":
                # An in-band error terminates the stream. Reported WITHOUT the
                # provider's own message: it is third-party text of unknown
                # content and this exception reaches a log line.
                raise ProviderUnavailableError(  # i18n-exempt: caught by the orchestrator
                    "the model provider reported a stream error"
                )

            if kind == "message_start":
                input_tokens += _usage(frame.get("message"), "input_tokens")
                output_tokens += _usage(frame.get("message"), "output_tokens")
            elif kind == "content_block_start":
                block = frame.get("content_block") or {}
                if isinstance(block, Mapping) and block.get("type") == "tool_use":
                    pending[_index(frame)] = {
                        "id": str(block.get("id") or ""),
                        "name": str(block.get("name") or ""),
                        "json": "",
                    }
            elif kind == "content_block_delta":
                delta = frame.get("delta") or {}
                if not isinstance(delta, Mapping):
                    continue
                if delta.get("type") == "text_delta":
                    text = delta.get("text")
                    if isinstance(text, str) and text:
                        yield TextBlock(text=text)
                elif delta.get("type") == "input_json_delta":
                    slot = pending.get(_index(frame))
                    fragment = delta.get("partial_json")
                    if slot is not None and isinstance(fragment, str):
                        slot["json"] += fragment
            elif kind == "content_block_stop":
                slot = pending.pop(_index(frame), None)
                if slot is not None:
                    yield ToolCallBlock(
                        id=slot["id"] or f"call-{_index(frame)}",
                        name=slot["name"],
                        arguments=parse_tool_arguments(slot["json"]),
                    )
            elif kind == "message_delta":
                input_tokens += _usage(frame, "input_tokens")
                output_tokens += _usage(frame, "output_tokens")

        if input_tokens or output_tokens:
            yield UsageBlock(input_tokens=input_tokens, output_tokens=output_tokens)


def _index(frame: Mapping[str, Any]) -> int:
    try:
        return int(frame.get("index", 0))
    except (TypeError, ValueError):
        return 0


def _usage(container: Any, field: str) -> int:
    if not isinstance(container, Mapping):
        return 0
    usage = container.get("usage")
    if not isinstance(usage, Mapping):
        return 0
    try:
        return int(usage.get(field) or 0)
    except (TypeError, ValueError):
        return 0
