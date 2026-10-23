"""rsp/1 framing (docs/kernel/wire-protocol/remote-screen-protocol.md).

The server reads only the type byte (for direction), AUTH, STATE and
CONTROL. Everything else is opaque to it, so these tests pin those, the
shared vectors the agent also reads, and the recording container.
"""

import json
import struct
from pathlib import Path

import pytest

from app.modules.remote import rsp

VECTORS = (
    Path(__file__).resolve().parents[2] / "docs" / "kernel" / "wire-protocol" / "rsp1-vectors.json"
)


def test_shared_vectors_decode_the_same_here_as_in_the_agent() -> None:
    """The Go agent's internal/rsp tests read the same file."""
    frames = {v["name"]: v for v in json.loads(VECTORS.read_text())["frames"]}
    for name, v in frames.items():
        frame = rsp.decode(bytes.fromhex(v["hex"]))
        assert (frame.type, frame.seq) == (v["type"], v["seq"]), name
        if "json" in v:
            assert json.loads(frame.body) == v["json"], name

    auth = rsp.decode(bytes.fromhex(frames["auth_agent"]["hex"]))
    assert rsp.parse_auth(auth, secret_field="token")[1] == "tok"
    viewer = rsp.decode(bytes.fromhex(frames["auth_viewer"]["hex"]))
    assert rsp.parse_auth(viewer, secret_field="ticket")[1] == "tic"
    ended = rsp.parse_state(rsp.decode(bytes.fromhex(frames["state_ended"]["hex"])))
    assert ended == rsp.State("ended", "ended_by_user")
    control = rsp.decode(bytes.fromhex(frames["control_view_only"]["hex"]))
    assert rsp.parse_control(control) is False
    assert rsp.agent_to_viewer(frames["tile_fill"]["type"])
    assert rsp.viewer_to_agent(frames["pointer"]["type"])


def test_frame_round_trip() -> None:
    raw = rsp.encode(rsp.TILE, 7, b"\x00\x01payload")
    assert raw[0] == rsp.TILE
    assert struct.unpack(">I", raw[1:5]) == (7,)
    frame = rsp.decode(raw)
    assert (frame.type, frame.seq, frame.body) == (rsp.TILE, 7, b"\x00\x01payload")


@pytest.mark.parametrize("raw", [b"", b"\x03", b"\x03\x00\x00\x00"])
def test_short_frame_is_rejected(raw: bytes) -> None:
    with pytest.raises(rsp.FrameError):
        rsp.decode(raw)


def test_oversized_frame_is_rejected() -> None:
    with pytest.raises(rsp.FrameError):
        rsp.decode(bytes([rsp.TILE]) + b"\x00" * 4 + b"x" * rsp.MAX_MESSAGE_BYTES)


def test_direction_is_by_range_so_new_types_pass_without_a_gateway_upgrade() -> None:
    assert rsp.agent_to_viewer(rsp.HELLO) and rsp.agent_to_viewer(rsp.STATE)
    assert rsp.agent_to_viewer(0x3F)  # unassigned, but in the agent range
    assert not rsp.agent_to_viewer(rsp.POINTER)
    assert rsp.viewer_to_agent(rsp.KEY) and rsp.viewer_to_agent(0x7F)
    assert not rsp.viewer_to_agent(rsp.TILE)
    for hop in (rsp.AUTH, rsp.AUTH_OK):
        assert not rsp.agent_to_viewer(hop) and not rsp.viewer_to_agent(hop)
    # 0x00 and the reserved block belong to nobody.
    for t in (0x00, 0x80, 0xEF):
        assert not rsp.agent_to_viewer(t) and not rsp.viewer_to_agent(t)


def test_input_types_are_the_ones_the_gateway_may_drop() -> None:
    assert rsp.INPUT_TYPES == {rsp.POINTER, rsp.WHEEL, rsp.KEY, rsp.TEXT, rsp.SAS}


def test_auth_body_round_trip_and_rejects_garbage() -> None:
    raw = rsp.auth_frame(session_id="abc", secret_field="token", secret="t0k")
    frame = rsp.decode(raw)
    assert frame.type == rsp.AUTH
    assert rsp.parse_auth(frame, secret_field="token") == ("abc", "t0k")
    with pytest.raises(rsp.FrameError):
        rsp.parse_auth(frame, secret_field="ticket")
    with pytest.raises(rsp.FrameError):
        rsp.parse_auth(rsp.decode(rsp.encode(rsp.AUTH, 1, b"not json")), secret_field="token")
    with pytest.raises(rsp.FrameError):
        rsp.parse_auth(rsp.decode(rsp.encode(rsp.TILE, 1, b"{}")), secret_field="token")


def test_state_body_parses_and_ignores_unknown_members() -> None:
    body = json.dumps({"state": "ended", "reason": "ended_by_user", "future": 1}).encode()
    state = rsp.parse_state(rsp.decode(rsp.encode(rsp.STATE, 3, body)))
    assert state.state == "ended" and state.reason == "ended_by_user"
    assert rsp.parse_state(rsp.decode(rsp.encode(rsp.STATE, 3, b"[]"))) is None
    assert rsp.parse_state(rsp.decode(rsp.encode(rsp.STATE, 3, b"\xff"))) is None


def test_control_body() -> None:
    assert rsp.parse_control(rsp.decode(rsp.encode(rsp.CONTROL, 1, b"\x01"))) is True
    assert rsp.parse_control(rsp.decode(rsp.encode(rsp.CONTROL, 1, b"\x00"))) is False
    assert rsp.parse_control(rsp.decode(rsp.encode(rsp.CONTROL, 1, b""))) is None


def test_recording_container(tmp_path) -> None:
    path = tmp_path / "rec.rsp"
    with path.open("wb") as fh:
        writer = rsp.RecordingWriter(fh, limit_bytes=1024)
        assert writer.write(5, rsp.encode(rsp.FRAME_BEGIN, 1, b""))
        assert writer.write(9, rsp.encode(rsp.FRAME_END, 2, b""))
    data = path.read_bytes()
    assert data[:8] == rsp.RECORDING_MAGIC
    records = list(rsp.read_recording(data))
    assert [(t, f[0]) for t, f in records] == [(5, rsp.FRAME_BEGIN), (9, rsp.FRAME_END)]


def test_recording_stops_at_the_cap_and_says_so(tmp_path) -> None:
    with (tmp_path / "rec.rsp").open("wb") as fh:
        writer = rsp.RecordingWriter(fh, limit_bytes=40)
        assert writer.write(0, rsp.encode(rsp.TILE, 1, b"x" * 10))
        assert not writer.write(1, rsp.encode(rsp.TILE, 2, b"x" * 10))
        assert writer.truncated
        # Once truncated it stays truncated: no later small frame sneaks in.
        assert not writer.write(2, rsp.encode(rsp.FRAME_END, 3, b""))
