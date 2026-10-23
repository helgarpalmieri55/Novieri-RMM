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
 * preview harness (it has its own browser pass, `a11y-browser.mjs`), the two
 * sign-in callbacks (the console's own and Email intake's Microsoft return,
 * pages whose only job is to consume an authorization code the gate does not
 * have) and the `*` redirect.
 *
 * Read by `harmony.mjs` (Node) and by the vitest guard, so it is plain ESM
 * with no imports. The `.d.mts` beside it is what lets the TypeScript test
 * import it under `strict`.
 */

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
});

export const EXCLUDED = new Set([
  "/__preview",
  "/auth/callback",
  "/settings/email-intake/oauth/callback",
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

export const ROUTES = [
  "/devices",
  "/devices/onboard",
  "/devices/push-runs",
  "/devices/push-runs/run-1",
  "/devices/dev-1",
  "/software",
  "/credentials",
  "/docs",
  "/security",
  "/jobs",
  "/remote",
  "/remote/sess-1",
  "/remote/sess-1/recording",
  "/patch",
  "/patch/policies",
  "/deploy",
  "/deploy/packages",
  "/deploy/scripts",
  "/backup",
  "/backup/repositories",
  "/backup/policies",
  "/backup/coverage",
  "/backup/offsite",
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
  "/platform-status",
  "/error-monitor",
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
  "/email-intake",
  "/portal",
  "/portal/new",
  "/portal/tickets/tkt-1",
  "/dashboard",
  "/audit",
  "/enrollment",
  "/device-groups",
  "/discovered",
  "/agent-packages",
  "/settings",
  // A QUERY VARIANT: `/settings` opens Organization, so no other tab is ever
  // measured. A variant is `<a route listed here>?<query>`; the guard checks
  // the part before `?` against App.tsx, and harmony joins its own
  // `__fixtures` flag with `&`. Settings -> AI connections is walked because
  // it is the console's door for connecting AI apps (MCP design §8).
  "/settings?tab=ai-connections",
  "/admin",
  "/governance",
  "/assets",
  "/profile",
];
