/**
 * Your own sign-in security, as `GET /api/v1/me/security` serves it (own
 * authentication design §2.2, §2.4, §2.6). NOTHING SECRET IS IN IT, by the
 * server's construction and by this file's: a passkey is its name and dates, a
 * remembered device and a session are where and when. The values a person sees
 * once -- the authenticator key, new recovery codes -- come in the answer to
 * their own write and are never part of this read.
 */

export interface MyPasskey {
  id: string;
  /** The name the person gave it when they added it. Recorded text. */
  name: string;
  created_at: string;
  last_used_at: string | null;
  /** Synced by a password manager or a platform account (WebAuthn backup state). */
  backup_state: boolean;
}

/** A device that skips the second factor until `expires_at` (design §2.6). */
export interface MyTrustedDevice {
  id: string;
  /** What the server called it when it was remembered ("Firefox on Windows"). Recorded text. */
  label: string;
  created_at: string;
  expires_at: string;
  last_used_at: string | null;
  /** This browser is that device. */
  current: boolean;
}

/** One signed-in session (design §2.4). */
export interface MySession {
  id: string;
  created_at: string;
  last_seen_at: string;
  expires_at: string;
  /** As the browser sent it. Recorded text. */
  user_agent: string;
  ip: string;
  /** The session this console is using: it ends by signing out, not from this list. */
  current: boolean;
}

/** The Microsoft account linked to this person (own-auth phase 2): which organisation, and when. */
export interface MyMicrosoftLink {
  /** The Microsoft organisation (tenant) the account belongs to. */
  tenant_id: string;
  created_at: string;
  last_used_at: string | null;
}

export interface MySecurity {
  /** A technician or a client's contact: whose rules apply (own-auth phase 2). */
  kind: "staff" | "contact";
  /** False for someone who signed up with a passkey or Microsoft and never set one. */
  has_password: boolean;
  microsoft: MyMicrosoftLink | null;
  has_totp: boolean;
  passkeys: MyPasskey[];
  recovery_codes_left: number;
  trusted_devices: MyTrustedDevice[];
  sessions: MySession[];
}

/** `PUT /me/password`. `new` is the server's key; TypeScript allows it as a property name. */
export interface PasswordChange {
  current: string;
  new: string;
}

// `POST /me/recovery-codes` answers the very shape `POST /auth/enroll/finish`
// does, `RecoveryCodes` of `modules/auth/types.ts` (own-auth Task 11), and
// the new-password bounds before the round trip are Task 11's
// `checkNewPassword` (`modules/auth/errors.ts`): neither is restated here.

/** How many codes a set holds (design §2.2). */
export const RECOVERY_CODE_COUNT = 10;
