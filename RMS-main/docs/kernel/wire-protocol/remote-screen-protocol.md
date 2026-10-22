# rsp/1: the remote-screen protocol

The media half of a screen session
(`docs/modules/remote-control/spec-screen-share.md` §3). The control half is
the signed `remote.screen.open` job (`RemoteScreenOpenParams` /
`RemoteScreenOpenResult` in this directory). rsp/1 is not JSON and has no
schema file; this document is its contract, and `rsp1-vectors.json` pins the
bytes. Three parties implement it and each reads those vectors in its tests:

| Party | Code | Reads the vectors in |
|---|---|---|
| Agent | `agent/internal/rsp` | `agent/internal/rsp/rsp_test.go` |
| Server (gateway relay, api viewer leg) | `backend/app/modules/remote/rsp.py` | `backend/tests/test_remote_rsp.py` |
| Browser viewer | `frontend/src/modules/remote/rsp.ts` | `frontend/src/modules/remote/rsp.test.ts` |

## Where it runs

```
browser ──WSS /api/v1/remote/sessions/{id}/screen──▶ api ──WS /relay-internal──▶ gateway ◀──WSS mTLS /relay── agent
            (user JWT, RBAC, owner check)              (viewer ticket, :8444,         (agent certificate +
                                                          compose network only)          one-shot relay token)
```

- **Agent leg.** The agent's second outbound connection, to the `relay_url`
  from the signed params (`wss://<gateway>:8443/relay`): same port, same client
  certificate, same pinned CA as the agent channel. Nothing listens on the
  endpoint.
- **Viewer leg.** The api dials the gateway's internal listener with a viewer
  ticket it holds in memory. The browser never sees the relay token or the
  ticket.
- **Browser.** Speaks rsp/1 to the api as binary WebSocket messages. The api
  forwards only viewer→agent types upward and only agent→viewer types down.

Both relay legs negotiate the WebSocket subprotocol **`itms-rsp.1`**. A leg
that does not is closed with 4400. The gateway accepts connections that offer
no subprotocol on every other path, so agents on `/agent` are unaffected.

## Frame

One binary WebSocket message is one frame:

```
offset  size  field
0       1     type   (u8)
1       4     seq    (u32, big-endian)
5       n     body   (type-specific)
```

- **seq** counts frames per sender per leg, starting at 1, and may wrap. ACK
  refers to it.
- **Maximum message size is 1 MiB** (1 048 576 bytes, header included). A
  sender splits larger updates into more tiles. Receivers close the leg on a
  larger message (the WebSocket layer enforces the same limit).
- All integers are big-endian. Coordinates are pixels relative to the selected
  monitor's top-left.
- Text messages are not part of the protocol; receivers drop them.

## Direction is the type's range

| Range | Direction | Gateway |
|---|---|---|
| `0x01`–`0x3F` | agent → viewer | forwards from the agent leg only |
| `0x40`–`0x7F` | viewer → agent | forwards from the viewer leg only |
| `0xF0`–`0xFF` | a leg ↔ the gateway | never forwarded |
| `0x00`, `0x80`–`0xEF` | reserved | dropped |

The gateway drops (and counts) any frame arriving from the wrong side. Because
the rule is a range, **a new frame type inside an existing range needs no
gateway release**; only its two ends must understand it. Readers **ignore
types they do not know**, which is what lets a newer viewer talk to an older
agent. `HELLO` lists what the agent understands.

## Frame types

### Hop-by-hop (a leg ↔ the gateway)

| Type | Name | Body |
|---|---|---|
| `0xF0` | AUTH | UTF-8 JSON object. Agent leg: `{"session_id", "token"}`. Viewer leg: `{"session_id", "ticket"}`. |
| `0xF1` | AUTH_OK | UTF-8 JSON object `{"session_id"}`. |

AUTH must be the first message on a leg, within 10 s. The gateway answers
AUTH_OK or closes the leg (see close codes). JSON members not listed are
ignored.

The agent leg is admitted only when **all** hold (spec §3.2): the certificate
resolves to a non-revoked, unexpired agent; the session's registration still
exists in Redis and is consumed by this attempt (so a token works once, and a
failed attempt burns it); the agent's device and organisation are the
registration's; `sha256(token)` matches; `connect_by` has not passed; the
session is `awaiting_consent` or `connecting`. Each refusal is audited as
`remote.relay.token_rejected` or `remote.relay.identity_mismatch` with a
`reason`.

