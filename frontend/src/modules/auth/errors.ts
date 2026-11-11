/**
 * What a refused sign-in step says (own-auth design §2.1, §2.3), from the
 * refusals of own-auth Tasks 6–8:
 *
 *   401 `invalid_credentials`   the first step: wrong address or password
 *   401 `factor_failed`         a second factor at sign-in not accepted
 *   400 `factor_failed`         a first factor (enrolment) not accepted
 *   401 `auth_required`         the pre-factor session is gone (the door)
 *   429 `factor_wait`           wait; `wait_s` in the body (and `Retry-After`)
 *   410 `invalid_link`          an emailed or server-made link used, expired or unknown
 *   `password_rejected`         a new password refused; `detail` says why
 *   503 `sign_in_unavailable`   the server cannot sign anyone in right now
 *
 * GENERIC ON PURPOSE. The server answers a wrong password the same whether or
 * not the account exists, and a wrong code the same whatever was wrong with
 * it; the page must not be more precise than the server is. So a 401 or a 400
 * from a step is the step's one "not accepted" sentence, and only two things
 * are told apart: a pre-factor session that is gone, which can only be started
 * again, and a wait, which a person can do something about.
 *
 * A REJECTED PASSWORD STATES ITS REASON. NIST SP 800-63B-4 §3.1.1.2: the
 * verifier "SHALL provide the reason for rejection" ([S1]). The server words
 * it (`detail`, translated by `tr()`); this page shows it as sent. The length
 * is also checked here first, in code points as the server counts it, so the
 * commonest refusal costs no round trip.
 *
 * Bodies are read from `ApiError.message`, which `ApiClient` fills with the
 * server's text on these routes (`request()`'s `anonymous`), through
 * `lib/api.ts::errorBody`.
 */
import { describeSubmitError } from "../../components/form";
import { translate, type MessageKey } from "../../i18n/core";
import { ApiError, apiErrorCode, errorBody } from "../../lib/api";

/** How long the server asked to wait, in whole seconds, when it said. */
export function waitSeconds(error: unknown): number | null {
  const wait = errorBody(error).wait_s;
  return typeof wait === "number" && Number.isFinite(wait) && wait > 0 ? Math.ceil(wait) : null;
}

/** The server's sentence for a refused new password (`password_rejected`), when it sent one. */
export function passwordRefusal(error: unknown): string | null {
  if (apiErrorCode(error) !== "password_rejected") return null;
  const detail = errorBody(error).detail;
  return typeof detail === "string" && detail.trim() !== "" ? detail : null;
}

/**
 * The sentence for a refused step. `rejected` is the step's own "not
 * accepted", for a 401 or a 400; without one those fall to the server's own
 * words. `canTimeOut` is false where there is no pre-factor session to lose
 * (the first step, the emailed links).
 */
export function describeAuthError(
  error: unknown,
  { rejected, canTimeOut = true }: { rejected?: MessageKey; canTimeOut?: boolean } = {},
): string {
  if (error instanceof ApiError) {
    if (error.status === 429) {
      const seconds = waitSeconds(error);
      return seconds === null ? translate("auth.common.tooMany") : translate("auth.common.wait", { count: seconds });
    }
    const refusal = passwordRefusal(error);
    if (refusal) return refusal;
    if (error.status === 401 && canTimeOut && apiErrorCode(error) === "auth_required") {
      return translate("auth.common.timedOut");
    }
    if (rejected && (error.status === 401 || error.status === 400)) return translate(rejected);
  }
  return describeSubmitError(error);
}

/** A link the server no longer honours: used, expired or unknown (410 `invalid_link`). */
export function isDeadLink(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 410 || apiErrorCode(error) === "invalid_link");
}

/**
 * The new-password checks the console can make before asking (length in code
 * points, design §2.1): at least `minimum` -- 12 for a technician, 15 for a
 * client's contact (design §3) -- and at most 128.
 */
export function checkNewPassword(password: string, again: string, minimum: number = 12): string | null {
  const length = Array.from(password).length;
  if (length < minimum) return translate("auth.password.too_short", { min: minimum });
  if (length > 128) return translate("auth.password.too_long");
  if (password !== again) return translate("auth.password.mismatch");
  return null;
}
