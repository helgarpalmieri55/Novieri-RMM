# Remote Control — Screen Share (own implementation)

> **Status: PHASE 0 BUILT, PHASE 1 BUILT (not yet verified on a laptop),
> phases 2–5 DESIGNED.** See "Phase 0 as built" and "Phase 1 as built" under
> §7 for what exists and where it differs from the text. Where this document
> names existing code it was checked against that code on 2026-09-23.
>
> Parent spec: `docs/modules/remote-control/spec.md` (control-plane session
> lifecycle, RBAC, recording, retention). This document **supersedes that
> spec's RDP-loopback agent half for attended helpdesk** and adds the agent,
> relay, wire and viewer design for sharing the user's *actual* screen.

---

## 0. Decisions at a glance

| Question | Decision | Why, in one line |
|---|---|---|
| Transport | Custom framed protocol (`rsp/1`) over a **second outbound mTLS WebSocket** from agent to gateway `/relay` | No new dependencies, no UDP, no TURN, same port and trust as today; relay is forced anyway, so WebRTC's P2P advantage is void |
| Codec, phase 1 | Dirty-rect **JPEG tiles** + solid fills + copy-rect, Go stdlib `image/jpeg` | Pure Go, decodes in every browser, costs ~0 on a static screen |
| Codec, later | Optional **H.264 via OS encoder** (Media Foundation / VideoToolbox), decoded with WebCodecs, negotiated per session | Better on video-heavy screens; never required, because Windows Server lacks Media Foundation by default |
| Where capture runs (Windows) | A **helper process** in the console session: the agent binary re-executed with a subcommand | One binary, one Authenticode signature; the SYSTEM service never loads graphics code |
| Service ↔ helper IPC | **Inherited anonymous pipes** only | No named pipe, no loopback socket, nothing that listens; the helper never holds the agent's mTLS key |
| Job type | New **`remote.screen.open`**, not a new `protocol` value on `remote.session.open` | Old agents refuse an unknown type loudly (`unknown_type`), and the params differ in kind |
| Consent reporting | A control frame on the relay leg, **not** a widened `JobStatusMsg.state` | Widening that enum is fatal for newer agents against older control planes (wire README) |
| Credentials | **None.** No vault checkout on this path | The helper already runs in the user's session; there is no logon to perform |
| guacd path | **Retired** (operator decision 2026-09-23): removed from compose and the API with phase 1 | Screen share is the only remote path |

---

## 1. Scope

**In scope:** attended and unattended viewing and control of the physical
console of Windows 11 laptops and Windows Server (phases 1–2), then macOS
(phase 4), then Linux servers running X11 (phase 5). Consent prompt,
always-visible indicator, recording, audit, RBAC, policy.

**Out of scope for this spec:** clipboard and file transfer (designed in §3.8,
built in phase 3 at the earliest), audio, multi-technician sessions, chat,
remote printing, Wayland capture, sessions to RDP-only (non-console) Windows
sessions before phase 2.

---

## 2. Architecture

### 2.1 End-to-end picture

```
 browser (ScreenViewer: canvas + worker decode)
    │  WSS  /api/v1/remote/sessions/{id}/screen      (user JWT, RBAC, owner or supervisor check)
    ▼
 api process ── viewer leg ────────────┐
    │  internal WS, docker network only │  per-session viewer ticket (never sent to browser)
    ▼                                   │
 gateway process  /relay-internal  ◄────┘   (not published; server-side listener)
    │  joins legs by session_id; tees nothing; enforces frame-type direction
    ▲
    │  WSS mTLS  :8443/relay   (agent cert + one-shot relay token from the signed job)
    │  OUTBOUND from the agent
 agent service (SYSTEM)  ── remote.screen.open handler
    │  inherited anonymous pipes (stdin/stdout of the child), no listener
    ▼
 helper process (console session): capture → encode → frames ; input ← frames ; consent + indicator UI
```

Control path (session setup) rides the existing signed-job gateway. Media
rides the relay. The relay carries opaque frames and is authorised only by a
token minted during a verified, RBAC-checked session start, exactly as the
parent spec §6 intends.

### 2.2 Two defects in today's relay this design has to fix first

These are facts about the current code, not design choices:

1. **The gateway serves no `/relay` path.** `GatewayServer._handle`
   (`backend/app/gateway/main.py`) treats every connection as an agent control
   channel regardless of the request path. `Settings.gateway_relay_url`
   defaults to `wss://localhost:8443/relay` and the compose comment says the
   relay shares port 8443, but nothing answers there.
2. **`SessionRelay` lives in the api process's memory** (`app.state.remote_service`
   in `backend/app/modules/remote/__init__.py`), while agent mTLS is terminated
   by the **gateway** process. An agent that dialled `/relay` would reach a
   process that has no registration for its session. `attach_agent` has no
   production caller.

Resolution (phase 1, task 1):

- `_handle` dispatches on `connection.request.path`: `/relay` goes to the new
  relay handler; **every other path keeps today's behaviour** (deployed agents
  dial whatever `gateway_url` they were enrolled with and must not break).
- Relay registration moves from api memory into Redis:
  `remote:relay:{session_id}` → `{device_id, token_hash, viewer_ticket_hash,
  connect_by}`, with a Redis TTL equal to `connect_by`. Registration is written
  by `RemoteSessionService.dispatch_pending` (post-commit, as today) and
  **consumed atomically** (`GETDEL`) on agent attach, which makes the token
  one-shot by construction.
- The gateway gains an internal-only WebSocket listener (`/relay-internal`,
  port 8444, on the compose network, **not** published through Traefik). The
  api's viewer leg dials it with the viewer ticket. Both legs are joined in the
  gateway process by `session_id`.

Why the join is in the gateway and not the api: the agent leg is the one that
must be mTLS-authenticated, and only the gateway terminates mTLS (Traefik does
TCP passthrough). Why the browser does not connect to the gateway directly: the
gateway has no HTTP surface and no user-token validation; `authorize_tunnel`
(`tunnel.py`) already implements admission, RBAC and ownership for the browser
and is reused unchanged.

### 2.3 Agent side: the capability

New package `agent/internal/capabilities/remote/`, registered in `mustBuild()`
as `remote.screen.open`. Registration is a map insert.

- **Dispatcher key:** `capabilityKey("remote.screen.open")` is `"remote"`: its
  own queue, **not** in `jobs.DefaultHeavyKeys`. A session can last hours; if it
  held a heavy slot it would starve patch and backup. The per-key worker runs
  one job at a time, so a second session on the same device queues; its
  `connect_by` will have passed when it starts, and it ends immediately with
  `outcome: expired`. The server also refuses a second live session per device
  (409), so this is a backstop.
- **Job lifetime:** the handler's context is bounded by the job's `expires_at`
  (`jobs.Handler` contract). The server creates this job with
  `ttl_s = max_duration_s + consent_timeout_s + 120`. Freshness is not weakened
  by the long TTL: the relay token is valid only until `connect_by`
  (issued_at + 60 s), so a late or replayed delivery cannot connect. Nonce
  non-replay still applies.