The viewer leg receives AUTH_OK as soon as its ticket checks out, then waits
for the agent until `connect_by` (plus a few seconds). If the agent never
arrives the gateway ends the session (`expired`) and closes the leg with 4408.

### Agent → viewer

| Type | Name | Body |
|---|---|---|
| `0x01` | HELLO | UTF-8 JSON: `{"version": 1, "capabilities": [..], "monitors": [{"id", "x", "y", "width", "height", "dpi", "primary"}], "codecs": [..], "selected"}` |
| `0x02` | FRAME_BEGIN | empty |
| `0x03` | TILE | `x u16, y u16, w u16, h u16, codec u8, data`. codec 0 = fill (data = 3 bytes RGB), 1 = JPEG (data = a baseline JPEG of w×h). |
| `0x04` | COPYRECT | `src_x u16, src_y u16, x u16, y u16, w u16, h u16`: copy the w×h area at (src_x, src_y) to (x, y). |
| `0x05` | FRAME_END | empty |
| `0x06` | CURSOR_SHAPE | `hot_x u16, hot_y u16, w u16, h u16, codec u8, data` (codec 2 = PNG) |
| `0x07` | CURSOR_POS | `x u16, y u16, visible u8` |
| `0x08` | STATE | UTF-8 JSON: `{"state": <name>, ...}` (below) |

A frame on screen is `FRAME_BEGIN`, any number of `TILE`/`COPYRECT`, then
`FRAME_END`, applied atomically at `FRAME_END`.

**HELLO** (details added with phase 1, all compatible):

- `selected` is the `id` of the monitor the following frames describe; a
  reader that finds it absent assumes the primary. HELLO is **sent again**
  whenever the selection or that monitor's geometry changes (a
  `MONITOR_SELECT`, a display-mode change, the desktop coming back after a
  secure-desktop switch), and the frames after it are a full picture of the
  new geometry. The gateway marks the session active on the first HELLO only.
- `capabilities` names what the viewer may use: `tile`, `copyrect`,
  `cursor`, `control` (input is accepted at all), `text`, `monitor_select`,
  `quality`, `refresh`. A viewer does not offer what is absent.
- Monitor coordinates are physical pixels (the agent is per-monitor DPI
  aware); ids are stable for the session.

**Frames start on REFRESH.** After HELLO the agent sends no picture until the
viewer's first `REFRESH`, because the viewer may attach after the agent (the
gateway buffers only a short backlog before a viewer arrives). A viewer
therefore sends `REFRESH` on every HELLO. `REFRESH` also clears the agent's
flow-control window: the viewer is asking to start over.

**TILE, cursor.** JPEG tiles are baseline JPEGs of exactly `w`×`h`; fills are
3 bytes RGB. `CURSOR_SHAPE` uses codec 2, a PNG with alpha; a pixel the OS
would invert against the screen arrives black. `CURSOR_POS` is relative to
the selected monitor; `visible` is 0 while the pointer is on another monitor.

**Flow control.** The agent does not begin a new frame while more than two
`FRAME_END`s are unacknowledged. Dirty regions accumulate meanwhile, so a slow
link gets fewer, larger updates rather than a growing queue.

**STATE names.** The gateway reads these; everything else is forwarded only.

| state | Extra members | Server effect |
|---|---|---|
| `consent_pending` | `timeout_s` | audit `remote.session.consent_prompted` |
| `consent_granted` | | `awaiting_consent → connecting`, audit `remote.session.consent_granted` |
| `consent_denied` | | session ends `denied_consent`, audit `remote.session.denied`, both legs closed |
| `consent_timeout` | | session ends `consent_timeout`, audited, both legs closed |
| `control_revoked_by_user` | | input off for the rest of the session, audit `remote.session.input_disabled` `{by: endpoint_user}` |
| `input_desktop_unavailable` | | forwarded only (the viewer shows a placeholder) |
| `privacy_blank_on` / `privacy_blank_off` | | audit `remote.session.privacy_blank` (phase 2) |
| `ended` | `reason` | the session ends with `reason` when the agent closes; `ended_by_user` is also audited as `remote.session.ended_by_user` |

