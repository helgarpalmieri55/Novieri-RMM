"""The api's side of a screen session (spec-screen-share.md §2.1, §3.9).

The browser's WebSocket (``/api/v1/remote/sessions/{id}/screen``) is admitted
by ``authorize_tunnel`` like the guacd tunnel. This module then dials the
gateway's internal relay listener with the session's viewer ticket and pumps
rsp/1 frames between the two, teeing agent->viewer frames into a recording
spooled to disk. On every exit it uploads the recording (multipart) and ends
the session; if the gateway already ended it, that end is a no-op.

The browser never sees the relay token or the viewer ticket.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import websockets
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.modules.remote import rsp
from app.modules.remote.models import RECORDING_MAX_BYTES
from app.modules.remote.recording import finalize_recording_file
from app.modules.remote.service import SCREEN, InvalidSessionState, RemoteSessionService

logger = logging.getLogger(__name__)

AUTH_TIMEOUT_S = 10.0

Dial = Callable[..., Any]
# Built at upload time: constructing the store touches S3, and an outage must
# not stop a session from starting.
StoreFactory = Callable[[], ObjectStore]


class _Recording:
    """An ``.rsp`` spool file, capped at the policy's recording_max_bytes."""

    def __init__(self, spool_dir: Path | None, limit_bytes: int) -> None:
        fd, name = tempfile.mkstemp(prefix="rsp-", suffix=".rsp", dir=spool_dir)
        self.path = Path(name)
        self._fh = open(fd, "wb")  # noqa: SIM115 - closed in close()
        self._writer = rsp.RecordingWriter(self._fh, limit_bytes=limit_bytes)
        self._t0 = time.monotonic()
        self.frames = 0

    def write(self, frame: bytes) -> None:
        if self._writer.write(int((time.monotonic() - self._t0) * 1000), frame):
            self.frames += 1

    @property
    def truncated(self) -> bool:
        return self._writer.truncated

    def close(self) -> None:
        self._fh.close()

    def discard(self) -> None:
        with contextlib.suppress(OSError):
            self._fh.close()
        with contextlib.suppress(OSError):
            self.path.unlink()


async def run_screen_tunnel(
    *,
    browser_send: Callable[[bytes], Awaitable[None]],
    browser_recv: Callable[[], Awaitable[bytes | None]],
    session_id: UUID,
    org_id: UUID,
    service: RemoteSessionService,
    store: StoreFactory,
    app_engine: AsyncEngine,
    relay_internal_url: str,
    spool_dir: Path | None = None,
    dial: Dial = websockets.connect,
) -> None:
    """Drive the viewer leg from browser open to close.

    Returns without touching the session when this process holds no viewer
    ticket for it (not a screen session, already viewed, or ended).
    """
    params = service.connection_params(session_id)
    if params is None or params.get("protocol") != SCREEN:
        return
    ticket = service.take_viewer_ticket(session_id)
    if ticket is None:
        return

    recording: _Recording | None = None
    if params.get("record"):
        limit = int(params.get("recording_max_bytes") or RECORDING_MAX_BYTES)
        recording = _Recording(spool_dir, limit)

    try:
        async with dial(
            relay_internal_url,
            subprotocols=[rsp.SUBPROTOCOL],
            max_size=rsp.MAX_MESSAGE_BYTES,
            open_timeout=AUTH_TIMEOUT_S,
        ) as gateway:
            await gateway.send(
                rsp.auth_frame(session_id=str(session_id), secret_field="ticket", secret=ticket)
            )
            first = await asyncio.wait_for(gateway.recv(), timeout=AUTH_TIMEOUT_S)
            if not isinstance(first, bytes) or rsp.decode(first).type != rsp.AUTH_OK:
                return
            await _pump(gateway, browser_send, browser_recv, recording)
    except (OSError, TimeoutError, rsp.FrameError, websockets.exceptions.WebSocketException):
        logger.warning("screen session %s: relay leg failed", session_id, exc_info=True)
    finally:
        await _finish(
            service, store, app_engine, org_id=org_id, session_id=session_id, recording=recording
        )


async def _pump(
    gateway: Any,
    browser_send: Callable[[bytes], Awaitable[None]],
    browser_recv: Callable[[], Awaitable[bytes | None]],
    recording: _Recording | None,
) -> None:
    async def downstream() -> None:
        async for raw in gateway:
            if not isinstance(raw, bytes):
                continue
            try:
                frame_type = rsp.decode(raw).type
            except rsp.FrameError:
                continue
            if not rsp.agent_to_viewer(frame_type):
                continue
            if recording is not None:
                recording.write(raw)
            await browser_send(raw)

    async def upstream() -> None:
        while True:
            raw = await browser_recv()
            if raw is None:
                return
            try:
                frame_type = rsp.decode(raw).type
            except rsp.FrameError:
                continue
            if rsp.viewer_to_agent(frame_type):
                await gateway.send(raw)

    tasks = [asyncio.create_task(downstream()), asyncio.create_task(upstream())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception) and not isinstance(
            result, websockets.exceptions.ConnectionClosed
        ):
            logger.debug("screen pump ended with %r", result)


async def _finish(
    service: RemoteSessionService,
    store: StoreFactory,
    app_engine: AsyncEngine,
    *,
    org_id: UUID,
    session_id: UUID,
    recording: _Recording | None,
) -> None:
    """Upload the recording, then end the session, in separate transactions
    so an upload failure can never leave the session open."""
    if recording is not None:
        recording.close()
        try:
            if recording.frames > 0:
                object_store = store()
                async with org_session(org_id, engine=app_engine) as s:
                    await finalize_recording_file(
                        s,
                        object_store,
                        org_id=org_id,
                        session_id=session_id,
                        path=recording.path,
                        truncated=recording.truncated,
                    )
        except Exception:  # noqa: BLE001 - never block the end below
            logger.warning(
                "screen session %s: recording upload failed", session_id, exc_info=True
            )
        finally:
            recording.discard()
    try:
        async with org_session(org_id, engine=app_engine) as s:
            await service.end_session(s, session_id=session_id, reason="completed")
    except InvalidSessionState:
        pass  # the gateway (or a stop) ended it first, with the real reason
    except Exception:  # noqa: BLE001 - cleanup must not raise
        logger.warning("screen session %s: end failed", session_id, exc_info=True)
    service.discard_params(session_id)
