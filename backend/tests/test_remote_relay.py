"""Tests for the session relay (docs/modules/remote-control/spec.md).

The relay authenticates a per-session token presented by the agent and glues
the agent's byte stream to the guacd stream. It never inspects payload bytes.
"""

import asyncio
import socket
from uuid import uuid4

from app.modules.remote.relay import (
    SessionRelay,
    StreamReaderLike,
    StreamWriterLike,
    mint_relay_token,
    token_hash,
)


def _fake_stream() -> tuple[StreamReaderLike, StreamWriterLike]:
    """A minimal reader/writer pair that satisfies attach_agent's storage needs
    without ever being pumped (used only in the reject-path test)."""

    class _FakeReader:
        async def read(self, n: int = -1) -> bytes:
            return b""

    class _FakeWriter:
        def write(self, data: bytes) -> None:
            pass

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            return None

    return _FakeReader(), _FakeWriter()


async def _connected_pair() -> tuple[
    tuple[asyncio.StreamReader, asyncio.StreamWriter],
    tuple[asyncio.StreamReader, asyncio.StreamWriter],
]:
    """A pair of connected (reader, writer) endpoints backed by a socketpair,
    wrapped as real asyncio streams via asyncio.open_connection(sock=...)."""

    sock_a, sock_b = socket.socketpair()
    sock_a.setblocking(False)
    sock_b.setblocking(False)
    reader_a, writer_a = await asyncio.open_connection(sock=sock_a)
    reader_b, writer_b = await asyncio.open_connection(sock=sock_b)
    return (reader_a, writer_a), (reader_b, writer_b)


def test_token_mint_and_hash() -> None:
    token, h = mint_relay_token()
    assert len(token) > 20 and h == token_hash(token) and len(h) == 64


async def test_attach_rejects_wrong_token_or_device() -> None:
    relay = SessionRelay()
    sid, did = uuid4(), uuid4()
    token, h = mint_relay_token()
    relay.register(sid, did, h)
    r, w = _fake_stream()
    assert await relay.attach_agent(sid, did, "wrong-token", r, w) is False
    assert await relay.attach_agent(sid, uuid4(), token, r, w) is False  # wrong device
    assert await relay.attach_agent(sid, did, token, r, w) is True


async def test_pump_relays_bytes_bidirectionally() -> None:
    # agent side and guacd side are socketpairs; the relay glues them
    relay = SessionRelay()
    sid, did = uuid4(), uuid4()
    token, h = mint_relay_token()
    relay.register(sid, did, h)

    agent_a, agent_b = await _connected_pair()      # relay holds agent_b
    guacd_a, guacd_b = await _connected_pair()       # relay holds guacd_b
    assert await relay.attach_agent(sid, did, token, agent_b[0], agent_b[1]) is True
    task = asyncio.create_task(relay.pump(sid, guacd_b[0], guacd_b[1]))

    guacd_a[1].write(b"select rdp;")               # browser->guacd->endpoint
    await guacd_a[1].drain()
    assert await agent_a[0].readexactly(len(b"select rdp;")) == b"select rdp;"

    agent_a[1].write(b"0.;")                        # endpoint->guacd->browser
    await agent_a[1].drain()
    assert await guacd_a[0].readexactly(3) == b"0.;"

    agent_a[1].close()
    guacd_a[1].close()
    await asyncio.wait_for(task, timeout=2)


async def test_pump_joins_both_legs_and_closes_writers_when_one_leg_errors() -> None:
    """If one direction's read() raises (e.g. ConnectionResetError from an
    abrupt socket disconnect) instead of cleanly reaching EOF, pump() must
    still join both copy legs and close both writers, rather than the failing
    leg's exception propagating out of pump() immediately and leaving the
    other leg orphaned as a background task whose eventual result/exception
    is never retrieved.

    The other leg is wired to still be "in flight" (asleep, not blocked on
    anything related to the failing leg) for a short, real duration after the
    failing leg raises. With the old `asyncio.gather(...)` (no
    `return_exceptions=True`), pump() would propagate the failing leg's
    exception as soon as it raises — near-instantly, well before the sleeping
    leg finishes and closes its writer — so this test fails fast under that
    behavior. It only passes when pump() genuinely joins both legs (blocking
    for at least the sleeping leg's duration) before returning."""
    relay = SessionRelay()
    sid, did = uuid4(), uuid4()
    token, h = mint_relay_token()
    relay.register(sid, did, h)

    guacd_writer_closed = False
    slow_leg_duration = 0.1  # seconds; well under the 2s wait_for timeout

    class _SlowThenEofReader:
        """Simulates a leg that is still copying normally (no relation to
        the other leg's failure) and only reaches EOF after some real time."""

        async def read(self, n: int = -1) -> bytes:
            await asyncio.sleep(slow_leg_duration)
            return b""

    class _AgentWriter:
        def write(self, data: bytes) -> None:
            pass

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            return None

    class _ResetReader:
        """Simulates an abrupt disconnect: read() raises instead of EOF."""

        async def read(self, n: int = -1) -> bytes:
            raise ConnectionResetError("connection reset by peer")

    class _GuacdWriter:
        def write(self, data: bytes) -> None:
            pass

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            nonlocal guacd_writer_closed
            guacd_writer_closed = True

        async def wait_closed(self) -> None:
            return None

    agent_reader: StreamReaderLike = _SlowThenEofReader()
    agent_writer: StreamWriterLike = _AgentWriter()
    assert await relay.attach_agent(sid, did, token, agent_reader, agent_writer) is True

    guacd_reader: StreamReaderLike = _ResetReader()
    guacd_writer: StreamWriterLike = _GuacdWriter()

    raised: Exception | None = None
    start = asyncio.get_event_loop().time()
    try:
        await asyncio.wait_for(relay.pump(sid, guacd_reader, guacd_writer), timeout=2)
    except ConnectionResetError as exc:
        raised = exc
    elapsed = asyncio.get_event_loop().time() - start

    # pump() surfaces the failing leg's exception rather than hanging...
    assert raised is not None
    # ...but only returns *after* the other, still-in-flight leg has actually
    # finished — proving pump() joined both legs instead of returning as soon
    # as the first one raised.
    assert elapsed >= slow_leg_duration * 0.9
    # ...and that other leg's writer was closed too — no orphaned leg left.
    assert guacd_writer_closed is True