- **Handler sequence:**
  1. Parse `RemoteScreenOpenParams`. Reject if `connect_by` has passed.
  2. Dial `relay_url` over mTLS (same client cert, same pinned server CA as
     `internal/transport`), subprotocol `itms-rsp.1`, present the relay token
     in the first frame. If the gateway refuses, end `outcome: relay_refused`.
  3. Resolve the target session (§2.4). No interactive user and
     `consent_mode = attended` → `STATE{no_user_session}`, end.
  4. Launch the helper (§2.4) with two inherited pipes.
  5. Attended: helper shows the consent prompt; forward its answer as
     `STATE{consent_granted|consent_denied|consent_timeout}`. Anything but
     granted ends the session.
  6. Pump frames helper → relay and input relay → helper until either side
     closes, `max_duration_s` elapses, the context is cancelled (`job.cancel`),
     or the endpoint user presses End.
  7. **Release:** close the relay, close both pipes, wait for the helper to exit
     (5 s), then `TerminateProcess` it; close the process and token handles.
     Report `RemoteScreenOpenResult`.
- The **service process never captures, encodes or injects.** It only moves
  bytes between two file descriptors, so it never loads `d3d11.dll`,
  `dxgi.dll` or an encoder. That is what makes the idle budget checkable
  (§4).

### 2.4 Windows helper

**Process creation (phase 1, attended):**
`WTSGetActiveConsoleSessionId` → `WTSQueryUserToken` → `DuplicateTokenEx`
(primary) → `CreateEnvironmentBlock` → `CreateProcessAsUser` of the agent's own
executable path with argument `remote-helper`, `CREATE_NO_WINDOW |
CREATE_UNICODE_ENVIRONMENT`, `lpDesktop = "winsta0\\default"`, and
`STARTUPINFO` std handles set to the two pipe ends (inheritance restricted to
exactly those handles via `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`). The helper
therefore runs **as the logged-in user, at the user's integrity level**. The
helper validates that its parent is the agent service before doing anything.

Consequences of running as the user, stated rather than hidden:

- The helper cannot see or click the **UAC secure desktop** or inject into
  elevated windows (UIPI). The viewer shows a "the user is looking at an
  elevation prompt" placeholder when the input desktop switches away.
- It cannot capture the **lock screen or Winlogon desktop**.

**Phase 2 adds a second helper mode** for unattended sessions and the cases
above: the service duplicates **its own SYSTEM token**, sets
`TokenSessionId` to the console session, and launches the helper on
`winsta0\\winlogon` or following `OpenInputDesktop` switches. This mode is used
**only** when the resolved policy allows unattended and either no user is logged
on or the technician requests secure-desktop access. The consent UI is never
shown by a SYSTEM-token process: in attended phase-2 sessions a separate
user-token helper renders consent and the indicator, and the SYSTEM-token helper
starts only after consent is granted.

**Capture:** DXGI Desktop Duplication (`IDXGIOutputDuplication::AcquireNextFrame`
with a timeout, so it **blocks** rather than polls; it also provides dirty rects
and move rects, which map directly onto `TILE` and `COPYRECT` frames). Fallback
to GDI `BitBlt` from the desktop DC with a 15 fps cap and tile-hash diffing when
duplication is unavailable (`DXGI_ERROR_UNSUPPORTED`, some virtual GPUs, Session
0). All COM calls go through `golang.org/x/sys/windows` syscalls — **no new
module dependencies, no cgo**.

**Input:** `SendInput` with `KEYEVENTF_SCANCODE` for keys (the endpoint's layout
interprets them, §5.4), `KEYEVENTF_UNICODE` for the "type text" box, and
absolute `MOUSEEVENTF_VIRTUALDESK` coordinates. **Ctrl+Alt+Del** (phase 2) is
sent by the **service**, not the helper, with `SendSAS(FALSE)`, which works
only when the `SoftwareSASGeneration` policy permits services. The agent never
changes that policy itself (OQ-5).

**UI (both in the helper, Win32 via `user32` syscalls, no toolkit):**

- *Consent dialog:* technician display name, organisation, reason, "this
  session will be recorded" when it will be, a countdown, **Allow** and
  **Decline**. Topmost, centred on the primary monitor, default button
  **Decline**, no text input. Timeout = decline.
- *Indicator:* a topmost, non-activating border around the shared monitor plus a
  small bar with "*Name* from *Org* is viewing/controlling your screen", a
  **Stop control** button (drops to view-only) and an **End** button (ends the
  session). The window is excluded from capture with
  `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`, so the technician does not
  see it in the stream. It cannot be hidden by the technician.

### 2.5 macOS helper (phase 4)

- The daemon (root, `LaunchDaemon`) cannot capture: TCC attaches Screen
  Recording and Accessibility to a process in the user's GUI session. The
  handler starts the helper with `launchctl asuser <uid>` into the console
  user's bootstrap namespace (console user from `SCDynamicStoreCopyConsoleUser`).
- Capture: **ScreenCaptureKit** (`SCStream`, macOS 12.3+). `CGDisplayStream` is
  the fallback for older releases only. Input: `CGEventPost` on the HID tap,
  which requires the Accessibility grant.
- **TCC without MDM:** neither permission can be pre-granted. PPPC profiles
  could only allow a *standard user* to approve Screen Recording anyway, and
  there is no MDM here. The flow:
  1. On install, the agent writes nothing to TCC (it cannot and must not).
  2. On the first session, the helper checks `CGPreflightScreenCaptureAccess` /
     `AXIsProcessTrusted`. If either is missing it shows a one-screen
     explanation with a button that opens the right System Settings pane, and
     the session reports `outcome: permission_missing` with which one.
  3. The device page shows per-device TCC state (reported in the job result
     and cached on the device row), so technicians know before trying.
  Grants are bound to the helper's code-signing identity: the macOS binary must
  be Developer ID signed and notarized with a **stable** identifier, or every
  agent update silently revokes the grants.
- The macOS login window cannot be captured by this design. Attended only on
  macOS.
- Build: ScreenCaptureKit is Objective-C, block- and delegate-based. The darwin
  agent is built with **cgo on a `macos-latest` runner** (where codesign and
  notarisation already have to run) instead of cross-compiled with
  `CGO_ENABLED=0` from Linux. See OQ-6.

### 2.6 Linux servers (phase 5)

- **X11 only:** capture with MIT-SHM (`XShmGetImage`) plus XDamage for dirty
  regions; input with XTEST. Implemented against the X11 wire protocol via
  `github.com/jezek/xgb` (BSD-3, pure Go), the only new dependency this spec
  adds anywhere. The helper runs as the display owner with that user's
  `XAUTHORITY`.
