/**
 * The wire shapes of `/api/v1/auth/*` (own-auth plan, Tasks 7 and 8), as the
 * console's sign-in pages read them. The WebAuthn options and responses are
 * the W3C `*JSON` dictionaries, typed by `@simplewebauthn/browser` itself so
 * what the server sends is exactly what `startRegistration` /
 * `startAuthentication` take.
 */
import type {
  AuthenticationResponseJSON,
  PublicKeyCredentialCreationOptionsJSON,
  PublicKeyCredentialRequestOptionsJSON,
  RegistrationResponseJSON,
} from "@simplewebauthn/browser";

export type {
  AuthenticationResponseJSON,
  PublicKeyCredentialCreationOptionsJSON,
  PublicKeyCredentialRequestOptionsJSON,
  RegistrationResponseJSON,
};

/** A second factor the account can finish signing in with. The server lists them passkey first. */
export type FactorMethod = "passkey" | "totp" | "recovery";

/**
 * `POST /auth/login`. `factor_required` and `enroll_required` set a session
 * cookie that has no factor yet; `signed_in` comes only from a remembered
 * computer (design §2.6).
 *
 * `remember_device_days` is the organisation's "Remember this computer"
 * length, or `null` when it is switched off (Settings → Sessions). Absent, the
 * console offers no "remember" box -- the safe reading of a server that did
 * not say.
 */
export type LoginResult =
  | { state: "factor_required"; methods: FactorMethod[]; remember_device_days?: number | null }
  | { state: "enroll_required" }
  | { state: "signed_in" };

/** A second factor accepted: the session is complete and its identifier rotated. */
export interface SignedIn {
  state: "signed_in";
}

/** `POST /auth/enroll/totp/start`: the new secret, ONCE, as an `otpauth://` URI. */
export interface TotpEnrollment {
  otpauth_uri: string;
}

/**
 * Who calls an enrol route (`/auth/enroll/totp/*`, `/auth/enroll/passkey*`).
 * The first-factor page (`/sign-in/enroll`) calls with a session still
 * waiting for its factor, where a 401 is the step's own answer (`request()`'s
 * `anonymous`): leave it out. My security (own-auth Task 12) calls with the
 * full console session, `{ signedIn: true }`: there a 401 IS that session
 * ending, and it must reach `onUnauthorized` like any other console request.
 * Neither carries `X-Client-Id`.
 */
export interface EnrollFrom {
  signedIn?: boolean;
}

/** `POST /auth/enroll/finish` and the regeneration in Sign-in and security: ten codes, shown once. */
export interface RecoveryCodes {
  recovery_codes: string[];
}

/** `POST /auth/invite/details {token}`: who the invitation is for. */
export interface InviteDetails {
  email: string;
  display_name: string;
}

/** `POST /auth/recover/details {token}`: whose account the server's recovery link is for. */
export interface RecoveryLink {
  email: string;
  /**
   * True while the account's password was last set with an emailed link: the
   * recovery then needs a new password (operator decision 2026-11-05).
   */
  password_required: boolean;
}

/**
 * `POST /auth/recover/accept`: the link's token, and the new password while
 * the server asks for one (`RecoveryLink.password_required`, or a 409
 * `recovery_needs_password`). Without that, the server ignores a password.
 */
export interface RecoveryAccept {
  token: string;
  password?: string;
}
