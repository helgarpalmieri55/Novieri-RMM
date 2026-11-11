/**
 * SIGN IN WITH MICROSOFT, THE CONSOLE'S HALF (own-auth phase 2, Task 12;
 * design §4; plan D30).
 *
 * THE SERVER HOLDS THE TRANSACTION. `POST /auth/microsoft/start` stores the
 * state and the nonce, sets the `__Host-itms_entra` cookie (the PKCE
 * verifier) and answers Microsoft's address. Microsoft sends the browser back
 * to `/sign-in/microsoft#code=...&state=...` (`response_mode=fragment`): a
 * fragment reaches no server log and no `Referer`, and the page posts it
 * same-origin, so the Strict cookie travels with it.
 *
 * WHAT THE TAB KEEPS (decision 3) is where to come back to: an intent and a
 * path, never a secret, in `sessionStorage`, read once and filtered by
 * `safeNext`. Storage that throws (a private window, blocked site data) only
 * means coming back to the root.
 *
 * THE CONSOLE LEAVES ONLY FOR MICROSOFT (decision 4): `authorize_url` is
 * checked against the one host the server speaks to (`entra.MICROSOFT_HOST`,
 * [M3]) before the whole document goes there.
 */
import { translate } from "../../i18n/core";
import { ApiError, apiErrorCode } from "../../lib/api";
import { DEFAULT_AFTER_SIGN_IN, safeNext } from "../../lib/session";

export const MICROSOFT_ORIGIN = "https://login.microsoftonline.com";

const RETURN_KEY = "itms.microsoft.return";

export type MicrosoftIntent = "sign_in" | "link" | "invite";

export interface MicrosoftReturn {
  intent: MicrosoftIntent;
  /** A page of the console, already through `safeNext`. */
  back: string;
}

/** Before leaving for Microsoft: where to come back to. */
export function rememberMicrosoftReturn(value: MicrosoftReturn): void {
  try {
    window.sessionStorage.setItem(RETURN_KEY, JSON.stringify(value));
  } catch {
    // No storage: the return page lands on the root.
  }
}

/**
 * On the way back: where to go. A missing or unreadable value is the root.
 * PURE, so a state initializer may call it (React calls those twice in
 * strict mode); the page forgets it once it has sent the code.
 */
export function readMicrosoftReturn(): MicrosoftReturn {
  let raw: string | null = null;
  try {
    raw = window.sessionStorage.getItem(RETURN_KEY);
  } catch {
    raw = null;
  }
  try {
    const parsed = raw ? (JSON.parse(raw) as Partial<MicrosoftReturn>) : {};
    const intent: MicrosoftIntent =
      parsed.intent === "link" || parsed.intent === "invite" ? parsed.intent : "sign_in";
    return { intent, back: safeNext(parsed.back) };
  } catch {
    return { intent: "sign_in", back: DEFAULT_AFTER_SIGN_IN };
  }
}

/** Once the return has been used: nothing of it stays in the tab. */
export function forgetMicrosoftReturn(): void {
  try {
    window.sessionStorage.removeItem(RETURN_KEY);
  } catch {
    // Nothing was kept.
  }
}

/** Read it and forget it. */
export function takeMicrosoftReturn(): MicrosoftReturn {
  const value = readMicrosoftReturn();
  forgetMicrosoftReturn();
  return value;
}

/**
 * What Microsoft put in the fragment: a code and a state, its refusal (with
 * the state, when it came back, so the page can tell the server to drop the
 * transaction: decision 2), or nothing usable.
 */
export function microsoftReturnOf(
  hash: string,
): { code: string; state: string } | { error: string; state: string | null } | null {
  const params = new URLSearchParams(hash.startsWith("#") ? hash.slice(1) : hash);
  const error = params.get("error");
  if (error) return { error, state: params.get("state") || null };
  const code = params.get("code");
  const state = params.get("state");
  return code && state ? { code, state } : null;
}

/** Microsoft's address, only when it is Microsoft's (decision 4). */
export function checkedAuthorizeUrl(raw: string): string | null {
  try {
    return new URL(raw).origin === MICROSOFT_ORIGIN ? raw : null;
  } catch {
    return null;
  }
}

/**
 * The sentence for a refused step of the Microsoft sign-in, or `null` when it
 * is not one of its codes. `microsoft_unavailable` is one sentence whatever
 * lies behind it: switched off, not set up, or a configured organization that
 * does not exist (the server answers them alike, Task 9).
 */
export function describeMicrosoftError(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  switch (apiErrorCode(error)) {
    case "microsoft_unavailable":
      return translate("auth.microsoft.unavailable");
    case "microsoft_failed":
      return translate("auth.microsoft.failed");
    case "microsoft_not_linked":
      return translate("auth.microsoft.notLinked");
    case "microsoft_tenant_refused":
      return translate("auth.microsoft.tenantRefused");
    case "microsoft_already_linked":
      return translate("auth.microsoft.alreadyLinked");
    case "last_method":
      return translate("auth.microsoft.lastMethod");
    case "password_required":
      // A technician with no password of their own, at a link (Task 9,
      // decision 5, C3). The invitation page asks for one before leaving.
      return translate("auth.microsoft.passwordFirst");
    case "step_up_required":
      // At the return of a link: the confirmation ran out on the way (Task 9,
      // decision 3). Before leaving, `useStepUp().run` handles it.
      return translate("auth.microsoft.stepUpAgain");
    default:
      return null;
  }
}
