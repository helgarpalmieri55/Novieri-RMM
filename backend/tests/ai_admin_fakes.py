"""Shared seeding and spies for the `ai_admin` tests.

TWO THINGS LIVE HERE AND BOTH ARE LOAD-BEARING.

`ai_user` builds a user holding EXACTLY the permission codes asked for, by
creating an org-scoped role and granting it those codes. `api_env.create_user`
takes ROLE names, and every seeded role is a bundle -- `technician` carries
dozens of codes -- so an authorization test written against a role cannot say
"has `ticket.read` and NOT `devices.read`", which is the exact distinction spec
§13 test 2 is about.

`RecordingTransport` wraps the real in-process transport and keeps every
`httpx.Request` the executor built. Assertions are made against the RECORDED
REQUEST -- its method, its full URL, its complete header set -- rather than
against the response, because a response only tells you what came back; the
adversarial-input tests (§13 test 4) are about what went OUT, and about the
calls that must NOT have been made at all.

THE PROVIDER FAKES ARE THE ONLY MODEL THIS REPO EVER TALKS TO. No test in this
module contacts a network, and none may. Two shipped provider clients now exist,
so the guarantee is no longer "no client exists" -- it is that the clients build
their outbound requests through ONE transport seam, that every test replaces
that seam with a local stub, and that `test_ai_admin_egress.py` records what
would have left. The fakes implement the real `ModelProvider` protocol
(`stream_turn` returning an async iterator of blocks), so the orchestrator's
tests exercise the shipped call shape rather than a convenient one.

`ScriptedProvider` IS HOW AN OBEDIENT-TO-INJECTION MODEL IS SIMULATED. It emits
whatever blocks the test hands it -- including tool calls with names no registry
entry has and arguments no args model accepts -- because a fake that only ever
emits legal calls cannot see a missing allowlist. That is the exact fixture
blindness this module is most exposed to.
"""

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


async def ai_user(api_env: Any, org_id: UUID, *codes: str) -> tuple[UUID, str]:
    """A user in `org_id` holding exactly `codes` and nothing else.

    Returns `(user_id, oidc_sub)`. The role is org-scoped and freshly named per
    call, so two callers in one test never share a grant set.
    """
    from app.models import Permission, Role, RolePermission, User, UserRole

    engine: AsyncEngine = api_env.admin_engine
    sub = f"ai-{uuid4().hex[:10]}"
    async with AsyncSession(engine, expire_on_commit=False) as session:
        user = User(org_id=org_id, external_subject=sub, email=f"{sub}@test.local")
        session.add(user)
        role = Role(org_id=org_id, name=f"ai-role-{uuid4().hex[:8]}", description="test")
        session.add(role)
        await session.flush()
        for code in codes:
            permission_id = (
                await session.execute(select(Permission.id).where(Permission.code == code))
            ).scalar_one()
            session.add(RolePermission(role_id=role.id, permission_id=permission_id))
        session.add(UserRole(org_id=org_id, user_id=user.id, role_id=role.id))
        await session.commit()
        return user.id, sub


class RecordingTransport(httpx.AsyncBaseTransport):
    """A real `ASGITransport` with a tape.

    `requests` holds every `httpx.Request` that reached the transport, in order.
    Zero recorded requests is itself an assertion in several tests -- an unknown
    tool name and a failed argument validation must produce a tool error and NO
    HTTP TRAFFIC, and "the response was an error" cannot tell those apart from
    "the request was made and the route rejected it".
    """

    def __init__(self, app: Any) -> None:
        self._inner = httpx.ASGITransport(app=app)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self._inner.handle_async_request(request)

    @property
    def only(self) -> httpx.Request:
        assert len(self.requests) == 1, f"expected exactly 1 request, got {len(self.requests)}"
        return self.requests[0]


class StaticStatusTransport(httpx.AsyncBaseTransport):
    """Answers every request with one status and body, without touching the app.

    Used for the 401/5xx mappings, which cannot be produced by the real stack
    without forging an expired token -- and forging one would test the token
    validator, not the executor's mapping of what it returns.
    """

    def __init__(self, status_code: int, body: bytes = b"{}") -> None:
        self.status_code = status_code
        self.body = body
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status_code, content=self.body, request=request)


class StaticJsonTransport(httpx.AsyncBaseTransport):
    """Answers every request with one status and one JSON body.

    Used to simulate a route that has QUIETLY GROWN A FIELD -- the case the
    registry test structurally cannot see, and the only reason the scrubber
    exists on top of the allowlist. Forcing it at the transport rather than by
    editing another module's response model keeps that module's own tests
    honest.
    """

    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self.body = body
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status_code, json=self.body, request=request)


