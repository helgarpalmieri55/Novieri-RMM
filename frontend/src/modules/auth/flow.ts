/**
 * What the sign-in steps hand each other (own-auth Task 11).
 *
 * `?next=` travels from step to step in the URL -- it is a page, not a
 * secret -- and is filtered by `lib/session.ts::safeNext` wherever it is used.
 *
 * THE TOKEN OF AN EMAILED OR SERVER-MADE LINK TRAVELS IN THE FRAGMENT
 * (`/invite#<token>`, `/reset-password#<token>`, `/recover#<token>`), which a
 * browser never sends: it reaches no access log and no `Referer`. The page
 * reads it once, takes it off the address (`useLinkToken`) so it does not sit
 * in the tab's history either, and sends it in a request body.
 *
 * THE RECOVERY CODES DO NOT TRAVEL IN THE URL OR IN THE ROUTER'S STATE. Router
 * state is `history.state`, which a browser keeps with the session's history
 * and may write to disk to restore tabs; ten single-use codes that each sign
 * the person in do not belong there, any more than a token in
 * `sessionStorage` did (design §1.2, [S6]). They are handed from the step
 * that made them to the page that shows them in this module's memory, which a
 * reload or a sign-in empties.
 */
import { useEffect, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";

import { DEFAULT_AFTER_SIGN_IN, enterConsole, safeNext } from "../../lib/session";
import type { LoginResult } from "./types";

/** The raw `?next=` of the current step, filtered where it is used. */
export function useNext(): string | null {
  const [params] = useSearchParams();
  return params.get("next");
}

/** What the server's answer to `POST /auth/login` decides: the next page, and nothing else does. */
export function afterLogin(
  result: LoginResult,
  next: string | null,
  navigate: (to: string, options?: { state?: unknown }) => void,
): void {
  if (result.state === "signed_in") {
    enterConsole(next);
    return;
  }
  if (result.state === "enroll_required") {
    navigate(carryNext("/sign-in/enroll", next));
    return;
  }
  navigate(carryNext("/sign-in/factor", next), {
    state: { methods: result.methods, rememberDays: result.remember_device_days ?? null },
  });
}

/** `path`, carrying `next` on to the following step when there is somewhere to go back to. */
export function carryNext(path: string, next: string | null): string {
  const back = safeNext(next);
  return back === DEFAULT_AFTER_SIGN_IN ? path : `${path}?next=${encodeURIComponent(back)}`;
}

/** The token in a link's fragment: `#<token>`, or `#token=<token>`. Empty when there is none. */
export function tokenFromHash(hash: string): string {
  const raw = hash.startsWith("#") ? hash.slice(1) : hash;
  if (raw.includes("=")) return new URLSearchParams(raw).get("token") ?? "";
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

/**
 * The link's token, read once from the fragment, which then leaves the
 * address (`replace`, so Back does not bring it back). A reload therefore
 * finds no token, and the server answers that link as it would any unknown
 * one; the email still holds it.
 */
export function useLinkToken(): string {
  const location = useLocation();
  const navigate = useNavigate();
  const [token] = useState(() => tokenFromHash(location.hash));
  useEffect(() => {
    if (location.hash) navigate({ pathname: location.pathname, search: location.search }, { replace: true });
  }, [location.hash, location.pathname, location.search, navigate]);
  return token;
}

/** A six-digit code as typed: spaces and dashes a phone app shows between the halves dropped. */
export function digitsOf(code: string): string {
  return code.replace(/[\s-]/g, "");
}

/**
 * A recovery code as typed, in the form it was shown (`XXXXX-XXXXX`, own-auth
 * Task 4): upper case, with the dash where it belongs whether or not the
 * person typed it. Anything that is not ten characters is sent as typed and
 * the server refuses it like any other wrong code.
 */
export function recoveryCodeOf(code: string): string {
  const compact = code.toUpperCase().replace(/[\s-]/g, "");
  return compact.length === 10 ? `${compact.slice(0, 5)}-${compact.slice(5)}` : code.trim();
}

/** The setup key inside an `otpauth://` URI, in groups of four for typing, or `null`. */
export function setupKeyOf(otpauthUri: string): string | null {
  try {
    const secret = new URL(otpauthUri).searchParams.get("secret");
    return secret ? secret.replace(/(.{4})(?=.)/g, "$1 ") : null;
  } catch {
    return null;
  }
}

let handed: readonly string[] = [];

/** From the step that made the recovery codes to the page that shows them. */
export function handOverCodes(codes: readonly string[]): void {
  handed = [...codes];
}

/** The codes handed over, if any. Reading does not clear them: React may render twice. */
export function heldCodes(): readonly string[] {
  return handed;
}

/** Drop the codes, once the page that showed them is gone. */
export function forgetCodes(): void {
  handed = [];
}
