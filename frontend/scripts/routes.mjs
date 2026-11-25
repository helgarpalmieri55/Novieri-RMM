/**
 * EVERY ROUTED PAGE, AS THE HARMONY GATE WALKS IT.
 *
 * One entry per `<Route path>` in `src/App.tsx`, with the dynamic segments
 * filled in from `PARAMS` so each is a URL a browser can open. This list is a
 * LITERAL on purpose: `src/test/routes-covered.test.ts` parses `App.tsx` and
 * fails when a route is mounted that is not named here (or named here and no
 * longer mounted), so adding a page is a deliberate act of adding it to the
 * gate, and never something the gate quietly stopped doing.
 *
 * `EXCLUDED` are the ones the gate cannot or must not walk: the dev-only
 * preview harness (it has its own browser pass, `a11y-browser.mjs`), Email
 * intake's Microsoft return (a page whose only job is to consume an
 * authorization code the gate does not have), the OAuth consent page
 * (`/oauth/authorize`, plan 2026-11-06: it needs an AI app's authorization
 * request the gate does not have) and the `*` redirect. The
 * console's own OIDC callback, `/auth/callback`, left with `oidc-client-ts`
 * (own-auth plan, Task 10).
 *
 * Read by `harmony.mjs` (Node) and by the vitest guard, so it is plain ESM
 * with no imports. The `.d.mts` beside it is what lets the TypeScript test
 * import it under `strict`.
 */

/**
 * THE CLIENT HARMONY CHOOSES IN THE TOP BAR for its passes with a client
 * chosen: Andes Logistics, `src/fixtures/core.ts`'s `CUSTOMER_ID`. A UUID,
 * because the console reads back nothing else from `itms.client`.
 */
export const CHOSEN_CLIENT = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57";

/** Sample ids for every `:param` in `App.tsx`. Same value in every fixture. */
export const PARAMS = Object.freeze({
  deviceId: "dev-1",
  runId: "run-1",
  jobId: "job-1",
  ticketId: "tkt-1",
  changeId: "chg-1",
  planId: "plan-1",
  testId: "test-1",
  licenseId: "lic-1",
  subscriptionId: "sub-1",
  personId: "person-maria",
  reportKey: "patch-compliance",
  siteServerId: "ss-1",
  actionId: "act-1",
  subnetId: "subnet-1",
  sessionId: "sess-1",
  clientId: CHOSEN_CLIENT,
});

export const EXCLUDED = new Set([
  "/__preview",
  "/settings/email-intake/oauth/callback",
  "/oauth/authorize",
  "*",
]);

/** `/devices/:deviceId` -> `/devices/dev-1`. Throws on a param with no sample. */
export function fillPath(pattern) {
  return pattern.replace(/:([A-Za-z]+)/g, (_, name) => {
    const value = PARAMS[name];
    if (value === undefined) throw new Error(`routes.mjs: no sample id for :${name} in ${pattern}`);
    return value;
  });
}

/** `/devices/dev-1` -> `/devices/:deviceId`, for matching the prototype's page keys. */
export function patternOf(route) {
  let out = route;
  for (const [name, value] of Object.entries(PARAMS)) {
    out = out.split(`/${value}`).join(`/:${name}`);
  }
  return out;
}

/**
 * ROUTES WHOSE `<details>` THE WALK OPENS BEFORE IT MEASURES. A closed
 * disclosure's controls are in the DOM but drawn nowhere, so axe, the token
 * diff and the overflow probe never see them. The Users tab keeps every
 * row's Disable, Reset, Sign out everywhere, Resend, Cancel and Delete behind
 * one (final review D2, Minor 5).
 */
export const OPEN_DISCLOSURES = new Set(["/settings?tab=users"]);

/**
 * THE ROUTES THAT DRAW NO CONSOLE SHELL: the pages reached without a session
 * (own-auth Task 11), which have the sign-in frame instead. Every other route
 * draws the top bar, and harmony fails one that does not (`missingShell`,
 * redesign plan pre-flight C1): a page the dev server could not serve draws
 * nothing, and an empty frame measures clean. Measured 2026-11-24: these ten
 * are exactly the routes of a full walk with no `.topbar`.
 */
export const FRAMELESS = new Set([
  "/sign-in",
  "/sign-in/factor",
  "/sign-in/enroll",
  "/sign-in/recovery-codes",
  "/sign-in/microsoft",
  "/forgot-password",
  "/reset-password",
  "/invite",
  "/recover",
  "/portal/sign-in",
]);

