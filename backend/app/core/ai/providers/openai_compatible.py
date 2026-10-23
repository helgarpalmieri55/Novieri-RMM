"""Provider kind 1: an OpenAI-compatible chat-completions endpoint.

docs/modules/ai-admin/spec.md §11.2 option 1 -- **the on-prem default.** vLLM,
Ollama, llama.cpp, or a customer gateway, on the customer's own network.
**Nothing leaves.** This is what `deploy/` documents and what the internal
Toeshee deployment uses.

§11.3's honest cost, recorded rather than engineered around: small self-hosted
models are materially worse at TOOL SELECTION than frontier models. That is a
QUALITY cost, not a safety cost -- a wrong tool choice is a wrong or incomplete
answer, never an unsafe action (§8.3) -- and nothing in this module is loosened
to compensate. A deployment that finds answer quality unacceptable has a choice
to make about egress, and it should make it knowingly.

THIS FILE CONSTRUCTS NO HTTP CLIENT. It builds a request payload and parses a
response stream; the single outbound client lives in `_http.py`, so the module
has one egress site rather than one per provider.

TWO PARSING DECISIONS WORTH THE WORDS:

  * **Tool-call arguments arrive as a JSON string split across frames** and must
    be accumulated per `index` before being parsed once. Parsing each fragment
    would yield nothing; taking the last fragment would yield a suffix.
  * **A tool call is emitted only after the stream ends**, because a call is not
    complete until its last argument fragment has arrived. Text, by contrast, is
    yielded as it arrives -- that is what the console streams.
"""

import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from app.core.ai.providers import (
    Block,
    Message,
    ProviderConfig,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    UsageBlock,
    parse_tool_arguments,
)
from app.core.ai.providers._http import post_sse
from app.core.ai.types import ToolSpec

logger = logging.getLogger("its.ai_admin.providers")

_PATH = "/chat/completions"
_DONE = "[DONE]"


def _tools_payload(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }
        for tool in tools
    ]


def _messages_payload(system: str, messages: Sequence[Message]) -> list[dict[str, Any]]:
    """The transcript in the chat-completions shape.

    A `user` message carrying `ToolResultBlock`s becomes one `tool` message per
    result -- this API has no notion of several tool results inside one user
    turn, and flattening them into prose would put retrieved content into the
    same channel as the administrator's own words, which §8.2 forbids.
    """
    payload: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for message in messages:
        text = "".join(
            block.text for block in message.content if isinstance(block, TextBlock)
        )
        calls = [block for block in message.content if isinstance(block, ToolCallBlock)]
        if message.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": text}
            if calls:
                entry["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(dict(call.arguments), default=str),
                        },
                    }
                    for call in calls
                ]
            payload.append(entry)
            continue
        for block in message.content:
            if isinstance(block, ToolResultBlock):
                payload.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.tool_call_id,
                        "content": json.dumps(block.content, default=str),
                    }
                )
        if text:
            payload.append({"role": "user", "content": text})
    return payload


class OpenAICompatibleProvider:
    """A `ModelProvider` over `POST {base_url}/chat/completions`."""

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config
        # Recorded on `ai_session.model_id`. Composed from configuration, never
        # a literal -- `test_ai_admin_providers.py`'s literal scan would observe
        # a model name written into this file.
        self.id = f"{config.kind}:{config.model}"

    def _headers(self) -> dict[str, str]:
        """Read at POINT OF USE (invariant #3), never held on the object.

        A provider with no key file sends NO credential header at all rather
        than an empty bearer: a self-hosted server on the customer's own network
        usually has no key, and servers that validate the header's shape answer
        401 to an empty one.
        """
        headers = {"accept": "text/event-stream"}
        key = self._config.read_api_key()
        if key:
            headers["authorization"] = f"Bearer {key}"
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
            # Usage is otherwise absent from a streamed response, and a turn
            # that reports no tokens is treated as "unknown" by the daily budget
            # -- never as free.
            "stream_options": {"include_usage": True},
            "messages": _messages_payload(system, messages),
        }
        if tools:
            payload["tools"] = _tools_payload(tools)

        calls: dict[int, dict[str, str]] = {}
        input_tokens = 0
        output_tokens = 0

        async for event in post_sse(
            config=self._config,
            path=_PATH,
            payload=payload,
            headers=self._headers(),
            deadline=deadline,
        ):
            if event.data == _DONE:
                break
            try:
                frame = json.loads(event.data)
            except ValueError:
                # An unparseable FRAME is skipped, not fatal (§10.5): a stray
                # keep-alive or a vendor extension must not end an
                # administrator's turn. An unparseable tool-call ARGUMENT is a
                # different thing and is surfaced -- see `parse_tool_arguments`.
                logger.debug("ai provider emitted an unparseable frame; skipped")
                continue
            if not isinstance(frame, dict):
                continue
            for choice in frame.get("choices") or []:
                delta = choice.get("delta") or {}
                text = delta.get("content")
                if isinstance(text, str) and text:
                    yield TextBlock(text=text)
                for raw in delta.get("tool_calls") or []:
                    _absorb_call(calls, raw)
            usage = frame.get("usage")
            if isinstance(usage, dict):
                input_tokens += int(usage.get("prompt_tokens") or 0)
                output_tokens += int(usage.get("completion_tokens") or 0)

        for index in sorted(calls):
            slot = calls[index]
            yield ToolCallBlock(
                id=slot["id"] or f"call-{index}",
                name=slot["name"],
                arguments=parse_tool_arguments(slot["arguments"]),
            )
        if input_tokens or output_tokens:
            yield UsageBlock(input_tokens=input_tokens, output_tokens=output_tokens)


def _absorb(calls: dict[int, dict[str, str]], index: int) -> dict[str, str]:
    return calls.setdefault(index, {"id": "", "name": "", "arguments": ""})


def _absorb_call(calls: dict[int, dict[str, str]], raw: Mapping[str, Any]) -> None:
    """Accumulate one streamed tool-call fragment into its slot."""
    try:
        index = int(raw.get("index", 0))
    except (TypeError, ValueError):
        index = 0
    slot = _absorb(calls, index)
    if raw.get("id"):
        slot["id"] = str(raw["id"])
    function = raw.get("function") or {}
    if isinstance(function, Mapping):
        if function.get("name"):
            slot["name"] = str(function["name"])
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            slot["arguments"] += arguments
