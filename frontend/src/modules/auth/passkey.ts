/**
 * The browser half of a passkey (own-auth design §2.2), through
 * `@simplewebauthn/browser` 14.0.0.
 *
 * THE LIBRARY'S CONTRACT, READ FROM ITS PUBLISHED PACKAGE
 * (https://registry.npmjs.org/@simplewebauthn/browser/-/browser-14.0.0.tgz,
 * `esm/methods/startRegistration.d.ts`, `startAuthentication.d.ts`):
 *
 *   startRegistration({ optionsJSON: PublicKeyCredentialCreationOptionsJSON })
 *     -> Promise<RegistrationResponseJSON>
 *   startAuthentication({ optionsJSON: PublicKeyCredentialRequestOptionsJSON })
 *     -> Promise<AuthenticationResponseJSON>
 *
 * Both are the W3C `*JSON` dictionaries, base64url for every byte string,
 * which is what py_webauthn's `options_to_json` sends and
 * `verify_registration_response` / `verify_authentication_response` accept
 * (own-auth Task 5). The options come from the server whole -- the challenge,
 * `userVerification: "required"`, the credentials to exclude -- and this file
 * adds nothing to them: what the authenticator is asked for is the server's
 * decision, and the server checks the answer (design §2.2; the console
 * protects nothing, invariant 6).
 *
 * A refusal arrives as the library's `WebAuthnError`, whose `name` is the
 * browser's (`NotAllowedError` when the person closed the prompt or it timed
 * out) and whose `code` names the case (`webAuthnError.d.ts`).
 */
import {
  WebAuthnError,
  browserSupportsWebAuthn,
  startAuthentication,
  startRegistration,
} from "@simplewebauthn/browser";

import { translate, type MessageKey } from "../../i18n/core";
import type {
  AuthenticationResponseJSON,
  PublicKeyCredentialCreationOptionsJSON,
  PublicKeyCredentialRequestOptionsJSON,
  RegistrationResponseJSON,
} from "./types";

/** Whether this browser can make or use a passkey at all; the passkey option is not offered when not. */
export function canUsePasskeys(): boolean {
  return browserSupportsWebAuthn();
}

/**
 * What to say when the person HAS a passkey and this browser cannot use it,
 * so the passkey option is not offered (final review D1, Minor 6): why it is
 * missing, and the method they still have. `null` when they have no other --
 * the page says that in its own words (signing in and confirming a change
 * differ in what is left to do).
 */
export function passkeyHiddenAdvice({ totp, recovery }: { totp: boolean; recovery: boolean }): MessageKey | null {
  if (totp && recovery) return "auth.passkey.hiddenUseAppOrCode";
  if (totp) return "auth.passkey.hiddenUseApp";
  if (recovery) return "auth.passkey.hiddenUseCode";
  return null;
}

/** Make a passkey from the server's registration options. */
export function createPasskey(optionsJSON: PublicKeyCredentialCreationOptionsJSON): Promise<RegistrationResponseJSON> {
  return startRegistration({ optionsJSON });
}

/** Sign the server's challenge with a passkey. */
export function signWithPasskey(optionsJSON: PublicKeyCredentialRequestOptionsJSON): Promise<AuthenticationResponseJSON> {
  return startAuthentication({ optionsJSON });
}

/**
 * The sentence for a passkey the browser refused, or `null` when the refusal
 * was not the browser's. EVERY `WebAuthnError` gets one of OUR sentences, in
 * the active language: the library's own message is English and is never shown.
 */
export function describePasskeyError(error: unknown): string | null {
  if (error instanceof WebAuthnError) {
    switch (error.code) {
      case "ERROR_AUTHENTICATOR_PREVIOUSLY_REGISTERED":
        return translate("auth.passkey.alreadyRegistered");
      case "ERROR_AUTHENTICATOR_MISSING_USER_VERIFICATION_SUPPORT":
        return translate("auth.passkey.noUserVerification");
      case "ERROR_CEREMONY_ABORTED":
        return translate("auth.passkey.aborted");
      default:
        // The person closed the prompt or it timed out: the browser's
        // `NotAllowedError`, carried as the error's name.
        if (error.name === "NotAllowedError") return translate("auth.passkey.cancelled");
        return translate("auth.passkey.generic");
    }
  }
  if (error instanceof Error && error.name === "NotAllowedError") return translate("auth.passkey.cancelled");
  if (error instanceof Error && /not supported in this browser/i.test(error.message)) {
    return translate("auth.passkey.unsupported");
  }
  return null;
}
