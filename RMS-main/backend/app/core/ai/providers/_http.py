"""**THE ONE OUTBOUND HTTP PATH IN THIS MODULE.**

docs/modules/ai-admin/spec.md §0.10, §5, §10.4, §11.1, §11.2, §11.4.

Both provider clients send through this file and nothing else does, so there is
exactly one place a reviewer -- or a test -- has to stand to see everything this
module can put on a wire. `tests/test_ai_admin_egress.py` asserts that as an
EQUALITY over the package's client-construction sites: `executor.py` (in-process
`ASGITransport`, no socket) and this file. A third site is a new egress path and
the test names the file it appeared in.

WHY `httpx` AND NO PROVIDER SDK. Spec §11.1 already forbids a provider SDK type
appearing above the `ModelProvider` interface, and adding a vendor SDK to a
domain-admin-equivalent, on-prem control plane is a supply-chain commitment that
buys a JSON-over-HTTP client. Both target APIs are plain HTTP with SSE
responses. `httpx` is already a backend dependency, so this module adds none.

**THE DEADLINE IS ABSOLUTE AND BOUNDS THE WHOLE EXCHANGE. `read` IS DELIBERATELY
`None`.** This is the file's one genuinely subtle decision, and it is the
opposite of what looks careful. A per-phase read timeout -- `httpx.Timeout(read=
5)` -- RESETS ON EVERY BYTE, so a provider that trickles one keep-alive comment
every ten milliseconds holds the connection, and the administrator's turn, open
for ever. `app/core/egress.py` writes down the same property as its property 5.
So every await below is bounded by `deadline - time.monotonic()`, recomputed
each time and never refreshed:

  * the response-start phase, because a provider that accepts the connection and
    sends no headers has no body to time out reading;
  * each line of the body.

Both are `asyncio.wait_for` around a single await rather than one
`asyncio.timeout` around the whole generator, because a timeout scope that spans
a `yield` cancels whoever is consuming the generator instead of the generator.

REDIRECTS ARE NEVER FOLLOWED. The base URL is server environment and is never
org-editable (§0.10), which removes the SSRF primitive rather than guarding it --
but a `302` from whatever answers at that address would reintroduce it as an
arbitrary-host client. A `3xx` is a provider failure here, not a hop.

ERRORS ARE MAPPED BY STATUS TO THIS MODULE'S OWN TYPES, never re-raised as
`httpx` types: §10.4's retry policy is control flow over the exception type, and
a policy recovered by matching message prose is a policy that changes when
somebody rewords an error. **No response body ever reaches an exception message
or a log line** -- a provider's error body is third-party text of unknown
content, and a `401` body in particular is the one most likely to echo what was
sent.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.ai.providers import (
    ProviderConfig,
    ProviderError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

logger = logging.getLogger("its.ai_admin.providers")

__all__ = ["SseEvent", "post_sse"]

# Bounds the phases that are NOT bounded by the turn deadline's own arithmetic:
# a TCP connect that hangs, and a write that blocks. Neither can be reached by
# `deadline` alone because both can stall before any of this file's awaits are
# entered. The read phase is deliberately absent -- see the module header.
_CONNECT_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class SseEvent:
    """One dispatched server-sent event. `event` is `""` when the stream did not
    name one, which is the OpenAI-compatible shape; the Anthropic shape names
    every event and also repeats the name inside `data`."""

    event: str
    data: str


def _transport() -> httpx.AsyncBaseTransport | None:
    """The outbound transport, as a named seam.

    `None` means "httpx builds its own", which is the production path. Tests
    replace this function with one returning a recording transport over a local
    stub -- which is how `test_ai_admin_egress.py` can record every request that
    would have left the box, and how no test in this repo ever reaches a
    network.
    """
    return None


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=_CONNECT_TIMEOUT_S,
        write=_CONNECT_TIMEOUT_S,
        pool=_CONNECT_TIMEOUT_S,
        # `read=None` IS THE POINT. See the module header before changing it.
        read=None,
    )


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after", "").strip()
    try:
        return float(raw)
    except ValueError:
        # The header's other legal form is an HTTP date. The API layer's answer
        # without a hint is "try again shortly", which is honest; inventing a
        # number from a date we did not parse would not be.
        return None


def _error_for(response: httpx.Response) -> ProviderError:
    """Status -> this module's error hierarchy (§10.4).

    429 is its own type because the orchestrator must NOT retry it and the API
    layer answers with the provider's own retry hint. Everything else maps to
    `ProviderUnavailableError`, which is retried exactly once: a `4xx` that is
    not a 429 is a deployment or configuration fault (a wrong key, a wrong model
    name), and one wasted retry is a better failure mode than a 500 escaping
    `run_turn` and losing the administrator's question.
    """
    if response.status_code == 429:
        return ProviderRateLimitedError(  # i18n-exempt: caught by the orchestrator
            "the model provider is rate limiting this deployment",
            retry_after_s=_retry_after(response),
        )
    return ProviderUnavailableError(  # i18n-exempt: caught by the orchestrator
        f"the model provider answered {response.status_code}"
    )


async def _lines_until(response: httpx.Response, deadline: float) -> AsyncIterator[str]:
    """`response`'s body, line by line, bounded by an ABSOLUTE deadline.

    The budget handed to each read is the time left until `deadline`, never a
    fresh per-read allowance -- which is the whole difference between this and a
    read timeout, and the reason a trickling provider ends the turn instead of
    owning it.
    """
    lines = response.aiter_lines()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderTimeoutError(  # i18n-exempt: caught by the orchestrator
                "the model provider did not finish answering within the turn's time limit"
            )
        try:
            line = await asyncio.wait_for(anext(lines), remaining)
        except StopAsyncIteration:
            return
        except TimeoutError as exc:
            raise ProviderTimeoutError(  # i18n-exempt: caught by the orchestrator
                "the model provider did not finish answering within the turn's time limit"
            ) from exc
        yield line


async def post_sse(
    *,
    config: ProviderConfig,
    path: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    deadline: float,
) -> AsyncIterator[SseEvent]:
    """`POST` `payload` to `config.base_url + path` and yield each SSE event.

    `path` is a module constant in both callers and is NEVER derived from model
    output; the base URL is server environment (§0.10). Nothing an org-admin or
    a model can influence contributes to the URL.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        # No request is issued at all. A turn whose clock ran out before the
        # model was reached must not spend an outbound request on an answer
        # nobody will read.
        raise ProviderTimeoutError(  # i18n-exempt: caught by the orchestrator
            "the turn's deadline passed before the model was called"
        )

    client = httpx.AsyncClient(
        transport=_transport(),
        base_url=config.base_url,
        timeout=_timeout(),
        follow_redirects=False,
    )
    try:
        request = client.build_request("POST", path, json=dict(payload), headers=dict(headers))
        try:
            response = await asyncio.wait_for(client.send(request, stream=True), remaining)
        except TimeoutError as exc:
            raise ProviderTimeoutError(  # i18n-exempt: caught by the orchestrator
                "the model provider did not answer within the turn's time limit"
            ) from exc
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(  # i18n-exempt: caught by the orchestrator
                "the model provider did not answer in time") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(  # i18n-exempt: caught by the orchestrator
                f"the model provider could not be reached: {type(exc).__name__}"
            ) from exc

        try:
            if response.status_code >= 400:
                # The body is NOT read and NOT reported. See the module header.
                raise _error_for(response)
            if response.status_code >= 300:
                raise ProviderUnavailableError(  # i18n-exempt: caught by the orchestrator
                    f"the model provider answered {response.status_code}; "
                    f"redirects are never followed"
                )
            event = ""
            data: list[str] = []
            try:
                async for line in _lines_until(response, deadline):
                    if not line:
                        if data:
                            yield SseEvent(event=event, data="\n".join(data))
                        event = ""
                        data = []
                    elif line.startswith(":"):
                        continue  # a keep-alive comment
                    elif line.startswith("event:"):
                        event = line[len("event:") :].strip()
                    elif line.startswith("data:"):
                        data.append(line[len("data:") :].strip())
            except httpx.TimeoutException as exc:
                raise ProviderTimeoutError(  # i18n-exempt: caught by the orchestrator
                    "the model provider stopped answering part-way through"
                ) from exc
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError(  # i18n-exempt: caught by the orchestrator
                    f"the model provider's answer was cut short: {type(exc).__name__}"
                ) from exc
            if data:
                # A final frame with no terminating blank line still counts.
                yield SseEvent(event=event, data="\n".join(data))
        finally:
            await response.aclose()
    finally:
        await client.aclose()
