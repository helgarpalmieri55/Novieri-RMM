// OIDC (authorization code + PKCE) against Keycloak via oidc-client-ts.
// Tokens stay in memory (sessionStorage-backed store only for the OIDC state).

import { User, UserManager, WebStorageStateStore } from "oidc-client-ts";
import { getLanguage } from "../i18n/core";

/**
 * Where Keycloak lives, derived from the page rather than baked into the build.
 *
 * THE PREVIOUS DEFAULT WAS `http://localhost:8080/realms/it-suite` AND IT
 * SHIPPED IN EVERY IMAGE. `VITE_OIDC_AUTHORITY` was referenced here and set
 * nowhere — not in `.env.example`, not in `frontend.Dockerfile`, not as a
 * compose build argument — so the fallback was always what ran. Every browser
 * that opened the console asked *its own machine* on port 8080 for the
 * discovery document. The login button did nothing, the network tab showed
 * 0-byte `openid-configuration` fetches, and no server logged anything, because
 * no request ever left the laptop.
 *
 * A build-time variable is the wrong shape for this value: the image is built
 * once and deployed under whatever hostname the operator owns, so anything
 * compiled in is wrong for every deployment except the one it was built for.
 * Keycloak is served on the SAME ORIGIN under `/realms` (the `kc` router in
 * `deploy/docker-compose.yml`), so the origin the console was loaded from is
 * the answer, and it cannot drift from the deployment's real hostname.
 *
 * The override remains for local development, where the Vite dev server and
 * Keycloak really are on different ports; `frontend/.env.development` sets it.
 */
export function resolveAuthority(configured: string | undefined, origin: string): string {
  // An empty string, not just undefined: an unsubstituted build argument
  // arrives as "" and must not be treated as a configured value.
  return configured ? configured : `${origin}/realms/it-suite`;
}

const authority = resolveAuthority(
  import.meta.env.VITE_OIDC_AUTHORITY,
  window.location.origin,
);
/**
 * The authority actually in use, exported so a FAILURE can name it.
 *
 * The console no longer has a launcher page: an unauthenticated visitor is
 * redirected to Keycloak immediately (`docs/modules/console-auth/spec.md`
 * §3.4). That removes the one screen that used to render regardless of whether
 * Keycloak was reachable — so when the redirect itself fails, this value is the
 * single most useful fact on the page, and the one nothing else reveals.
 */
export const oidcAuthority = authority;

const clientId = import.meta.env.VITE_OIDC_CLIENT_ID ?? "it-suite-web";

export const userManager = new UserManager({
  authority,
  client_id: clientId,
  redirect_uri: `${window.location.origin}/auth/callback`,
  // THE TRAILING SLASH IS LOAD-BEARING, and its absence was a BLOCKER.
  //
  // Keycloak validates this against the client's registered post-logout URIs
  // and refuses anything else with a full-screen "Invalid redirect uri" page
  // that carries no link, no button and no way back into the product -- the
  // terminal state for an operator mid-incident, reached by one click.
  //
  // The realm registers `/`, which resolves against the auth server's own
  // origin (Keycloak is served from this same host under `/realms`). Sending
  // a bare `window.location.origin` -- no trailing slash -- is a different
  // string and does not match. `backend/tests/test_console_auth_realm.py::
  // test_the_post_logout_uri_the_browser_asks_for_is_one_the_realm_accepts`
  // compares this line against the realm file, because neither file is wrong
  // on its own and only the pair can be checked.
  post_logout_redirect_uri: `${window.location.origin}/`,
  response_type: "code",
  scope: "openid profile email",
  automaticSilentRenew: true,
  userStore: new WebStorageStateStore({ store: window.sessionStorage }),
});

let currentUser: User | null = null;

userManager.events.addUserLoaded((user) => {
  currentUser = user;
});
userManager.events.addUserUnloaded(() => {
  currentUser = null;
});

