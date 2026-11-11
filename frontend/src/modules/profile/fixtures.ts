/**
 * Fixtures for the profile module: `PATCH /me` and your security page.
 *
 * `me` itself is the shell's, in `fixtures/core.ts` -- every route reads it --
 * so `updateMe` answers from that same record and the name in the top bar and
 * the name in the form agree. The server writes the four profile fields and
 * nothing else, and so does this.
 *
 * YOUR SECURITY (`GET /me/security` and its writes) is a small state the
 * writes change, so the page behaves under the harmony gate as it does against
 * the API. The authenticator set-up and passkey registration are the sign-in
 * module's fixtures (`enrollTotpStart`, `enrollPasskeyOptions`, ...), not
 * these: one method, one fixture. The recent confirmation answers "accepted".
 *
 * NOTHING SECRET IS SERVED: a passkey is its name and dates; the recovery
 * codes below are a fixture's, valid nowhere.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import { fixtures as core } from "../../fixtures/core";
import type { Me } from "../../lib/api";
import type { MySecurity } from "./types";

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const at = (offset: number): string => new Date(Date.now() + offset).toISOString();

/** The stored profile fields, once a save has replaced the seed's. */
let stored: Partial<Pick<Me, "display_name" | "phone" | "timezone" | "locale">> = {};

const updateMe: FixtureMap["updateMe"] = async (body) => {
  const current = await core.me!();
  stored = {
    display_name: body.display_name,
    phone: body.phone ?? undefined,
    timezone: body.timezone ?? undefined,
    locale: body.locale ?? undefined,
  };
  return { ...current, ...stored };
};

let security: MySecurity = {
  has_totp: true,
  passkeys: [
    { id: "pk-laptop", name: "Work laptop", created_at: at(-40 * DAY), last_used_at: at(-HOUR), backup_state: false },
    { id: "pk-phone", name: "Phone", created_at: at(-12 * DAY), last_used_at: null, backup_state: true },
  ],
  recovery_codes_left: 9,
  trusted_devices: [
    { id: "td-this", label: "Firefox on Windows", created_at: at(-3 * DAY), expires_at: at(12 * DAY), last_used_at: at(-2 * MINUTE), current: true },
    { id: "td-home", label: "Safari on macOS", created_at: at(-9 * DAY), expires_at: at(6 * DAY), last_used_at: at(-2 * DAY), current: false },
  ],
  sessions: [
    {
      id: "ses-this",
      created_at: at(-50 * MINUTE),
      last_seen_at: at(-MINUTE),
      expires_at: at(11 * HOUR),
      user_agent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0",
      ip: "10.20.4.18",
      current: true,
    },
    {
      id: "ses-mac",
      created_at: at(-5 * HOUR),
      last_seen_at: at(-25 * MINUTE),
      expires_at: at(7 * HOUR),
      user_agent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
      ip: "10.20.7.41",
      current: false,
    },
  ],
};

const CODES = [
  "7KQ2M-X9D4P", "3HV8N-QW2ZR", "M4T6C-8YJKD", "R2XPA-5N7GE", "Z9W3K-HT6QV",
  "C8D5M-2PRXN", "Q6J4T-W8ZAH", "N3F7V-K2MXD", "E5Y9R-6TQPC", "W2H8K-D4NZJ",
];

const getMySecurity: FixtureMap["getMySecurity"] = () => security;

const changePassword: FixtureMap["changePassword"] = () => {
  // A changed password forgets every remembered device and ends the other
  // sessions (design §2.6, `account.change_password`; final review D2,
  // Minor 4: the other sessions were kept here).
  security = {
    ...security,
    trusted_devices: [],
    sessions: security.sessions.filter((session) => session.current),
  };
};

const removePasskey: FixtureMap["removePasskey"] = (passkeyId) => {
  security = { ...security, passkeys: security.passkeys.filter((passkey) => passkey.id !== passkeyId) };
};

const removeTotp: FixtureMap["removeTotp"] = () => {
  security = { ...security, has_totp: false };
};

const regenerateRecoveryCodes: FixtureMap["regenerateRecoveryCodes"] = () => {
  security = { ...security, recovery_codes_left: CODES.length };
  return { recovery_codes: [...CODES] };
};

const revokeTrustedDevice: FixtureMap["revokeTrustedDevice"] = (deviceId) => {
  security = { ...security, trusted_devices: security.trusted_devices.filter((device) => device.id !== deviceId) };
};

const endMySession: FixtureMap["endMySession"] = (sessionId) => {
  security = { ...security, sessions: security.sessions.filter((session) => session.id !== sessionId) };
};

const stepUpTotp: FixtureMap["stepUpTotp"] = () => undefined;

const stepUpPasskeyOptions: FixtureMap["stepUpPasskeyOptions"] = () => ({
  challenge: "Zml4dHVyZS1zdGVwLXVw",
  rpId: "localhost",
  allowCredentials: [],
  userVerification: "required",
  timeout: 60_000,
});

const stepUpPasskey: FixtureMap["stepUpPasskey"] = () => undefined;

const stepUpRecovery: FixtureMap["stepUpRecovery"] = () => undefined;

const fixtures: FixtureMap = {
  updateMe,
  getMySecurity,
  changePassword,
  removePasskey,
  removeTotp,
  regenerateRecoveryCodes,
  revokeTrustedDevice,
  endMySession,
  stepUpTotp,
  stepUpPasskeyOptions,
  stepUpPasskey,
  stepUpRecovery,
};

export default fixtures;
