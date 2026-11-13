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

/** `POST /auth/invite/details {token}`: who the invitation is for, and how it may be accepted. */
export interface InviteDetails {
  email: string;
  display_name: string;
  /** A technician or a client's contact (own-auth phase 2). */
  kind: "staff" | "contact";
  /** The shortest password this person may choose: 12 or 15. */
  password_minimum: number;
  /** The invitation may be accepted with a passkey (contacts). */
  passkey: boolean;
  /** The invitation may be accepted with Microsoft (Task 12). */
  microsoft: boolean;
}

/** `POST /auth/invite/accept`: a technician still needs a factor; a contact is in. */
export type InviteAccepted = { state: "enroll_required" } | SignedIn;

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
 * The AI app a consent screen names (`backend/app/core/auth/oauth/consent.py`,
 * plan 2026-11-06 Appendix A.2). `name` is the app's own claim (RFC 7591 §5);
 * `client_host` is the host of a client metadata document's URL (`kind:
 * "metadata_document"`), null for an app that registered; `redirect_host` is
 * where the answer goes; `loopback_only` when every address it registered is
 * this computer. Hosts are ASCII (plan Task 1, decision 3).
 */
export interface ConsentClient {
  name: string | null;
  client_id: string;
  kind: "registered" | "metadata_document";
  client_host: string | null;
  redirect_host: string;
  loopback_only: boolean;
}

/**
 * What the server answers about an authorization request, other than "ask
 * the person". `refused` is shown and never sent anywhere (RFC 6749 §4.1.2.1:
 * a bad client or redirect; `temporarily_unavailable` says to try again);
 * `back` is shown with the host it goes to, and goes there only on the
 * person's click (RFC 9700 §4.11.2): an error the app should hear, the
 * person's decline, or a person who may not connect apps; `redirect` is where
 * to go now, with the code, after an approval -- and only then.
 */
export type ConsentAnswer =
  | { outcome: "refused"; error: string; detail: string }
  | {
      outcome: "back";
      reason: "error" | "declined" | "denied";
      detail: string;
      redirect_host: string;
      redirect_to: string;
    }
  | { outcome: "redirect"; redirect_to: string };

/** `POST /oauth/authorize/details`: `consent` asks the person; the rest is `ConsentAnswer`, never `redirect`. */
export type ConsentDetails =
  | { outcome: "consent"; client: ConsentClient; scope: string; account: string }
  | Exclude<ConsentAnswer, { outcome: "redirect" }>;

/**
 * `POST /auth/recover/accept`: the link's token, and a new password -- needed
 * while the server asks for one (`RecoveryLink.password_required`, or a 409
 * `recovery_needs_password`), optional otherwise, and applied whenever it is
 * sent (operator decision 2026-11-05, «Ofrecerla siempre, aplicar»).
 */
export interface RecoveryAccept {
  token: string;
  password?: string;
}