class RecordingProvider:
    """A `ModelProvider` fake that CAPTURES ITS COMPLETE INPUT and emits a
    scripted final answer.

    It exists so "the value never reached model context" can be asserted against
    what a provider was actually handed, rather than against the executor's
    return value -- which is a weaker claim. `seen` holds every `stream_turn`
    call's system prompt, message list and tool list, stringified, so a leak
    assertion can be made with one `json.dumps` over the whole capture.

    IT MAKES NO NETWORK CALL AND CONTACTS NO MODEL. Nothing in this module may.
    """

    def __init__(self, answer: str = "", *, model_id: str = "fake/recording") -> None:
        self.id = model_id
        self.answer = answer
        self.seen: list[dict[str, Any]] = []
        self.calls = 0

    def stream_turn(
        self,
        *,
        system: str,
        messages: Sequence[Any],
        tools: Sequence[Any],
        deadline: float,
    ) -> AsyncIterator[Any]:
        return self._run(system, messages, tools)

    async def _run(
        self, system: str, messages: Sequence[Any], tools: Sequence[Any]
    ) -> AsyncIterator[Any]:
        from app.core.ai.providers import TextBlock

        self.calls += 1
        self.seen.append(
            {
                "system": system,
                "messages": [str(m) for m in messages],
                "tools": [str(t) for t in tools],
            }
        )
        if self.answer:
            yield TextBlock(text=self.answer)


class ScriptedProvider:
    """A `ModelProvider` fake that emits a caller-supplied sequence of blocks.

    One entry of `scripts` per `stream_turn` call, in order. An entry may be a
    list of blocks to yield, or an exception INSTANCE -- raised instead, which
    is how provider 5xx / timeout / 429 policy is exercised without a network.
    An exception may also appear inside a list, to fail part-way through a
    stream.

    RUNNING OFF THE END RAISES. An exhausted script that quietly yielded nothing
    would make a turn end for a reason the test did not choose, and the test
    would then be asserting about the fake rather than about the loop.
    """

    def __init__(
        self, scripts: Sequence[Any], *, model_id: str = "fake/scripted"
    ) -> None:
        self.id = model_id
        self.scripts: list[Any] = list(scripts)
        self.calls = 0
        self.seen: list[dict[str, Any]] = []

    def stream_turn(
        self,
        *,
        system: str,
        messages: Sequence[Any],
        tools: Sequence[Any],
        deadline: float,
    ) -> AsyncIterator[Any]:
        return self._run(system, messages, tools)

    async def _run(
        self, system: str, messages: Sequence[Any], tools: Sequence[Any]
    ) -> AsyncIterator[Any]:
        self.calls += 1
        self.seen.append(
            {
                "system": system,
                "messages": [str(m) for m in messages],
                "tools": [str(t) for t in tools],
            }
        )
        if not self.scripts:
            raise AssertionError(
                f"ScriptedProvider ran out of script on call {self.calls}; "
                f"the loop asked for more turns than the test scripted"
            )
        script = self.scripts.pop(0)
        if isinstance(script, BaseException):
            raise script
        for block in script:
            if isinstance(block, BaseException):
                raise block
            yield block


class GatedProvider:
    """A provider that blocks until a test releases it, then answers.

    This is what makes the connection-pool test possible: the turn is genuinely
    IN FLIGHT while an ordinary request is issued, so "the route held a database
    session across the model call" and "the route did not" produce different
    outcomes rather than the same fast pass.
    """

    id = "fake/gated"

    def __init__(self, release: asyncio.Event, *, answer: str = "done") -> None:
        self.release = release
        self.answer = answer
        self.calls = 0
        self.started = asyncio.Event()

    def stream_turn(
        self,
        *,
        system: str,
        messages: Sequence[Any],
        tools: Sequence[Any],
        deadline: float,
    ) -> AsyncIterator[Any]:
        return self._run()

    async def _run(self) -> AsyncIterator[Any]:
        from app.core.ai.providers import TextBlock

        self.calls += 1
        self.started.set()
        await self.release.wait()
        yield TextBlock(text=self.answer)


class HangingProvider:
    """A provider that never finishes.

    `trickle=False` blocks on a sleep and emits nothing -- the "connected and
    silent" case. `trickle=True` emits a token every `interval` seconds forever,
    which is the case a per-phase read timeout CANNOT bound: each token resets
    the phase clock, so only a whole-turn wall clock ends it. Both must end the
    turn at the deadline.
    """

    id = "fake/hanging"

    def __init__(self, *, trickle: bool = False, interval: float = 0.01) -> None:
        self.trickle = trickle
        self.interval = interval
        self.calls = 0

    def stream_turn(
        self,
        *,
        system: str,
        messages: Sequence[Any],
        tools: Sequence[Any],
        deadline: float,
    ) -> AsyncIterator[Any]:
        return self._run()

    async def _run(self) -> AsyncIterator[Any]:
        from app.core.ai.providers import TextBlock

        self.calls += 1
        while True:
            await asyncio.sleep(self.interval)
            if self.trickle:
                yield TextBlock(text=".")


def install_spy(monkeypatch: Any, transport: httpx.AsyncBaseTransport) -> None:
    """Point the STAFF executor's transport seam at `transport`."""
    from app.core.ai import executor_staff as executor

    monkeypatch.setattr(executor, "_transport", lambda app: transport)