`ended` reasons: `completed`, `ended_by_user`, `no_user_session`,
`capture_unavailable`, `permission_missing`, `unsupported_display`, `expired`,
`error`. An unknown reason is stored as `error`.

The session becomes `active` (audit `remote.session.started`) at the first
`HELLO` after consent. **Before consent the gateway forwards STATE only**: any
other agent frame is dropped, so no pixel reaches a viewer the user has not
allowed.

### Viewer → agent

| Type | Name | Body |
|---|---|---|
| `0x40` | ACK | `frame_seq u32`: the seq of the last `FRAME_END` applied |
| `0x41` | POINTER | `x u16, y u16, buttons u8` (bitmask: 1 left, 2 right, 4 middle, 8 back, 16 forward) |
| `0x42` | WHEEL | `dx i16, dy i16`, in 1/120ths of a notch; positive `dy` scrolls down and positive `dx` right (the DOM's sense) |
| `0x43` | KEY | `down u8, code` where code is `KeyboardEvent.code`, UTF-8, at most 32 bytes |
| `0x44` | TEXT | UTF-8, at most 256 bytes |
| `0x45` | MONITOR_SELECT | `monitor_id u8` |
| `0x46` | QUALITY | `level u8` (0 auto, 1 low, 2 medium, 3 high); a level above 3 is ignored |
| `0x47` | REFRESH | empty: send a full frame. A viewer sends it after it attaches. |
| `0x48` | SAS | empty (phase 2: Ctrl+Alt+Del) |
| `0x49` | CONTROL | `mode u8` (0 view only, 1 control) |

The gateway forwards nothing from the viewer before consent. It drops
POINTER, WHEEL, KEY, TEXT and SAS while the session is view-only, drops
`CONTROL(1)` when policy disallows input or after the endpoint user has
stopped control, and audits every change of mode
(`remote.session.input_enabled` / `input_disabled`, `{by: technician}`).

The agent validates every input frame again on its own (spec §3.3): types from
an allowlist, coordinates clamped to the selected monitor, key codes from a
fixed table (`agent/internal/capabilities/remote/helper/keymap.go`; a code not
in it is dropped), TEXT capped, at most 500 input events per second, nothing
before consent and nothing while view-only. Leaving control releases every key
and button the technician was holding. The gateway's filtering is a second
line, not the first.

## Close codes

| Code | Meaning |
|---|---|
| 4000 | session ended (the reason is the close reason text) |
| 4400 | protocol error: wrong subprotocol, first frame not AUTH, malformed AUTH |
| 4401 | certificate unknown, revoked or expired |
| 4403 | refused: token or ticket did not check out (audited) |
| 4408 | timed out: no AUTH, the agent never came, or no viewer came |
| 4409 | conflict: that leg is already attached |

## Recordings (`.rsp`)

The api tees every agent→viewer frame it forwards into
`org/{org_id}/remote-recordings/{session_id}.rsp`:

```
"ITMSRSP1"                                  8-byte magic
repeat: t_ms u32 | len u32 | frame (len bytes)
```

`t_ms` is milliseconds since the recording started. The file is spooled to
disk and uploaded multipart. At the policy cap (`recording_max_bytes`, default
2 GiB) recording stops, the session continues, and the row is flagged
`recording_truncated` and audited `remote.recording.truncated`.

## Compatibility

- **rsp/1 is a three-party contract** (agent, gateway, browser), each of a
  different age in the field. An incompatible change to a frame's layout is
  **rsp/2**, negotiated as a new subprotocol; the agent keeps speaking rsp/1
  for at least one release after.
- **Compatible changes:** a new frame type in an existing range; a new JSON
  member in HELLO, STATE or AUTH; a new STATE name; a new TILE or cursor
  codec that the agent only uses when HELLO-negotiated. Receivers ignore what
  they do not know.
- **The gateway path dispatch is additive:** only the exact path `/relay`
  behaves differently; every other path is the agent channel as before.
- **No existing enum widened.** `JobStatusMsg.state`, `JobResultMsg.state` and
  `RemoteSessionOpenParams.protocol` are unchanged; consent travels as STATE,
  and the outcome is `RemoteScreenOpenResult.outcome`.