export const ROUTES = [
  // The pages reached without a session (own-auth Task 11). No top bar, so
  // harmony measures their tokens, overflow and axe, and skips the bar.
  "/sign-in",
  "/sign-in/factor",
  "/sign-in/enroll",
  "/sign-in/recovery-codes",
  // Microsoft's return (own-auth phase 2, Task 12). Harmony cannot add the
  // fragment Microsoft does, so it measures the page a reload shows.
  "/sign-in/microsoft",
  "/forgot-password",
  // The emailed links carry their token in the fragment, which harmony cannot
  // add (`?__fixtures` would land inside it); the fixtures answer any token.
  "/reset-password",
  "/invite",
  "/recover",
  "/portal/sign-in",
  "/devices",
  "/devices/onboard",
  "/devices/push-runs",
  "/devices/push-runs/run-1",
  "/devices/dev-1",
  "/devices/dev-1?tab=hardware",
  "/devices/dev-1?tab=health",
  "/software",
  "/credentials",
  "/docs",
  "/security",
  // THE STATES THE PAGE WAVES PUT IN THE URL (kit requests batch 1, item 8):
  // a drawer or an editor opened from a query parameter is drawn on no plain
  // route, so the gate measures it only as a variant. Each id is one the
  // module's fixtures answer. On a base whose page ignores the parameter the
  // variant draws the plain page, which still passes.
  "/security?event=sec-1",
  "/jobs",
  "/jobs?job=job-1",
  "/remote",
  "/remote/sess-1",
  "/remote/sess-1/recording",
  "/patch",
  "/patch/policies",
  "/patch/policies?policy=pol-default",
  "/deploy",
  "/deploy/packages",
  "/deploy/scripts",
  "/backup",
  "/backup/repositories",
  "/backup/policies",
  "/backup/coverage",
  "/backup/offsite",
  // The off-site storage editor (`?edit=<id>`), drawn with the fixture
  // session's `backup.offsite.manage`.
  "/backup/offsite?edit=offsite-wasabi",
  "/provisioning",
  "/provisioning/images",
  "/provisioning/profiles",
  "/changes",
  "/changes/approvals",
  "/changes/calendar",
  "/changes/chg-1",
  "/bcp",
  "/bcp/plans/plan-1",
  "/bcp/report",
  "/bcp/tests/test-1",
  "/bcp/tests/test-1/report",
  "/licenses/vendors",
  "/licenses/products",
  "/licenses/compliance",
  "/licenses/review-queue",
  "/licenses/renewals",
  "/licenses",
  "/licenses/lic-1",
  "/subscriptions/spend",
  "/subscriptions/renewals",
  "/subscriptions",
  "/subscriptions/sub-1",
  "/people",
  "/people/unmatched",
  "/people/person-maria",
  "/budget",
  "/budget/planner",
  "/budget/expenses",
  "/budget/confirm",
  "/budget/admin",
  "/service-desk",
  "/service-desk/my-work",
  "/service-desk/admin",
  "/service-desk/tickets/tkt-1",
  "/notifications",
  "/reports",
  "/reports/patch-compliance",
  "/site-servers",
  "/site-servers/ss-1",
  "/sla",
  "/health",
  // Alerts' configuration views (redesign decision D21): the profiles, the
  // approved actions and the settings that /health alone no longer draws.
  "/health?view=profiles",
  "/health?view=actions",
  "/health?view=settings",
  "/platform-status",
  "/error-monitor",
  "/error-monitor?group=eg-1",
  "/assistant",
  "/assistant/settings",
  "/assistant/autonomy",
  "/assistant/actions",
  "/assistant/actions/act-1",
  "/agent-push",
  "/agent-push/authorizations",
  "/agent-push/credential-scopes",
  "/ipam",
  "/ipam/drift",
  "/ipam/search",
  "/ipam/registry",
  "/ipam/scan-authorizations",
  "/ipam/import",
  "/ipam/subnets/subnet-1",
  "/ipam/subnets/subnet-1?tab=sweeps",
  "/email-intake",
  "/portal",
  "/portal/new",
  "/portal/tickets/tkt-1",
  "/portal/security",
  "/dashboard",
  "/audit",
  "/enrollment",
  // Enrollment -> Certificates (kit requests batch 2, K7): `/enrollment` opens
  // Tokens, so the certificates' list and its revoked chip are walked only here.
  "/enrollment?tab=certificates",
  "/device-groups",
  "/discovered",
  "/agent-packages",
  "/clients",
  "/clients/5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57",
  // The client 360's tabs that read other modules (redesign design §1.3).
  "/clients/5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57?tab=devices",
  "/clients/5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57?tab=tickets",
  "/clients/5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57?tab=alerts",
  "/clients/5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57?tab=assets",
  "/settings",
  // A QUERY VARIANT: `/settings` opens Organization, so no other tab is ever
  // measured. A variant is `<a route listed here>?<query>`; the guard checks
  // the part before `?` against App.tsx, and harmony joins its own
  // `__fixtures` flag with `&`. Settings -> AI connections is walked because
  // it is the console's door for connecting AI apps (MCP design §8).
  "/settings?tab=ai-connections",
  // Settings -> Sessions (own authentication design §2.4) is walked for the
  // same reason: `/settings` alone never opens it.
  "/settings?tab=sessions",
  // Settings -> Microsoft sign-in (own-auth phase 2, Task 12), for the same
  // reason: the application, the redirect address and the certificate.
  "/settings?tab=microsoft",
  // Settings -> Users (final review D2, Minor 5): the screen own
  // authentication changed most -- the invite form, the shown-once link,
  // and the row's actions, with Resend and Cancel for the fixtures' open
  // invitation (`usr-invited-seed`).
  "/settings?tab=users",
  // Settings -> Email and -> Roles (own-auth phase 2, Task 14), for the same
  // reason as Sessions: their saves now ask for a recent second factor, and
  // `/settings` alone never opens either. No `<details>` to open: the Email
  // fixture signs in with a password, and Roles has none.
  "/settings?tab=email",
  "/settings?tab=roles",
  "/admin",
  "/governance",
  "/assets",
  "/profile",
  "/profile/security",
];