/**
 * Whether the session is known to be ending, and a way to hear about it.
 *
 * "THE SESSION DROPS MID-TASK DURING CONTINUOUS ACTIVE USE, WITH NO WARNING,
 * NO EXPIRY NOTICE, AND TOTAL LOSS OF CONTEXT" -- auditor A, HIGH. Silent
 * renewal is configured and usually works; when it does not (the identity
 * provider's SSO session hit its own maximum lifespan, the network dropped,
 * the realm was restarted) the console simply stopped being signed in. The
 * user found out by having a request fail.
 *
 * `signIn()` above now returns them to the page they were on, which limits
 * the damage. This is the other half: SAY SO. A renewal that failed is a
 * fact the console knows a minute before the user does, and a person who is
 * told can finish the sentence they are typing and save.
 *
 * DELIBERATELY NOT A MODAL, and not an automatic redirect. Interrupting
 * someone mid-form to announce that they are about to be interrupted is the
 * same defect wearing a politer face; the shell renders this as a banner and
 * lets them choose when to re-authenticate.
 */
type SessionListener = (state: { expiring: boolean }) => void;
const sessionListeners = new Set<SessionListener>();
let sessionExpiring = false;

function announce(expiring: boolean): void {
  if (sessionExpiring === expiring) return;
  sessionExpiring = expiring;
  for (const listener of sessionListeners) listener({ expiring });
}

/** Subscribe to session-expiry notices. Returns an unsubscribe function. */
export function onSessionExpiring(listener: SessionListener): () => void {
  sessionListeners.add(listener);
  return () => sessionListeners.delete(listener);
}

export function isSessionExpiring(): boolean {
  return sessionExpiring;
}

// A FAILED RENEWAL, not an expiring token, is what the user needs to hear
// about. `addAccessTokenExpiring` fires roughly a minute before every expiry
// and is followed by a successful silent renew almost every time; announcing
// on it would train people to ignore the banner, which is worse than not
// having one. `addSilentRenewError` is the event that means the session is
// genuinely ending.
userManager.events.addSilentRenewError(() => announce(true));
userManager.events.addUserLoaded(() => announce(false));

export async function initAuth(): Promise<User | null> {
  currentUser = await userManager.getUser();
  return currentUser;
}

export function accessToken(): string | null {
  return currentUser?.access_token ?? null;
}

export function isAuthenticated(): boolean {
  return currentUser != null && !currentUser.expired;
}

/** The one route that must never be recorded as a place to return to. */
const CALLBACK_PATH = "/auth/callback";

/**
 * Start a sign-in, REMEMBERING WHERE THE USER WAS.
 *
 * TWO AUDIT FINDINGS, ONE MISSING LINE. This used to call `signinRedirect()`
 * with nothing, so `safeReturnTo(undefined)` fell through to
 * `DEFAULT_AFTER_SIGN_IN` every time:
 *
 *   * "Post-login redirect discards the originally requested deep link" --
 *     a bookmarked device, or a ticket link out of a notification email,
 *     silently dumps the user on an unrelated page and makes them re-navigate.
 *   * "The session drops mid-task during continuous active use, with no
 *     warning, no expiry notice, and total loss of context." Same line. When
 *     the token expires mid-task `RequireAuth` re-authenticates, and without a
 *     recorded page the user comes back to the front door instead of the form
 *     they were filling in.
 *
 * The value travels the same filtered path `startAccountAction` uses, so
 * `safeReturnTo`'s open-redirect refusals cover it: it is written into the
 * OIDC `state`, comes back over the wire, and is whitelisted before it reaches
 * `navigate()`.
 *
 * THE CALLBACK ROUTE IS EXCLUDED, and that exclusion is load-bearing rather
 * than tidy: recording `/auth/callback` would send the user back to the page
 * whose only job is to consume an authorization code, with no code in the URL
 * the second time -- a sign-in that lands on a failure screen.
 *
 * `ui_locales` IS THE CONSOLE'S LANGUAGE, sent so the sign-in page opens in it
 * before Keycloak knows who is signing in (once it does, the account's saved
 * `locale` attribute takes precedence there). Without it the page follows the
 * browser's Accept-Language alone, so a person who chose Spanish in the console
 * on an English browser would be signed out into an English page.
 */
export function signIn(): Promise<void> {
  const here = window.location.pathname + window.location.search;
  const returnTo = window.location.pathname.startsWith(CALLBACK_PATH) ? undefined : here;
  return userManager.signinRedirect({
    ui_locales: getLanguage(),
    ...(returnTo ? { state: { returnTo } } : {}),
  });
}

export async function completeSignIn(): Promise<User> {
  const user = await userManager.signinRedirectCallback();
  currentUser = user;
  return user;
}

export function signOut(): Promise<void> {
  return userManager.signoutRedirect();
}

