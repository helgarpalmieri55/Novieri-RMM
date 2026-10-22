/**
 * Fixtures for remote control: the sessions `/remote` lists, the one
 * `/remote/sess-1` opens, its recording, and the start/stop mutations.
 *
 * Every `device_id` is a core-fleet row so the hostname join on `/remote`
 * resolves. `sess-1` is the live one the gate walks: an RDP session on the
 * finance workstation, eighteen minutes in, being recorded. Behind it sit a
 * week of ended sessions with recordings and one still waiting for the
 * person at the desk to consent.
 *
 * THE RECORDING IS A REAL, TINY GUACAMOLE LOG: four frames of flat
 * rectangles over four seconds, in the protocol's own text encoding, so
 * `/remote/sess-1/recording` actually plays something instead of drawing an
 * empty box. It was an EMPTY log, which the player correctly reported as a
 * recording with no frames — a true statement about a fixture that made the
 * one function of that route unreachable in fixture mode. Nothing here
 * pretends to be a capture of a real desktop: it is four coloured blocks,
 * and it says so.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import type { RemoteSession } from "../../lib/api";
import { ApiError } from "../../lib/api";

const MINUTE = 60_000;
const ago = (minutes: number): string => new Date(Date.now() - minutes * MINUTE).toISOString();

interface Seed {
  id: string;
  device: string;
  technician: string;
  protocol: string;
  consent: string;
  state: string;
  credential: string | null;
  recording: boolean;
  /** Minutes ago the session was created. */
  created: number;
  /** Minutes after creation it started, or null if it never did. */
  startAfter: number | null;
  /** Minutes it ran, or null if it is still open. */
  ran: number | null;
  endReason: string | null;
}

const SEEDS: Seed[] = [
  { id: "sess-1", device: "dev-1", technician: "usr-helgar", protocol: "rdp", consent: "attended", state: "active", credential: "cred-1", recording: true, created: 19, startAfter: 1, ran: null, endReason: null },
  { id: "sess-2", device: "dev-4", technician: "usr-helgar", protocol: "rdp", consent: "attended", state: "awaiting_consent", credential: null, recording: false, created: 2, startAfter: null, ran: null, endReason: null },
  { id: "sess-3", device: "dev-5", technician: "usr-helgar", protocol: "ssh", consent: "unattended", state: "ended", credential: "cred-3", recording: true, created: 4 * 60 + 12, startAfter: 0, ran: 26, endReason: "technician" },
  { id: "sess-4", device: "dev-2", technician: "usr-helgar", protocol: "rdp", consent: "attended", state: "ended", credential: "cred-1", recording: true, created: 26 * 60 + 5, startAfter: 2, ran: 41, endReason: "technician" },
  { id: "sess-5", device: "dev-3", technician: "usr-helgar", protocol: "vnc", consent: "attended", state: "ended", credential: null, recording: true, created: 2 * 24 * 60 + 30, startAfter: 1, ran: 9, endReason: "user" },
  { id: "sess-6", device: "dev-8", technician: "usr-helgar", protocol: "rdp", consent: "unattended", state: "ended", credential: "cred-1", recording: false, created: 3 * 24 * 60, startAfter: null, ran: null, endReason: "device_offline" },
  { id: "sess-7", device: "dev-14", technician: "usr-helgar", protocol: "ssh", consent: "attended", state: "ended", credential: "cred-3", recording: true, created: 5 * 24 * 60 + 15, startAfter: 1, ran: 14, endReason: "technician" },
  { id: "sess-8", device: "dev-1", technician: "usr-helgar", protocol: "rdp", consent: "attended", state: "ended", credential: "cred-1", recording: true, created: 6 * 24 * 60 + 40, startAfter: 3, ran: 22, endReason: "technician" },
];

