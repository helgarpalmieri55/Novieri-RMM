"""rsp/1: the remote-screen frame format (docs/kernel/wire-protocol/remote-screen-protocol.md).

A frame is one binary WebSocket message: ``type:u8 | seq:u32be | body``.
The server reads only the type byte (direction), AUTH, STATE and CONTROL; all
other bodies are opaque here, so a new frame type inside an existing direction
range needs no gateway release.

Type ranges carry the direction, which is what the gateway enforces:

* ``0x01-0x3F`` agent -> viewer
* ``0x40-0x7F`` viewer -> agent
* ``0xF0-0xFF`` hop-by-hop (a leg <-> the gateway), never forwarded
* ``0x00`` and ``0x80-0xEF`` reserved: dropped by the gateway
"""

from __future__ import annotations

import json
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from typing import BinaryIO

SUBPROTOCOL = "itms-rsp.1"

# Largest message either leg may send. Matches the gateway's websockets
# max_size, so the codec and the socket agree on what "too big" means.
MAX_MESSAGE_BYTES = 1 << 20

HEADER_BYTES = 5

# agent -> viewer
HELLO = 0x01
FRAME_BEGIN = 0x02
TILE = 0x03
COPYRECT = 0x04
FRAME_END = 0x05
CURSOR_SHAPE = 0x06
CURSOR_POS = 0x07
STATE = 0x08

# viewer -> agent
ACK = 0x40
POINTER = 0x41
WHEEL = 0x42
KEY = 0x43
TEXT = 0x44
MONITOR_SELECT = 0x45
QUALITY = 0x46
REFRESH = 0x47
SAS = 0x48
CONTROL = 0x49

# hop-by-hop
AUTH = 0xF0
AUTH_OK = 0xF1

# Frames that act on the endpoint's input. The gateway drops these while the
# session is view-only; CONTROL is handled separately because it changes mode.
INPUT_TYPES = frozenset({POINTER, WHEEL, KEY, TEXT, SAS})

# WebSocket close codes used on both relay legs.
CLOSE_PROTOCOL = 4400
CLOSE_UNAUTHORIZED = 4401
CLOSE_REFUSED = 4403
CLOSE_TIMEOUT = 4408
CLOSE_CONFLICT = 4409
CLOSE_ENDED = 4000

RECORDING_MAGIC = b"ITMSRSP1"
_RECORD_HEADER = struct.Struct(">II")


class FrameError(ValueError):
    """A message that is not a well-formed rsp/1 frame of the expected kind."""


@dataclass(frozen=True)
class Frame:
    type: int
    seq: int
    body: bytes


@dataclass(frozen=True)
class State:
    state: str
    reason: str | None


def agent_to_viewer(frame_type: int) -> bool:
    return 0x01 <= frame_type <= 0x3F


def viewer_to_agent(frame_type: int) -> bool:
    return 0x40 <= frame_type <= 0x7F


def encode(frame_type: int, seq: int, body: bytes = b"") -> bytes:
    return struct.pack(">BI", frame_type, seq & 0xFFFFFFFF) + body


def decode(raw: bytes) -> Frame:
    if len(raw) < HEADER_BYTES:
        raise FrameError(  # i18n-exempt: relay catches it and closes the leg
            "short frame"
        )
    if len(raw) > MAX_MESSAGE_BYTES:
        raise FrameError(  # i18n-exempt: relay catches it and closes the leg
            "frame exceeds rsp/1 message limit"
        )
    frame_type, seq = struct.unpack_from(">BI", raw)
    return Frame(frame_type, seq, bytes(raw[HEADER_BYTES:]))


def _json_body(frame: Frame) -> dict[str, object]:
    try:
        value = json.loads(frame.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FrameError(  # i18n-exempt: relay catches it and closes the leg
            "body is not UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise FrameError(  # i18n-exempt: relay catches it and closes the leg
            "body is not a JSON object"
        )
    return value


def auth_frame(*, session_id: str, secret_field: str, secret: str, seq: int = 1) -> bytes:
    body = json.dumps({"session_id": session_id, secret_field: secret}).encode()
    return encode(AUTH, seq, body)


def parse_auth(frame: Frame, *, secret_field: str) -> tuple[str, str]:
    """Return ``(session_id, secret)`` from an AUTH frame. ``secret_field`` is
    ``token`` on the agent leg and ``ticket`` on the viewer leg, so a secret
    presented on the wrong leg is a protocol error rather than a guess."""
    if frame.type != AUTH:
        raise FrameError(  # i18n-exempt: relay catches it and closes the leg
            "first frame must be AUTH"
        )
    body = _json_body(frame)
    session_id, secret = body.get("session_id"), body.get(secret_field)
    if not isinstance(session_id, str) or not isinstance(secret, str) or not secret:
        raise FrameError(  # i18n-exempt: relay catches it and closes the leg
            f"AUTH needs session_id and {secret_field}"
        )
    return session_id, secret


def auth_ok_frame(session_id: str, seq: int = 1) -> bytes:
    return encode(AUTH_OK, seq, json.dumps({"session_id": session_id}).encode())


def parse_state(frame: Frame) -> State | None:
    """None for a STATE the server cannot read; it is still forwarded."""
    try:
        body = _json_body(frame)
    except FrameError:
        return None
    state, reason = body.get("state"), body.get("reason")
    if not isinstance(state, str):
        return None
    return State(state, reason if isinstance(reason, str) else None)


def state_frame(state: str, seq: int, **extra: object) -> bytes:
    return encode(STATE, seq, json.dumps({"state": state, **extra}).encode())


def parse_control(frame: Frame) -> bool | None:
    """True = control requested, False = view-only, None = unreadable."""
    if len(frame.body) < 1:
        return None
    return frame.body[0] == 1


class RecordingWriter:
    """Appends frames to an ``.rsp`` recording, stopping at ``limit_bytes``.

    Container: ``RECORDING_MAGIC`` then records of
    ``t_ms:u32be | len:u32be | frame``, ``t_ms`` from the recording's start.
    """

    def __init__(self, fh: BinaryIO, *, limit_bytes: int) -> None:
        self._fh = fh
        self._limit = limit_bytes
        self._fh.write(RECORDING_MAGIC)
        self.bytes_written = len(RECORDING_MAGIC)
        self.truncated = False

    def write(self, t_ms: int, frame: bytes) -> bool:
        if self.truncated:
            return False
        size = _RECORD_HEADER.size + len(frame)
        if self.bytes_written + size > self._limit:
            self.truncated = True
            return False
        self._fh.write(_RECORD_HEADER.pack(t_ms & 0xFFFFFFFF, len(frame)))
        self._fh.write(frame)
        self.bytes_written += size
        return True


def read_recording(data: bytes) -> Iterator[tuple[int, bytes]]:
    if not data.startswith(RECORDING_MAGIC):
        raise FrameError(  # i18n-exempt: format guard, no route calls it
            "not an rsp/1 recording"
        )
    offset = len(RECORDING_MAGIC)
    while offset < len(data):
        t_ms, length = _RECORD_HEADER.unpack_from(data, offset)
        offset += _RECORD_HEADER.size
        yield t_ms, data[offset : offset + length]
        offset += length