/**
 * The credential steps a user may start from inside ITMS.
 *
 * A CLOSED UNION, not a string. These values are handed to the identity
 * provider as `kc_action`; an unknown one produces a redirect that
 * re-authenticates and silently does nothing, which presents as a button that
 * appears to work and does not.
 */
export type AccountAction =
  | "UPDATE_PASSWORD"
  | "CONFIGURE_TOTP"
  | "webauthn-register-passwordless";

const ACCOUNT_ACTIONS: readonly AccountAction[] = [
  "UPDATE_PASSWORD",
  "CONFIGURE_TOTP",
  "webauthn-register-passwordless",
];

/**
 * Run ONE credential step in the sign-in pages, then come back into ITMS.
 *
 * This is what replaces linking to the identity provider's own account
 * console: same theme, same tab, no second application. The platform still
 * never handles the credential — it only asks for the step to be run, and the
 * user authenticates to Keycloak directly, exactly as at sign-in.
 *
 * `state.returnTo` records the page that started the action, and
 * `App.tsx`'s `CallbackPage` reads it back through `safeReturnTo` below.
 *
 * HOW EXACT "the page you left" IS, now that it is exact. This carries a URL,
 * and the profile is a URL: `/profile`, since 2026-08-20.
 *
 * IT WAS NOT, AND THAT WAS A REPORTED BUG. The profile used to be a tab inside
 * `SettingsPage`, whose active tab lives in React state and is therefore not
 * in the URL -- so every credential step returned the user to `/settings` and
 * its default tab, Organization. From the operator's chair that is
 * indistinguishable from the step having done nothing: "when I click it just
 * refreshes and goes back to organization". The other half of that report was
 * a realm that had never registered the actions (see
 * `deploy/keycloak/init-clients.py`); this half was the return path.
 */
export async function startAccountAction(action: AccountAction): Promise<void> {
  if (!ACCOUNT_ACTIONS.includes(action)) {
    throw new Error(`unsupported account action: ${action}`);
  }
  await userManager.signinRedirect({
    // The sign-in pages open in the language the console is in; see `signIn`.
    ui_locales: getLanguage(),
    extraQueryParams: { kc_action: action },
    state: { returnTo: window.location.pathname + window.location.search },
  });
}

/**
 * Where sign-in lands when nothing better is known: the root, whose route
 * (`Landing` in `App.tsx`) picks the account's home -- the portal for a
 * requester, the fleet for an operator.
 */
export const DEFAULT_AFTER_SIGN_IN = "/";

/**
 * The path to land on after a callback, from OIDC `state` THAT CAME BACK OVER
 * THE WIRE.
 *
 * WHY THIS IS FILTERED AT ALL. `startAccountAction` writes `returnTo` into the
 * authorization request's `state`, and `oidc-client-ts` returns whatever came
 * back with the response. Handing that string to `navigate()` unchecked is an
 * OPEN REDIRECT through the auth callback: the history API resolves
 * `//evil.example` against the current scheme, so a protocol-relative path
 * leaves the origin entirely, and a phishing page then wears the URL the user
 * just typed their password into. That is a vulnerability, not a cosmetic bug,
 * so the rule is a whitelist and not a blacklist:
 *
 *   accept ONE leading "/", then anything that is not another "/" or "\".
 *
 * Which refuses, by construction: absolute URLs (`https://...`), scheme-only
 * payloads (`javascript:...`), protocol-relative paths (`//host`), the
 * backslash spellings browsers normalise INTO protocol-relative paths
 * (`/\host`), and bare relative paths, which would resolve against
 * `/auth/callback` and land somewhere nobody chose.
 *
 * Control characters are refused separately because browsers STRIP tab, CR and
 * LF from URLs before resolving them: `"/\tevil"` is not a path that starts
 * with `/` followed by a letter once the tab is gone -- and `"/\t/evil.example"`
 * would become `//evil.example`, defeating the check above.
 */
export function safeReturnTo(state: unknown): string {
  const raw = (state as { returnTo?: unknown } | null | undefined)?.returnTo;
  if (typeof raw !== "string") return DEFAULT_AFTER_SIGN_IN;
  if (/[\u0000-\u001f\u007f\s]/.test(raw)) return DEFAULT_AFTER_SIGN_IN;
  if (!/^\/(?![/\\])/.test(raw)) return DEFAULT_AFTER_SIGN_IN;
  return raw;
}
