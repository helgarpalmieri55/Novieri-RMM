/**
 * THE CONSOLE'S SESSION, AS THE CONSOLE SEES IT (own-auth design §1.1, §1.2,
 * §6.4). It replaces `lib/auth.ts` and `oidc-client-ts`.
 *
 * WHAT THE CONSOLE NO LONGER HOLDS: a token, anywhere. The session is the
 * `__Host-itms_session` cookie -- `HttpOnly`, so no script on this page can
 * read it -- and the browser sends it with every same-origin request and with
 * the live socket's handshake. `lib/auth.ts` kept the whole OIDC user, access
 * token included, in `sessionStorage` (its line 74) under a header that said
 * the opposite; OWASP: "Do not store authentication tokens ... in
 * localStorage or sessionStorage" (design §1.2, [S6]). That store went with
 * the module.
 *
 * SO THE ONE QUESTION IS `GET /me`. An answer is a signed-in person. A 401 is
 * no session (`ApiClient` reports it to `./sessionEnd`). Anything else is a
 * server that could not say, and the guard says so rather than guess.
 *
 * EVERY CHANGE OF SIDE IS A WHOLE-DOCUMENT LOAD. Being sent to sign in
 * (`goToSignIn`), entering the console after signing in (`enterConsole`) and
 * signing out (`signOut`) all navigate the document. The query cache, the
 * client chosen in the top bar, the live socket and the session-ended flag of
 * whoever was here never reach the next person's console on a shared
 * computer, and nothing has to remember to clear them. A session that ends
 * WITHOUT a navigation -- inactivity, the maximum, an administrator, another
 * person in the cookie -- is hidden and emptied by the guard at once
 * (`App.tsx`'s `RequireAuth`, operator decision 3, 2026-11-05).
 */
import { useQuery, type QueryClient } from "@tanstack/react-query";
import { useLayoutEffect, useSyncExternalStore } from "react";

import { ApiError, type ApiClient, type Me } from "./api";
import { useApi } from "./ApiContext";
import {
  hadSession,
  isSessionEnded,
  sessionEndReason,
  sessionPerson,
  sessionSignedOut,
  subscribeSessionEnd,
} from "./sessionEnd";

/**
 * - `signed_in`: `/me` answered, and nothing has ended it since.
 * - `signed_out`: no session, and this tab never had one -- a visitor, sent
 *   to sign in.
 * - `ended`: the session this tab had is over (inactivity, the maximum, an
 *   administrator, another person in the cookie). The guard hides the page
 *   behind its notice and never navigates by itself.
 * - `signing_out`: the person signed out here, and `signOut` is loading the
 *   sign-in page.
 * - `loading`: not known yet.
 */
export type SessionStatus = "loading" | "signed_out" | "signed_in" | "ended" | "signing_out";

export interface Session {
  status: SessionStatus;
  /**
   * The person, while `signed_in` only. NOT KEPT AFTER THE SESSION ENDS: the
   * cache it came from is emptied then (operator decision 3).
   */
  me?: Me;
  /** `/me` failed for a reason that is not "no session": the server could not say, or refused. */
  failure?: unknown;
}

/** The console's sign-in page. Task 11 of the own-auth plan mounts it. */
export const SIGN_IN_PATH = "/sign-in";

/**
 * Where signing in lands when nothing better is known: the root, whose route
 * (`Landing` in `App.tsx`) picks the account's home.
 */
export const DEFAULT_AFTER_SIGN_IN = "/";

/**
 * The pages a person reaches WITHOUT a session, mounted outside `RequireAuth`
 * (own-auth Task 11). Prefixes: `/sign-in/factor` is under `/sign-in`. The
 * emailed and server-made links (`/invite`, `/reset-password`, `/recover`)
 * carry their token in the fragment, never in the path. None begins with a
 * prefix the edge sends elsewhere (`/api`, `/enroll`, `/i/`, `/mcp`,
 * `/.well-known`, `/realms`, `/resources`, `/js`; plan decision D19).
 */