- **Wayland is not supported.** Capture on Wayland goes through
  `xdg-desktop-portal`, which asks the local user every session and has no
  unattended mode. The handler detects a Wayland session and ends with
  `outcome: unsupported_display`.
- **Headless servers have no screen to share.** Most Linux servers are headless;
  for them the right tool is a shell, not this module (OQ-4).

---

## 3. Transport and security model

### 3.1 Transport choice

Two options were weighed:

**(a) WebRTC**, server as TURN relay. **(b) `rsp/1`**, framed binary over a
second outbound mTLS WebSocket. **Chosen: (b).**

| Criterion | (a) WebRTC | (b) rsp/1 over WSS |
|---|---|---|
| No endpoint listening port | Holds, because ICE candidates are outbound and relayed | Holds; same dial pattern as `internal/transport` |
| NAT / corporate egress | Needs UDP 3478 + relay range, or TURN-over-TLS on 443 as a fallback | Same 8443/TCP the agent already reaches |
| Latency | Better on lossy WAN (no head-of-line blocking) | TCP; mitigated by ack-window flow control (§3.3). Fine for helpdesk |
| Bandwidth | Congestion-controlled video | Near zero when static; tiles are heavier on video content |
| Binary size and deps | Pion stack (ICE, DTLS, SRTP, SCTP): many modules, several MB | Zero new modules on Windows |
| Server side | coturn or pion/turn container, UDP ports in compose and firewall | One new path on the existing gateway |
| Idle budget | Nothing at idle either way | Nothing at idle either way |
| Browser | Universal | Universal (WebSocket + canvas); WebCodecs only for the optional H.264 |

The deciding points: the operator requires all media to go through the ITMS
server, which removes WebRTC's main advantage (direct peer-to-peer paths), while
its costs (TURN, UDP, a large dependency tree in an agent CLAUDE.md requires to
stay minimal) remain. (b) reuses the port, trust anchor and dial code the fleet
already uses.

**Rejected also:** a raw TCP mTLS relay port (a second published port,
different firewall rule, same result); streaming through the existing control
WebSocket (media on the command channel would delay job frames and heartbeats
behind video).

### 3.2 Binding the media session to the signed job

- The relay token (`mint_relay_token()`, 32 bytes) and `connect_by` travel only
  inside the **signed** `remote.screen.open` params. The accepted exposure in
  parent spec §6 (raw token in the `jobs` row) applies unchanged and stays
  bounded: one-shot, 60 s, device-bound.
- The gateway accepts an agent leg only if **all** hold: mTLS cert resolves to a
  non-revoked agent (same check as `_handle`); that agent's `device_id` equals
  the registration's; `sha256(token)` matches (`hmac.compare_digest`); the
  Redis entry still exists (`GETDEL`, so a second attach fails); the session row
  is in `awaiting_consent` or `connecting`. Any failure closes 4403 and writes
  `remote.relay.identity_mismatch` or `remote.relay.token_rejected`.
- The viewer ticket is a second 32-byte secret, minted with the relay token,
  held only in api memory and the Redis entry as a hash, and presented by the
  api's viewer leg to `/relay-internal`. The browser never sees either secret.

### 3.3 `rsp/1` framing

Binary WebSocket messages. Byte 0 = type, bytes 1–4 = big-endian sequence,
then a type-specific header and payload. The full byte layout belongs in
`docs/kernel/wire-protocol/remote-screen-protocol.md` (new, phase 1).

Agent → viewer:
`HELLO` (protocol version, capability list, monitors with geometry and DPI,
codecs), `FRAME_BEGIN`, `TILE` (rect, codec `jpeg|fill`, payload), `COPYRECT`
(src, dst), `FRAME_END`, `CURSOR_SHAPE`, `CURSOR_POS`, `STATE` (consent_pending,
consent_granted, consent_denied, consent_timeout, control_revoked_by_user,
input_desktop_unavailable, privacy_blank_on/off, ended{reason}).

Viewer → agent:
`ACK` (last complete frame seq), `POINTER` (x, y, buttons), `WHEEL`, `KEY`
(KeyboardEvent.code, down/up), `TEXT` (UTF-8 string, ≤ 256 bytes),
`MONITOR_SELECT`, `QUALITY` (low/medium/high/auto), `REFRESH`, `SAS` (phase 2),
`CONTROL` (request view-only or control).

Rules:
- **Flow control:** the agent does not start a new frame while more than two
  frames are unacknowledged. Dirty regions accumulate meanwhile, so a slow link
  gets fewer, larger updates instead of a growing queue.
- **Direction is enforced at the gateway:** it drops and counts any
  viewer→agent type arriving from the agent leg and vice versa. The gateway
  otherwise does not parse payloads.
- **Unknown types are ignored** by both ends (a newer viewer against an older
  agent). `HELLO` carries capabilities so the viewer knows what it may send.
- **The agent validates every input frame:** allowlisted types only,
  coordinates clamped to the selected monitor, key codes from a fixed table,
  `TEXT` length-capped, rate-limited to 500 input events/s. Input frames are
  dropped entirely while the session is view-only or the user has pressed Stop
  control.

### 3.4 Consent semantics

- **Attended** is the default for every device (parent spec DR-03). The session
  sits in `awaiting_consent` until the helper reports. **Timeout = decline.**
  Default 30 s, policy range 15–120 s.
- No interactive user logged on + attended → `no_user_session`. It never
  degrades to unattended.
- The endpoint user can at any time **Stop control** (view-only) or **End**.
  Both are audited. The technician cannot re-enable control after the user has
  stopped it; they must start a new session with a new prompt.
- **Unattended** requires all of: the resolved device-group policy
  `allow_unattended = true`; the technician holds `remote.unattended`; the
  request explicitly asks for it. The existing silent downgrade to attended in
  `start_session` stays.
- **Endpoint-side opt-in for unattended (recommended; OQ-2):** the agent honours
  `consent_mode: unattended` only if its local config has
  `remote_unattended_allowed = true`, set at install time by the installer
  parameter and not settable by any job. That way a compromised control plane
  cannot turn a laptop into an unattended target by editing a policy row;
  servers opt in at install.

### 3.5 Visible indicator and privacy blanking

- The indicator (§2.4) appears in **every** session where an interactive user
  session exists, attended or unattended. Unattended does not mean silent.
- **Privacy blanking** (phase 2, policy `allow_privacy_blank`, default false):
  for unattended server work the technician may blank the physical console
  (a topmost black window excluded from capture, plus `BlockInput` for local
  input). It is refused when a user is actively logged on and the session is
  attended. It is audited on and off.

### 3.6 Credential-free

This path performs no logon and uses no stored credential. The helper inherits
the user's existing session (or the SYSTEM token in phase 2). `start_session`
does not call `vault.checkout` for `protocol = "screen"`, and the API rejects a
`credential_id` on screen sessions (422). That removes a whole class of
exposure present on the RDP path.

### 3.7 What a compromised relay or control plane can and cannot do