def install_portal_spy(monkeypatch: Any, transport: httpx.AsyncBaseTransport) -> None:
    """Point the PORTAL executor's transport seam at `transport`.

    A SECOND function rather than a `which` argument, for the same reason there
    are two executors (docs/modules/ai-automation.md §0.2): a test that patched
    "the executor" would silently patch the wrong one the day a persona changed
    executors, and the assertion it makes -- "zero requests were built" -- is
    exactly the assertion an unpatched seam satisfies for free.
    """
    from app.core.ai import executor_portal as executor

    monkeypatch.setattr(executor, "_transport", lambda app: transport)


# --------------------------------------------------------------------------- #
# The OUTBOUND side: stubs and spies for the two provider clients.
#
# Everything below stands in for a model provider's HTTP endpoint. NOTHING HERE
# TOUCHES A NETWORK, and the egress test asserts that by recording every request
# that reaches the provider client's transport and naming the hosts.
# --------------------------------------------------------------------------- #


def sse(*frames: str) -> bytes:
    """Frames joined into an SSE body.

    Each frame is written as its raw lines (`event: ...` / `data: ...`) and the
    blank-line terminator is added here, so a test's canned script reads like the
    wire format rather than like an escaping exercise.
    """
    return "".join(f"{frame}\n\n" for frame in frames).encode("utf-8")


class ScriptedSseApp:
    """A local ASGI stub that answers each request with the next scripted body.

    It is a real ASGI app reached through a real `httpx` request, so the client
    under test builds a genuine request -- method, path, headers and JSON body --
    and this records all four. Asserting on `requests[-1]["body"]` is asserting
    on WHAT WAS SENT, which is the only thing an egress test may rely on.

    Running off the end of the script raises inside the app rather than serving
    an empty body, because a client that made one more call than the test
    scripted must fail loudly instead of seeing a stream that ends immediately.
    """

    def __init__(self, bodies: Sequence[bytes], *, status: int = 200) -> None:
        self.bodies = list(bodies)
        self.status = status
        self.requests: list[dict[str, Any]] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        import json as _json

        chunks: list[bytes] = []
        while True:
            message = await receive()
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        raw = b"".join(chunks)
        self.requests.append(
            {
                "method": scope["method"],
                "path": scope["path"],
                "headers": {
                    key.decode(): value.decode() for key, value in scope["headers"]
                },
                "body": _json.loads(raw) if raw else None,
            }
        )
        if not self.bodies:
            raise AssertionError(
                f"the provider client made call {len(self.requests)}; "
                f"the test scripted {len(self.requests) - 1}"
            )
        body = self.bodies.pop(0)
        await send(
            {
                "type": "http.response.start",
                "status": self.status,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})


class OutboundSpy(httpx.AsyncBaseTransport):
    """Records EVERY request that reaches a provider client's transport, then
    delegates.

    `hosts` is the set of `(scheme, host, port)` tuples that actually left, and
    `urls` is the full URL of each one. Both are recorded, because a test that
    only checks the configured host would miss a URL smuggled through a path or
    a redirect -- the set answers "who did we contact", the URL list answers
    "with what".
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.urls: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        return await self.inner.handle_async_request(request)

    @property
    def hosts(self) -> set[tuple[str, str | None, int | None]]:
        return {
            (httpx.URL(url).scheme, httpx.URL(url).host, httpx.URL(url).port)
            for url in self.urls
        }


class TricklingTransport(httpx.AsyncBaseTransport):
    """Answers with headers immediately, then emits one keep-alive comment every
    `interval` seconds FOREVER.

    THIS IS THE CASE A PER-PHASE READ TIMEOUT CANNOT BOUND: each byte resets the
    read clock, so only a whole-exchange wall clock ends it. A client that swaps
    the absolute deadline for `httpx.Timeout(read=...)` never returns here.
    """

    def __init__(self, *, interval: float = 0.01) -> None:
        self.interval = interval
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        interval = self.interval

        async def forever() -> AsyncIterator[bytes]:
            while True:
                await asyncio.sleep(interval)
                yield b": keep-alive\n\n"

        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=forever()
        )


class SilentTransport(httpx.AsyncBaseTransport):
    """Accepts the connection and never sends response headers at all.

    The other half of the deadline property: the trickle case is bounded while
    reading the body, this one is bounded before the body exists.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        await asyncio.sleep(3600)
        raise AssertionError("unreachable: the deadline should have ended this")


class StatusTransport(httpx.AsyncBaseTransport):
    """One status, one body, optional headers -- for the 429/5xx/redirect maps."""

    def __init__(
        self,
        status_code: int,
        *,
        body: bytes = b"",
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.headers = dict(headers or {})
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            self.status_code, headers=self.headers, content=self.body, request=request
        )


def install_outbound_spy(monkeypatch: Any, transport: httpx.AsyncBaseTransport) -> None:
    """Point the PROVIDER clients' single transport seam at `transport`.

    There is exactly one such seam in the module (`providers/_http.py`), which is
    the same property `test_ai_admin_egress.py` asserts structurally: one
    outbound construction site, so one place a test has to stand to see
    everything that leaves.
    """
    from app.core.ai.providers import _http

    monkeypatch.setattr(_http, "_transport", lambda: transport)