export const PUBLIC_PATHS: readonly string[] = ["/sign-in", "/forgot-password", "/reset-password", "/invite", "/recover"];

export function isPublicPath(pathname: string): boolean {
  return PUBLIC_PATHS.some((prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`));
}

/**
 * The page to land on after signing in, from `?next=` -- A VALUE ANYONE CAN
 * WRITE INTO A LINK THEY SEND.
 *
 * Handing it to the browser unchecked is an OPEN REDIRECT through the sign-in
 * page: a protocol-relative path (`//evil.example`) leaves the origin, and a
 * phishing page then wears the URL the person just typed their password into.
 * So the rule is a whitelist, the one `lib/auth.ts::safeReturnTo` applied to
 * the OIDC `state`:
 *
 *   accept ONE leading "/", then anything that is not another "/" or "\".
 *
 * Which refuses, by construction: absolute URLs, `javascript:` and every
 * other scheme, protocol-relative paths, the backslash spellings browsers
 * normalise into them (`/\host`), and relative paths. Control characters and
 * whitespace are refused separately because browsers STRIP tab, CR and LF
 * before resolving: `"/\t/evil.example"` would become `//evil.example`.
 *
 * A PAGE REACHED WITHOUT A SESSION IS REFUSED TOO: coming back to the sign-in
 * page after signing in would ask for the password again, and a loop between
 * two public pages has no way out.
 */
export function safeNext(raw: unknown): string {
  if (typeof raw !== "string") return DEFAULT_AFTER_SIGN_IN;
  if (/[\u0000-\u001f\u007f\s]/.test(raw)) return DEFAULT_AFTER_SIGN_IN;
  if (!/^\/(?![/\\])/.test(raw)) return DEFAULT_AFTER_SIGN_IN;
  // RESOLVE IT THE WAY THE BROWSER WILL, then judge the result: `/x/../sign-in`
  // and `/%2e%2e/sign-in` are the sign-in page once normalised, and a
  // same-origin path under an edge prefix is not a console page at all
  // (`/i/<code>/windows` would download an agent installer for a person who
  // just signed in).
  let url: URL;
  try {
    url = new URL(raw, window.location.origin);
  } catch {
    return DEFAULT_AFTER_SIGN_IN;
  }
  if (url.origin !== window.location.origin) return DEFAULT_AFTER_SIGN_IN;
  // `URL` leaves percent-escapes of letters alone (`/%69/` stays `/%69/`),
  // and an edge that decodes them would route `/%69/<code>/windows` to the
  // installer. So the rules are checked on the decoded path too, and a path
  // that does not decode is refused.
  let decoded: string;
  try {
    decoded = decodeURIComponent(url.pathname);
  } catch {
    return DEFAULT_AFTER_SIGN_IN;
  }
  for (const path of [url.pathname, decoded]) {
    if (path.startsWith("//") || path.includes("\\")) return DEFAULT_AFTER_SIGN_IN;
    if (isPublicPath(path) || isEdgePath(path)) return DEFAULT_AFTER_SIGN_IN;
  }
  return raw;
}

/** Paths the edge serves itself, never the console's router. */
const EDGE_PREFIXES: readonly string[] = ["/i", "/api", "/enroll", "/mcp", "/.well-known", "/realms", "/resources", "/js"];

function isEdgePath(pathname: string): boolean {
  return EDGE_PREFIXES.some((prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`));
}

/** `/sign-in`, with `?next=` when there is somewhere better than the root to come back to. */
export function signInPath(next?: string): string {
  const back = safeNext(next);
  return back === DEFAULT_AFTER_SIGN_IN ? SIGN_IN_PATH : `${SIGN_IN_PATH}?next=${encodeURIComponent(back)}`;
}

/**
 * The two whole-document navigations, on an object so a test can spy on them:
 * jsdom implements neither, and `window.location` cannot be replaced. Every
 * caller goes through `navigation.assign` / `navigation.replace`, never
 * `window.location` directly.
 */
export const navigation = {
  assign(url: string): void {
    window.location.assign(url);
  },
  replace(url: string): void {
    window.location.replace(url);
  },
};

/**
 * To the sign-in page, remembering where the person was.
 *
 * NEVER FROM A PAGE REACHED WITHOUT A SESSION (plan decision D20). Those pages
 * are where a person signs in; sending them to sign in from there would reload
 * the page they are on, and a request of theirs that answered 401 would do it
 * again, for ever.
 */
export function goToSignIn(next?: string): void {
  if (isPublicPath(window.location.pathname)) return;
  navigation.assign(signInPath(next));
}

/**
 * Into the console once signing in is done. `replace`, so Back does not
 * return to a sign-in step whose session has moved on.
 */
export function enterConsole(next: unknown): void {
  navigation.replace(safeNext(next));
}

/**
 * Sign out: the server ends the session, then the whole document goes to the
 * sign-in page.
 *
 * A 401 IS SUCCESS HERE: the session had already ended, which is what was
 * asked for. ANY OTHER FAILURE REJECTS AND THE PAGE STAYS. A person on a
 * shared computer who clicked "Sign out" and was shown the sign-in page while
 * the server never heard would walk away from an open session; the shell says
 * it is still open instead (`shell.app.signOutFailed`).
 *
 * ONCE THE SERVER HAS ENDED IT, NOTHING OF THE SESSION WAITS FOR THE LOAD:
 * the guard empties the query cache and hides the page at once
 * (`sessionSignedOut`, operator decision 3) -- the browser keeps the old page
 * on screen until the sign-in page has loaded.
 */
export async function signOut(api: Pick<ApiClient, "logout">): Promise<void> {
  try {
    await api.logout();
  } catch (error: unknown) {
    if (!(error instanceof ApiError && error.status === 401)) throw error;
  }
  sessionSignedOut();
  navigation.assign(SIGN_IN_PATH);
}

/**
 * Whether a person is signed in right now, as far as this tab knows: `/me`
 * answered and no 401 has been seen since. `LanguageBridge` saves a language
 * to the account only then (plan decision D20): on a sign-in page there is no
 * account to save it to, and the request would only answer 401.
 */
export function hasSession(queryClient: QueryClient): boolean {
  return !isSessionEnded() && queryClient.getQueryData(["me"]) !== undefined;
}

/**
 * The session, from `GET /me` and what this tab has learnt since
 * (`./sessionEnd.ts`). The states are `SessionStatus`'s; `loading` carries
 * `failure` when `/me` failed for a reason that is not "no session".
 *
 * THE PERSON `/me` NAMES IS REMEMBERED, the first time it answers, and an
 * answer naming anybody else ends the session (`sessionPerson`, final review
 * D1, Minor 3): someone signed in on this browser in another tab. The socket's
 * re-check applies the same rule (`./ws.ts`). In a LAYOUT effect, so the new
 * person's `/me` is never painted: the end runs before the browser draws, and
 * the page it reads the typed text from is still in the document.
 *
 * NOT ASKED AGAIN ONCE THE SESSION HAS ENDED: the answer is a 401, or the
 * next person's `/me`, and neither is this tab's to hold.
 *
 * The query is the shell's own `["me"]`: one request answers the guard, the
 * shell, the client selector and `Landing`.
 */
export function useSession(): Session {
  const api = useApi();
  const ended = useSyncExternalStore(subscribeSessionEnd, sessionEndReason, sessionEndReason);
  const me = useQuery({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000, enabled: ended === null });
  const userId = me.data?.user_id;
  useLayoutEffect(() => {
    if (userId !== undefined) sessionPerson(userId);
  }, [userId]);
  if (ended === "signed_out") return { status: "signing_out" };
  if (ended === "ended") return { status: hadSession() ? "ended" : "signed_out" };
  if (me.data !== undefined) return { status: "signed_in", me: me.data };
  if (me.error instanceof ApiError && me.error.status === 401) return { status: "signed_out" };
  if (me.error) return { status: "loading", failure: me.error };
  return { status: "loading" };
}