- **Relay compromise (gateway process):** it sees frames in plaintext (TLS ends
  at the gateway) and can inject input into a session that is **already live**.
  It cannot start a session: that needs a signed job, and the relay holds no
  signing key. Bounded by `max_duration_s` enforced on the agent, the indicator
  and the user's End button. End-to-end encryption between browser and agent is
  deliberately not in v1, because the server-side recording (DR-04) needs the
  frames. See OQ-3.
- **Signing-key compromise:** invariant 1 protects endpoints from the network,
  not from the holder of the key. Such an attacker could sign
  `remote.screen.open`. On attended devices the user still has to click Allow.
  On unattended devices the endpoint-local opt-in (§3.4) limits exposure to
  machines that opted in at install.
- **Browser compromise** (technician's session): limited to what that
  technician's RBAC permits, and every session is recorded and audited.

### 3.8 Clipboard and file transfer (not in phase 1–2)

Off by default and not implemented until phase 3. When built: text clipboard
only first, both directions separately policy-gated (existing
`allow_clipboard`), size-capped (64 KiB), each transfer audited with byte count
and direction but **not** content. File transfer uses the existing
`allow_file_transfer`, goes through object storage (not the frame stream), and
needs its own spec section before build.

### 3.9 Recording

Reuses `recording.py`, `put_remote_recording`, and the retention sweep
unchanged in behaviour. Changes:
- The api's viewer leg tees agent→viewer frames (it already sees all of them)
  into a recording as `rsp/1` frames with a millisecond timestamp prefix, saved
  as `org/{org_id}/remote-recordings/{session_id}.rsp`.
- **Spool to disk and upload multipart.** Today `put_remote_recording` takes
  `bytes` and `run_tunnel` holds a `bytearray`. An 8-hour tile stream can reach
  hundreds of MB. New `ObjectStore.put_remote_recording_stream` uses S3 multipart
  from a scratch file. The per-session cap is policy-configurable (default
  2 GiB); on the cap the session continues and the recording is marked
  truncated, both audited.
- `RecordingPlayer` gains an `.rsp` player that reuses the viewer's decoder;
  `.guac` playback stays.
- The consent prompt and indicator say "recorded" when `record = true`.

### 3.10 RBAC

| Code | New? | Grants | Default roles |
|---|---|---|---|
| `remote.control` | existing | attended view and control | super-admin, org-admin, technician |
| `remote.unattended` | **new** | unattended sessions (plus policy) | super-admin, org-admin |
| `remote.privacy_blank` | **new** (phase 2) | blanking the console | super-admin, org-admin |
| `remote.recordings.read` | existing | playback | super-admin, org-admin, auditor |
| `remote.policy.manage` | existing | policy edits | super-admin, org-admin |
| `remote.supervise` | **new** (2026-10-31, migration 0244) | stopping or joining another technician's session, on the session's client | super-admin, org-admin, and every role that held `orgs.manage` |

The viewer WebSocket and `POST /sessions/{id}/stop` admit the session's owner or
a holder of `remote.supervise` on the session's client (phase 3 design §5.3);
`orgs.manage` gave that until migration 0244.

Every route and the viewer WebSocket re-check server-side. Device scope uses the
same device-group scoping as `jobs.dispatch`. None of these are reachable by an
AI or automated actor (actor type `user` required, same rule as push install
clause 2a(e)).

> **Amended by CLAUDE.md invariant 1c (2026-10-09):** an AI connected over the
> platform's MCP server acts as the person who connected it and may reach this
> act within that person's RBAC; the reservation above now governs automated
> and in-app-AI actors only.

### 3.11 Audit events

Existing and reused: `remote.session.requested`, `remote.session.started`,
`remote.session.ended`, `remote.session.denied`, `remote.relay.identity_mismatch`,
`remote.recording_accessed`, `remote.recording_expired`, `remote.policy.changed`.

New: `remote.session.consent_prompted`, `remote.session.consent_granted`,
`remote.session.consent_timeout`, `remote.session.ended_by_user`,
`remote.session.input_enabled`, `remote.session.input_disabled` (with
`by: technician|endpoint_user`), `remote.session.sas_sent`,
`remote.session.privacy_blank` (`on|off`), `remote.relay.token_rejected`,
`remote.recording.truncated`.

Consent and user-side events originate on the gateway (it receives the `STATE`
frames) and are written there. The gateway already writes audit events with the
same HMAC key (compose comment on the `gateway` service).

---

## 4. Idle budget

### 4.1 Design

- **At idle:** the capability is one map entry. No goroutine, no timer, no file
  handle, no loaded graphics DLL, no helper process.
- **During a session:** the service holds one relay WebSocket (two goroutines:
  read and write), two pipe copy goroutines and one process-wait goroutine. All
  capture, encode and UI cost is inside the helper, a separate process with its
  own address space.
- **On end:** the helper exits, so its memory goes back to the OS entirely (the
  main reason the helper is a process and not a goroutine: Go does not promptly
  return heap to the OS, and a DXGI device plus frame buffers are tens of MB).
  The service closes the relay, pipes and process handles and returns from the
  handler.

### 4.2 Falsifiers

Each of these observations would show the budget broken. Each is a CI assertion,
not a benchmark that prints a number.

| # | Falsifier | Measured by | Runner |
|---|---|---|---|
| F1 | Registering `remote.screen.open` allocates, starts a goroutine or opens a handle | extend `registry_test.go` counter and `internal/budget` goroutine proxy | ubuntu |
| F2 | Service goroutine count after a session ≠ count before (±0 after 5 s) | integration test with fake relay and fake helper | ubuntu, windows |
| F3 | A helper process survives session end by more than 5 s | Windows: enumerate processes with parent = service PID after the test session | windows |
| F4 | The service process has `d3d11.dll`, `dxgi.dll` or `mfplat.dll` loaded, before, during or after a session | `EnumProcessModules` on the service PID | windows |
| F5 | Service retained heap after 20 sequential sessions grows > 1 MiB over the baseline after 1 | `runtime.ReadMemStats` after forced GC, the budget package's accumulation proxy | ubuntu, windows |
| F6 | Any socket in LISTEN state owned by the agent or helper during a session | extend `listen_test.go`: `/proc/net/tcp` on Linux, `GetExtendedTcpTable` on Windows | ubuntu, windows |
| F7 | Static screen produces more than one frame per 10 s (keepalive) | encoder test with a frozen capture source | ubuntu |
| F8 | Encoder throughput on the fixed 1080p corpus below 25 fps-equivalent of dirty area | `testing.Benchmark` from a Test with a threshold, relative to a calibration loop on the same runner | ubuntu, windows |

The source-level half of F6 is already enforced: `listen_test.go`'s go/ast walk
forbids `net.Listen` in every non-test file, which covers the new package. The
IPC design (anonymous pipes, no named pipe) keeps the helper within the rule's
spirit as well as its letter: a named-pipe server is a listener in all but name,
and the spec forbids one.

Helper CPU during a session is a **design target**, not asserted: ≤ 15 % of one
core averaged over a 1080p office workload with DXGI. Measured by hand on the
phase-1 laptop and recorded in the phase report.

---

## 5. Server and UI changes

### 5.1 `backend/app/modules/remote/`

- `jobtypes.py`: add `RemoteScreenOpenParams` / `RemoteScreenOpenResult`
  (`extra="forbid"` on params, as today), register `remote.screen.open` **with a
  result handler** that writes `end_reason` from `outcome` if the session is not
  already ended (the relay path normally ends it first; the result catches the
  cases where the relay never connected). Unknown `outcome` strings are stored
  as `error` and logged, never rejected.
- `service.py`: `start_session` branches on `protocol == "screen"`: requires
  `agents.capabilities` to contain `remote.screen.open` (else 409 "agent too
  old to share its screen", which the UI shows), refuses a second live screen
  session per device (409), skips vault checkout, mints relay token and viewer
  ticket, creates the job with the long `ttl_s` (§2.3), writes the Redis
  registration in `dispatch_pending`. `_ALLOWED` is unchanged. New `end_reason`
  values (all ≤ 20 chars, fit `String(20)`): `consent_timeout`,
  `ended_by_user`, `no_user_session`, `capture_unavailable`,
  `permission_missing`, `relay_refused`, `expired`.
- `relay.py`: `SessionRelay`'s token logic moves behind a Redis-backed
  registration store; the byte pump stays and is reused by the gateway join.
- New `screen_relay.py` (runs in the gateway): agent-leg auth (§3.2), internal
  viewer-leg auth, join, direction enforcement, `STATE` → state transitions and
  audit, teardown on either side closing or on session stop published via Redis
  (`remote:stop:{session_id}`).
- New `screen_tunnel.py` (runs in the api): the `/screen` WebSocket route,
  `authorize_tunnel` reuse, dial `/relay-internal`, pump, recording tee to a
  spool file, `_finalize_and_end` reuse.
- `models.py` + migration **0100_remote_screen_share** (as built; see "Phase 0 as built"): `remote_sessions`:
  `ticket_id` (nullable FK), `alert_id` (nullable FK), `input_enabled` (bool),
  `recording_truncated` (bool). `remote_policies`: `consent_timeout_s` (int,
  30), `allow_input` (bool, true), `allow_privacy_blank` (bool, false),
  `recording_max_bytes` (bigint, 2 GiB). Permission seeds for
  `remote.unattended`, `remote.privacy_blank`. Forward-only; RLS unchanged
  (both tables already FORCE RLS).
- `backend/app/gateway/main.py`: path dispatch in `_handle`; start the internal
  listener. This is a core file: the change is a routing hook, and the
  job-dispatch path is untouched.
- **guacd:** retired with phase 1 (operator decision); see "Phase 1 as built".

### 5.2 Frontend `frontend/src/modules/remote/`

- New `ScreenViewer.tsx` (does not use guacamole-common-js): a `<canvas>`, a
  Web Worker that decodes `TILE` payloads with `createImageBitmap` and paints to
  an `OffscreenCanvas`, `COPYRECT` via `drawImage` on itself, cursor overlay.
  Sends `ACK` after each `FRAME_END`.
- Input capture: pointer events scaled from canvas to monitor coordinates;
  `KeyboardEvent.code` → `KEY`. In fullscreen, **Keyboard Lock API**
  (`navigator.keyboard.lock()`, Chromium/Edge) captures Win, Alt+Tab and Esc;
  elsewhere a toolbar offers Win, Alt+Tab, Ctrl+Esc and (phase 2) Ctrl+Alt+Del
  buttons. A "Type text" box sends `TEXT` for IME and layout-independent input.
- **Keyboard layouts:** physical codes are sent and the endpoint's active layout
  interprets them, so a technician on a US keyboard controlling a German
  endpoint gets German characters for the same physical keys. `TEXT` is the
  escape hatch when the technician wants their own characters typed.
- Toolbar: monitor switch (from `HELLO`), quality (auto/low/medium/high), view
  only ↔ control, fullscreen, refresh, end. State banner for
  `consent_pending` (with countdown), `input_desktop_unavailable`, user stopped
  control.
- `StartSessionDialog`: "Screen" is the default and, until OQ-4, the only type.
  Attended/unattended selector shown only when the resolved policy and the
  user's permissions allow unattended. Reason required.
- `RecordingPlayer`: dispatch on extension; `.rsp` uses the worker decoder.
- Entry points: **device page** "Remote" button (disabled with the reason when
  the agent lacks the capability or is offline); **alert detail** and
  **service-desk ticket** "Remote to device" buttons, which prefill the reason
  and set `alert_id` / `ticket_id` on the session, and the ticket timeline
  links to the session and recording.
- TanStack Query for session state; strict TS, no `any`; vitest for the frame
  decoder, input mapping and state banner.

### 5.3 Deploy

No new container, no new published port. `gateway` gets `ITS_RELAY_INTERNAL_PORT=8444`
(internal network only) and the api gets `ITS_RELAY_INTERNAL_URL`. Migration
0100 runs via `migrate` as usual. The operator deploy sequence for the build
that lands this will follow CLAUDE.md's reporting rules; nothing here needs a
new `.env` key that lacks a default.

---

## 6. Wire protocol changes (additive)

New files in `docs/kernel/wire-protocol/`:
`RemoteScreenOpenParams.schema.json`, `RemoteScreenOpenResult.schema.json`
(exported from `jobtypes.py` via `scripts/export_wire_schema.py`), and
`remote-screen-protocol.md` (the `rsp/1` frame layout, which is not JSON and is
versioned by WebSocket subprotocol).

`RemoteScreenOpenParams`:

```jsonc
{
  "session_id":       "uuid",
  "relay_url":        "wss://gateway:8443/relay",
  "relay_token":      "<one-shot secret>",
  "connect_by":       "2026-10-01T10:01:00Z",
  "consent_mode":     "attended|unattended",
  "consent":          { "technician": "…", "org": "…", "reason": "…", "timeout_s": 30 },
  "max_duration_s":   28800,
  "allow_input":      true,
  "record":           true,
  "desktop":          "user"            // phase 2 adds "secure" (SYSTEM-token helper)
}
```

`RemoteScreenOpenResult`: `outcome` (`ended|denied|consent_timeout|no_user_session|
capture_unavailable|permission_missing|unsupported_display|relay_refused|expired|error`),
`duration_s`, `detail` (string), `platform_state` (optional object, e.g. macOS
TCC booleans).

Compatibility, both directions:

- **New control plane → old agent.** An old agent has no `remote.screen.open`
  in its registry and refuses `unknown_type` before anything runs. The server
  checks `agents.capabilities` first, so in practice the job is not dispatched
  and the UI explains why.
- **New agent → old control plane.** The capability is only ever invoked, never
  initiated. An old control plane never sends the job; the agent never dials
  `/relay` on its own.
- **No existing enum is widened.** `JobStatusMsg.state`, `JobResultMsg.state`
  and `RemoteSessionOpenParams.protocol` are untouched. Consent travels as an
  `rsp/1` `STATE` frame, and the outcome lives in a new result schema.
- **Gateway path dispatch is additive:** only the exact path `/relay` changes
  behaviour; every other path is today's agent channel.
- **Params evolve by optional fields with defaults.** The agent ignores unknown
  params members (the cert-install precedent). The server's `extra="forbid"`
  applies to what the server itself builds, not to agent input.
- **`rsp/1` is a three-party contract** (agent, gateway direction filter,
  browser). A browser build and an agent build differ in age just like the
  server and the agent do. Any incompatible frame change is `rsp/2` with
  subprotocol negotiation, and the agent keeps speaking `rsp/1` for at least one
  release after.
- `agent/internal/wire/schema_test.go`: remove `RemoteSessionOpenParams`'s
  "no agent capability planned" entry only if that type gets a capability
  (it will not; its reason is updated to "superseded by remote.screen.open for
  console sharing"). Add the two new schemas with the "unmarshalled by
  internal/capabilities/remote, which asserts against this schema in its own
  schema_test.go" reason, and write that test.

---

## 7. Phasing

Each phase's definition of done requires running on the real OS. GitHub-hosted
`windows-latest` and `macos-latest` runners can execute Go tests and have an
interactive desktop session; consent UI and real EDR behaviour are proven on
physical machines.

### Phase 0 — Relay plumbing (server only)
Gateway `/relay` path dispatch, Redis registration, internal listener, api
viewer leg, direction filter, recording spool and multipart. **Done when:**
backend tests drive a fake agent (mTLS test cert) and a fake viewer through
consent → frames → end, including token reuse, wrong device, expired
`connect_by` and revoked-cert refusals, each with its audit row; a live-stack
test on the dev compose shows an `rsp/1` echo agent reaching a browser page.

#### Phase 0 as built (2026-09-23)

- **Gateway** (`backend/app/gateway/main.py`): `/relay` goes to
  `app/modules/remote/screen_relay.py`; every other path is the agent channel,
  unchanged. `itms-rsp.1` is negotiated with a selector that also accepts
  clients offering no subprotocol (passing `subprotocols=` to `serve` would
  have answered every deployed agent with HTTP 400). The internal listener
  serves `/relay-internal` on `ITS_RELAY_INTERNAL_PORT` (8444).
- **Registration** in Redis (`RelayRegistry`, `remote:relay:{id}`), consumed
  with GETDEL. It also carries `consent_mode`, `allow_input`,
  `max_duration_s` and `consent_timeout_s`, which the gateway enforces itself.
- **Handshake:** the relay token and the viewer ticket travel in an `AUTH`
  frame (`0xF0`), answered by `AUTH_OK` (`0xF1`): a hop-by-hop range the
  gateway never forwards. Full layout in
  `docs/kernel/wire-protocol/remote-screen-protocol.md`, bytes pinned by
  `rsp1-vectors.json`.
- **Gateway filtering beyond §3.3:** before consent only STATE reaches the
  viewer and nothing reaches the agent; input frames are dropped while
  view-only; `CONTROL(1)` is dropped after the user pressed Stop control or
  when policy disallows input. The endpoint still validates everything itself.
- **Unattended** follows the 2026-09-23 decision: policy `allow_unattended`
  plus the new `remote.unattended` permission plus the request. No
  endpoint-side opt-in (§3.4's recommendation is superseded).
- **Migration** is `0100_remote_screen_share` (not 0083, which the chain had
  already used). `ticket_id` / `alert_id` are left for phase 2, with the entry
  points that set them.
- **guacd:** RDP/VNC/SSH are refused by the API unless
  `ITS_REMOTE_GUACD_ENABLED=true`; `api` no longer waits on the `guacd`
  container, which stays in compose until phase 1 ships and is then deleted.
- **Test double:** `agent/internal/rsp/fakeagent` (Go, never packaged) is
  built and driven by `backend/tests/test_remote_screen_relay.py` through the
  real gateway over mTLS. The live-stack browser check on the dev compose is
  not done yet; the same path is covered by a test that runs the real
  `/screen` route under uvicorn against the real gateway.
- **Agent capabilities** are written only at enrollment today, so an agent
  that gains `remote.screen.open` by self-update must re-advertise it, or the
  API keeps answering 409. Phase 1 has to close that.

### Phase 1 — Windows attended view and control
Capability, user-token helper, DXGI + GDI capture, JPEG tiles, SendInput,
consent dialog, indicator, Stop control and End, `ScreenViewer`, device-page
entry, recording and playback. **Done when:**
- New `agent-windows` CI job on `windows-latest` runs the capability's tests,
  F2–F6 and F8, and an end-to-end test that installs the agent as a service,
  opens a session against a fake relay, verifies frames of a known test window
  and a round-tripped click, and asserts no helper survives.
- On a **real Windows 11 laptop** with Check Point Harmony Endpoint active:
  consent appears in the logged-on user's session; Decline and timeout both
  end with the right audit; accept shows the desktop within 3 s at 1080p;
  typing on a non-US endpoint layout works; End from the indicator ends the
  session; recording plays back; no Harmony block or alert (or a documented,
  signer-based exclusion).
- Windows agent binary Authenticode-signed (OQ-1) before any laptop outside the
  test bench runs it.

#### Phase 1 as built (2026-09-23)

What exists, where it differs from the text above, and what is still owed.
The definition of done above is **not** met yet: the Windows CI job runs, but
the laptop checks and the service-install end-to-end test have not.

**Agent: the capability** (`agent/internal/capabilities/remote`).
- `remote.screen.open` is registered only where this build has a helper:
  `remote.Supported` is `true` on Windows and `false` elsewhere, so the
  enrollment advertisement is truthful. The dispatcher key is `remote`, not a
  heavy key.
- The handler checks the params (wss only, `connect_by`, `desktop` of `user`),
  dials `relay_url` with the agent's certificate (re-read per dial) and the
  pinned CA, negotiates `itms-rsp.1`, sends AUTH and waits for AUTH_OK. A
  refusal is `outcome: relay_refused` and no helper starts.
- It launches the helper with `WTSGetActiveConsoleSessionId` →
  `WTSQueryUserToken` → `DuplicateTokenEx` → `CreateEnvironmentBlock` →
  `CreateProcessAsUser(<agent.exe> remote-helper)` on `winsta0\default`,
  created suspended with exactly three inheritable handles (stdin, stdout and
  stderr pipes, `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`), placed in a
  kill-on-close job object, then resumed. A crashed service takes the helper
  with it. No user at the console is `no_user_session`, in both modes.
- Service ↔ helper framing is `ipc` (length | kind | body: Config, Frame,
  Stop), not a wire contract. The service forwards only agent→viewer frames
  from the helper and only viewer→agent frames from the relay, and before the
  helper reports `consent_granted` it forwards STATE only and nothing to the
  helper.
- Teardown: Stop to the helper (it sends `STATE ended{reason}` itself), 5 s
  for it to exit, then `TerminateProcess`; relay closed with 4000; every
  goroutine joined before the handler returns. A cancelled job ends the
  session as `completed`; the time limit is `max_duration_s` +
  `consent_timeout_s`, as the gateway's.
- `internal/capabilities/remote/relayagent` is a never-packaged double that
  runs this capability's whole network side from the real signed-job runner.

**Agent: the helper** (`.../remote/helper`, Windows platform in `_windows.go`).
- Refuses to run unless stdin and stdout are pipes and its parent process
  has the same image name (a guard against a manual start, not a boundary).
  Per-monitor DPI aware V2.
- Consent: technician, organisation, reason, "recorded", a countdown, Allow
  and Decline. Decline is the default button; Esc, Enter with nothing
  focused, the close box and the timeout all decline. Text from the signed
  params is stripped of control and bidi-override characters and capped.
  Unattended skips the prompt only; the indicator still shows (operator
  decision 2026-09-23).
- Indicator: a topmost, never-activated bar at the top of the shared monitor
  ("*Name* from *Org* is controlling/viewing your screen (recorded)", Stop
  control, End) and a four-edge border, all excluded from capture
  (`WDA_EXCLUDEFROMCAPTURE`, `WDA_MONITOR` before 2004). The close box does
  nothing; the bar reasserts topmost every 2 s.
- Capture: DXGI Desktop Duplication through raw COM vtables (no cgo), the
  device created on the adapter that owns the output (hybrid-GPU laptops);
  blocks in `AcquireNextFrame`; dirty and move rects become the tiler's hints
  and COPYRECTs. GDI `BitBlt` at 15 fps is the fallback (no duplication, a
  rotated output, RDP). A secure-desktop switch sends
  `STATE input_desktop_unavailable` and retries each second; recovery resends
  HELLO and a full frame.
- Encoding: 64 px grid compared against what the viewer has; solid tiles are
  fills, changed neighbours merge into runs of up to eight tiles, stdlib
  JPEG on four workers joined per frame. QUALITY maps to JPEG 40/60/85; auto
  starts at 70 and drops while the viewer holds the flow window. Frames start
  on the first REFRESH; at most 30 frames per second.
- Input: SendInput with scan codes from a fixed table, `KEYEVENTF_UNICODE`
  for TEXT, absolute `VIRTUALDESK` coordinates, wheel converted from DOM
  sense. Rate-limited to 500/s, clamped, dropped before consent and while
  view only; leaving control releases held keys and buttons.

**Advertisement.** Capabilities are still sent only at enrollment. An agent
that gains `remote.screen.open` by self-update keeps answering the API's 409
until connect-time capability advertisement lands (being built separately;
this build does not touch the connect handshake). Until then such a device
must re-enroll.

**Server.** `SessionOut.input_enabled` (additive) lets the viewer start view
only when policy disallows input or the user already took control back.
Nothing else in the relay changed.

**Console** (`frontend/src/modules/remote`). `ScreenViewer` replaces the
Guacamole viewer: a canvas (JPEG tiles decoded by `createImageBitmap`, which
is off the main thread in browsers, so no Web Worker was added), ACK after
each frame is drawn, REFRESH on every HELLO, pointer/wheel/keys only while in
control and focused, keys released on blur, view-only toggle, monitor and
quality, Win / Alt+Tab / Ctrl+Esc buttons, "Type text", Keyboard Lock in full
screen, and the state in plain words (consent countdown, secure desktop,
control taken back, why it ended). `RecordingPlayer` plays `.rsp` with the
same decoder and keeps `.guac` for old recordings. "Remote in" is on the
device page and on a ticket's linked device (reason prefilled with the ticket
reference; `ticket_id` on the session is still phase 2).

**guacd retired.** The service is gone from `docker-compose.yml`; the
`/tunnel` route, the media tunnel, the Guacamole client and byte bridge and
the `ITS_REMOTE_GUACD_*` settings are deleted; RDP/VNC/SSH are refused. An
old `.env` naming those keys is ignored. The service layer's in-process
`SessionRelay` and non-screen branch are now unreachable from the API and
can be removed with their tests later.

**Idle budget.**

| # | Status |
|---|---|
| F1 | Asserted on every OS: `registry_test.go` registers the real handler and measures 0 allocs per lookup and no goroutine. |
| F2 | Asserted on every OS through a real child process (`TestFalsifierF2AndF3...`). |
| F3 | Asserted on every OS (the helper exited) and on Windows by process enumeration; the kill-on-close job is tested. |
| F4 | Asserted on windows-latest before, during and after a real helper session (`EnumProcessModules`). |
| F5 | Asserted on every OS: 20 sessions with an in-process helper, growth ~0.2 MiB (test harness included) against 1 MiB. |
| F6 | Source level on every OS (`listen_test.go` walks the package); on windows-latest, `GetExtendedTcpTable` for the helper's PID during a session. |
| F7 | Asserted on every OS for a blocking and a polling capturer: zero frames from a static screen. |
| F8 | Asserted on every OS against a same-machine stdlib-JPEG calibration: full-screen 1080p re-encode, 19-47 fps measured under load on a 4-vCPU box. |

Helper CPU during a session (≤ 15 % of a core) is **not measured**; it is
owed from the laptop.

**Windows CI.** `agent-windows` runs every test above plus GDI and DXGI
capture of the runner's desktop, SendInput round trips (pointer position, key
state), consent timeout and cancellation, the indicator, the real helper
through the production launch path with the runner's own token, and the
LocalSystem launcher (which skips, naming why, unless the runner is SYSTEM).
Every check that needs an interactive desktop skips with its reason rather
than passing.

**Owed before phase 1 is done**
- The `agent-windows` service-install end-to-end test (agent as a service,
  real WTS launch, known test window, round-tripped click).
- The laptop checklist above, on Windows 11 with Check Point Harmony, plus:
  a 150 % scaled display and a second monitor; a hybrid-GPU laptop (DXGI
  on the discrete adapter); a UAC prompt mid-session; the indicator's
  exclusion from capture; helper CPU; whether Harmony blocks
  `CreateProcessAsUser` into the session, DXGI duplication or `SendInput`
  from an unsigned binary (operator decision: unsigned until the pilot).

### Phase 2 — Windows unattended and servers
SYSTEM-token helper mode, Winlogon and lock screen, UAC secure desktop, SAS,
session picker (console vs RDP sessions via `WTSEnumerateSessions`), privacy
blanking, endpoint-local unattended opt-in, alert and ticket entry points.
**Done when:** on real Windows Server 2022 with no user logged on, an
unattended session shows the logon screen, Ctrl+Alt+Del works when the SAS
policy allows it and reports a clear refusal when not; on the Windows 11 laptop
a UAC prompt can be answered by the technician; an unattended request to a
device without the local opt-in is refused and audited.

### Phase 3 — Quality and convenience
Optional H.264 via Media Foundation (hardware MFT where present) decoded with
WebCodecs, negotiated in `HELLO`, tiles as fallback; all-monitors view; text
clipboard behind policy. **Done when:** a 1080p video playing on the endpoint
uses less bandwidth than tiles at equal perceived quality on the test laptop,
and a Windows Server without Media Foundation negotiates tiles without error.

### Phase 4 — macOS attended
LaunchAgent-context helper, ScreenCaptureKit, CGEvent, TCC onboarding, per-device
TCC state, cgo darwin build on `macos-latest`, Developer ID signing and
notarisation. **Done when:** on a real Mac without MDM, the first session walks
a user through both grants, the second session works with no prompt, and an
agent update keeps the grants.

### Phase 5 — Linux X11
xgb-based capture and XTEST input, Wayland and headless detection with clear
outcomes. **Done when:** a real X11 desktop session is shared and controlled,
and a Wayland session and a headless server each end with the correct outcome.

**Timeline against a January 2027 sale with testing through December:**
phases 0–2 are the realistic scope for the sellable build. Phases 3–5 are
post-launch unless the operator decides otherwise (OQ-7).

---

## 8. Risks and open questions

### 8.1 Risks

- **EDR (Check Point Harmony Endpoint).** A SYSTEM service that launches a
  process into the user's session, which then duplicates the desktop and calls
  `SendInput`, matches the behavioural signature of a remote-access trojan.
  Expect detections until the binary is signed with a stable publisher and a
  signer-based exclusion is in the Harmony policy. Mitigation: phase 1's DoD
  includes the Harmony laptop. Unknown until tested: whether Harmony's
  anti-keylogger or screen-capture protection blocks DXGI duplication even with
  an exclusion.
- **Code signing.** The Windows agent is unsigned today (`agent/packaging/build.sh`,
  `authenticode-signing.md`). The helper is the same executable, so one
  signature covers both, but an unsigned binary doing this will be flagged by
  Defender and EDRs and will not survive a customer's security review.
  SmartScreen itself does not gate service-launched processes (it applies to
  files with Mark-of-the-Web), so it is a distribution-path concern, not a
  runtime one.
- **DXGI on CI runners.** Hosted runners have no GPU; duplication on the
  Microsoft Basic Display Adapter normally works but is unverified here. The
  GDI fallback keeps CI meaningful either way; the test reports which path ran.
- **Pure-Go JPEG speed.** Stdlib `image/jpeg` may be too slow for full-screen
  changes at 1080p+. F8 gates it. If it fails, phase 1 switches to the Windows
  Imaging Component JPEG encoder via COM syscalls (OS-provided, still no new
  module), not to cgo.
- **TCP head-of-line blocking** on lossy links makes latency spiky. Flow control
  bounds queueing; it does not remove loss recovery delay. Acceptable for
  helpdesk; not for video.
- **High-DPI and mixed-DPI monitors** need the helper to be per-monitor DPI
  aware (`SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)`) or coordinates
  drift. Covered by a phase-1 test on a 150 % scaled display.
- **macOS re-prompts.** Recent macOS releases periodically ask the user to
  re-confirm screen recording for some capture APIs. The exact current
  behaviour must be checked against Apple's documentation before phase 4.
- **Recording contents are sensitive.** Recordings show whatever the user had
  on screen. Retention (90 days default) and `remote.recordings.read` apply; the
  consent text says so.

### 8.2 Operator decisions needed

- **OQ-1 Windows code signing before phase-1 laptop testing.** (a) OV
  certificate on a cloud HSM (e.g. Azure Trusted Signing), (b) EV on a hardware
  token, (c) test unsigned on the bench only and sign before pilot.
  *Recommended: (a).*
- **OQ-2 Endpoint-side opt-in for unattended.** (a) Required: set at install,
  no job can change it; (b) not required: control-plane policy alone decides;
  (c) required on laptops only (installer flag defaults differ by device
  class). *Recommended: (a).*
- **OQ-3 End-to-end encryption of the stream.** (a) No: the server must record,
  so it must see frames (this spec); (b) optional E2E per session with
  recording disabled for that session; (c) E2E with the recording encrypted to
  an auditor key. *Recommended: (a) for v1.*
- **OQ-4 The guacd path.** (a) Keep for SSH to Linux servers only and build
  that agent half next; (b) retire guacd entirely and remove it from compose;
  (c) keep RDP/VNC/SSH as an optional server feature. *Recommended: (a).*
- **OQ-5 Ctrl+Alt+Del on endpoints.** (a) The agent never touches the
  `SoftwareSASGeneration` policy; customers set it by GPO (SAS button disabled
  otherwise); (b) the installer sets it to "services" on servers only;
  (c) installer sets it everywhere. *Recommended: (a).*
- **OQ-6 macOS build.** (a) cgo darwin build on a `macos-latest` runner (this
  spec); (b) keep `CGO_ENABLED=0` and call ScreenCaptureKit through a pure-Go
  Objective-C bridge (one new dependency, fragile with blocks and delegates);
  (c) separate macOS helper binary, signed separately. *Recommended: (a).*
- **OQ-7 What is in the January 2027 sellable scope.** (a) Windows only
  (phases 0–2); (b) Windows plus macOS attended (phases 0–2 and 4, pushing
  phase 3); (c) everything through phase 5. *Recommended: (a).*
- **OQ-8 Default consent timeout and unattended indicator.** (a) 30 s, and the
  indicator is always shown when a user session exists (this spec); (b) 60 s,
  indicator always; (c) 30 s, indicator optional per policy for unattended.
  *Recommended: (a).*

## Operator decisions (2026-09-23)

Answered by the operator; these supersede any "open question" above.

| Question | Decision |
|---|---|
| Scope for the January 2027 sale | **Windows attended and unattended, plus macOS attended.** Linux X11 follows after launch. |
| Windows code signing | **Unsigned until the pilot.** Bench testing runs unsigned; a certificate is bought and signing added before any real laptop rollout. |
| Unattended access | **Server policy only** (RBAC permission + org/device policy). No install-time opt-in switch on the endpoint. |
| guacd | **Retire it.** Screen share becomes the only remote path; guacd leaves the compose stack once phase 1 ships. |
| End-to-end encryption | No: the relay must see frames to record them. |
| Ctrl+Alt+Del policy | The agent never changes the SAS policy; customers set it by GPO where needed. |
| macOS build | cgo build on a `macos-latest` runner. |
| Consent timeout / indicator | 30 s timeout (no answer = refused); the "being assisted" indicator is always shown when a user is logged on. |