function session(seed: Seed): RemoteSession {
  const started = seed.startAfter === null ? null : ago(seed.created - seed.startAfter);
  const ended =
    seed.startAfter === null || seed.ran === null ? null : ago(seed.created - seed.startAfter - seed.ran);
  return {
    id: seed.id,
    device_id: seed.device,
    technician_id: seed.technician,
    protocol: seed.protocol,
    consent_mode: seed.consent,
    state: seed.state,
    credential_id: seed.credential,
    has_recording: seed.recording,
    started_at: started,
    ended_at: seed.state === "ended" && seed.startAfter === null ? ago(seed.created) : ended,
    end_reason: seed.endReason,
    created_at: ago(seed.created),
  };
}

/** Every session, newest first, rebuilt per call so the durations are live. */
const sessions = (): RemoteSession[] =>
  SEEDS.map(session).sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));

function findSession(id: string): RemoteSession {
  const found = sessions().find((s) => s.id === id);
  if (!found) throw new ApiError(404, JSON.stringify({ detail: "session not found" }));
  return found;
}

/** One Guacamole instruction, in the protocol's `LEN.ELEMENT,…;` encoding. */
const inst = (opcode: string, ...args: (string | number)[]): string =>
  [opcode, ...args.map(String)].map((part) => `${part.length}.${part}`).join(",") + ";";

/** A filled rectangle on layer 0. `14` is the SRC_OVER channel mask. */
const block = (x: number, y: number, w: number, h: number, rgb: [number, number, number]): string =>
  inst("rect", 0, x, y, w, h) + inst("cfill", 14, 0, rgb[0], rgb[1], rgb[2], 255);

/**
 * FOUR FRAMES, FOUR SECONDS. `sync` closes a frame and carries its timestamp,
 * which is where `SessionRecording` gets the duration the transport control
 * reads. The picture is deliberately abstract — a desktop ground, a window,
 * a title bar, a caret that appears in the last frame — because a fixture
 * that looked like a screenshot of somebody's machine would be a lie about
 * where it came from.
 */
export const RECORDING_LOG: string =
  // Frame one is the POSTER: the page seeks to it on load, so it has to be
  // the frame that shows what this is — a desktop with a window on it, not an
  // empty ground that would look exactly like the blank panel this replaced.
  inst("size", 0, 1024, 640) +
  block(0, 0, 1024, 640, [24, 39, 48]) +
  block(96, 72, 832, 496, [249, 250, 252]) +
  block(96, 72, 832, 36, [229, 232, 238]) +
  inst("sync", 0) +
  block(128, 140, 560, 18, [102, 112, 128]) +
  inst("sync", 1200) +
  block(128, 176, 704, 18, [102, 112, 128]) +
  block(128, 212, 432, 18, [102, 112, 128]) +
  inst("sync", 2400) +
  block(128, 248, 12, 18, [31, 95, 209]) +
  inst("sync", 3600);

let minted = 0;

const fixtures: FixtureMap = {
  listRemoteSessions: (params = {}) => {
    const matched = sessions().filter(
      (s) => (!params.state || s.state === params.state) && (!params.device_id || s.device_id === params.device_id),
    );
    const offset = params.offset ?? 0;
    const limit = params.limit ?? 50;
    return { items: matched.slice(offset, offset + limit), total: matched.length };
  },

  getRemoteSession: (id) => findSession(id),

  startRemoteSession: (body) => {
    minted += 1;
    return {
      session_id: `sess-new-${minted}`,
      state: "awaiting_consent",
      consent_mode: body.mode ?? "attended",
    };
  },

  stopRemoteSession: (id) => ({ ...findSession(id), state: "ended", ended_at: ago(0), end_reason: "technician" }),

  getRemoteRecording: (id) => {
    const found = findSession(id);
    if (!found.has_recording) throw new ApiError(404, JSON.stringify({ detail: "no recording" }));
    return new Blob([RECORDING_LOG], { type: "application/octet-stream" });
  },
};

export default fixtures;
