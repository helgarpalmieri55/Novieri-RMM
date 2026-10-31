import { QueryClient, QueryClientProvider, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { AppShell } from "./shell/AppShell";
import { useHealthReading } from "./shell/healthSignal";
import { useReviewCount } from "./shell/useReviewCount";
import { crumbFor, isPortalOnly, portalCrumbFor, searchDestinations } from "./shell/nav";
import { AreaPage } from "./shell/AreaPage";
import { searchPrompt, useRecordSearch } from "./shell/recordSearch";
import { ApiProvider, useApi } from "./lib/ApiContext";
import { ClientScopeProvider } from "./lib/clientScope";
import { ClientSelector } from "./shell/ClientSelector";
import { I18nProvider, readStoredLanguage, useLanguage, useT } from "./i18n/context";
import { setActiveLanguage, subscribe, translate } from "./i18n/core";
import { resolveLanguage } from "./i18n/resolve";
import type { Language } from "./i18n/types";
import { Trans } from "./i18n/Trans";
import { Toaster, type ToastItem } from "./shell/Toaster";
import { FIXTURE_MODE } from "./fixtures/mode";
import { PreviewPage } from "./preview/PreviewPage";
import {
  completeSignIn,
  initAuth,
  isAuthenticated,
  isSessionExpiring,
  oidcAuthority,
  onSessionExpiring,
  safeReturnTo,
  signIn,
  signOut,
} from "./lib/auth";
import { roleChipName } from "./modules/settings/types";
import { CandidatesPage } from "./modules/devices/CandidatesPage";
import { DevicesPage } from "./modules/devices/DevicesPage";
import { OnboardPage } from "./modules/onboarding/OnboardPage";
import { GroupsPage } from "./modules/devices/GroupsPage";
import { DeviceDetailPage } from "./modules/inventory/DeviceDetailPage";
import { SoftwarePage } from "./modules/inventory/SoftwarePage";
import { SecurityPage } from "./modules/security/SecurityPage";
import { DocsPage } from "./modules/docs/DocsPage";
import { CredentialsPage } from "./modules/credentials/CredentialsPage";
import { JobsPage } from "./modules/jobs/JobsPage";
import { SessionsPage } from "./modules/remote/SessionsPage";
import { ScreenViewer } from "./modules/remote/ScreenViewer";
import { RecordingPlayer } from "./modules/remote/RecordingPlayer";
import { FleetPatchPage } from "./modules/patch/FleetPatchPage";
import { PatchPolicyEditor } from "./modules/patch/PatchPolicyEditor";
import { DeployPage } from "./modules/deploy/DeployPage";
import { PackagesPage } from "./modules/deploy/PackagesPage";
import { ScriptsPage } from "./modules/deploy/ScriptsPage";
import { BackupPointsPage } from "./modules/backup/BackupPointsPage";
import { RepositoriesPage } from "./modules/backup/RepositoriesPage";
import { BackupPolicyEditor } from "./modules/backup/BackupPolicyEditor";
import { CoveragePage as BackupCoveragePage } from "./modules/backup/CoveragePage";
import { OffsitePage } from "./modules/backup/OffsitePage";
import { ProvisioningRunsPage } from "./modules/provisioning/ProvisioningRunsPage";
import { ImagesPage } from "./modules/provisioning/ImagesPage";
import { ProfilesPage } from "./modules/provisioning/ProfilesPage";
import { ChangesPage } from "./modules/change/ChangesPage";
import { ApprovalQueue } from "./modules/change/ApprovalQueue";
import { ChangeDetail } from "./modules/change/ChangeDetail";
import { ChangeCalendar } from "./modules/change/ChangeCalendar";
import { PlansPage } from "./modules/bcp/PlansPage";
import { PlanDetail } from "./modules/bcp/PlanDetail";
import { TestRecorder } from "./modules/bcp/TestRecorder";
import { BcpReport } from "./modules/bcp/BcpReport";
import { VendorsPage } from "./modules/licenses/VendorsPage";
import { ProductsPage } from "./modules/licenses/ProductsPage";
import { LicensesPage } from "./modules/licenses/LicensesPage";
import { LicenseDetail } from "./modules/licenses/LicenseDetail";
import { ComplianceDashboard } from "./modules/licenses/ComplianceDashboard";
import { ReviewQueue } from "./modules/licenses/ReviewQueue";
import { RenewalCalendar } from "./modules/licenses/RenewalCalendar";
import { SubscriptionsPage } from "./modules/subscriptions/SubscriptionsPage";
import { SubscriptionDetail } from "./modules/subscriptions/SubscriptionDetail";
import { PersonPage } from "./modules/people/PersonPage";
import { UnmatchedPage } from "./modules/people/UnmatchedPage";
import { SpendDashboard } from "./modules/subscriptions/SpendDashboard";
import { RenewalCalendar as SubscriptionRenewalCalendar } from "./modules/subscriptions/RenewalCalendar";
import { BudgetDashboard } from "./modules/budget/BudgetDashboard";
import { BudgetPlannerPage } from "./modules/budget/BudgetPlannerPage";
import { ExpensesPage } from "./modules/budget/ExpensesPage";
import { ConfirmationQueuePage } from "./modules/budget/ConfirmationQueuePage";
import { BudgetAdminPage } from "./modules/budget/BudgetAdminPage";
import { TicketListPage } from "./modules/service-desk/TicketListPage";
import { TicketDetailPage } from "./modules/service-desk/TicketDetailPage";
import { MyWorkPage } from "./modules/service-desk/MyWorkPage";
import { ServiceDeskAdminPage } from "./modules/service-desk/ServiceDeskAdminPage";
import { NotificationAdminPage } from "./modules/notification/NotificationAdminPage";
import { ReportCataloguePage } from "./modules/reporting/ReportCatalogue";
import { ReportViewPage } from "./modules/reporting/ReportView";
import { EmailIntakeAdminPage } from "./modules/email-intake/EmailIntakeAdminPage";
import { OAuthCallback as EmailIntakeOAuthCallback } from "./modules/email-intake/OAuthCallback";
import { SiteServerDetailPage } from "./modules/site-servers/SiteServerDetailPage";
import { SiteServersPage } from "./modules/site-servers/SiteServersPage";
import { SlaAdminPage } from "./modules/sla/SlaAdminPage";
import { HealthPage } from "./modules/health/HealthPage";
import { PlatformStatusPage } from "./modules/platform-status/PlatformStatusPage";
import { ErrorMonitorPage } from "./modules/error-monitor/ErrorMonitorPage";
import { SubnetTreePage } from "./modules/ipam/SubnetTreePage";
import { SubnetDetailPage } from "./modules/ipam/SubnetDetailPage";
import { DriftPage } from "./modules/ipam/DriftPage";
import { SearchPage as IpamSearchPage } from "./modules/ipam/SearchPage";
import { RegistryPage as IpamRegistryPage } from "./modules/ipam/RegistryPage";
import { ScanAuthorizationsPage } from "./modules/ipam/ScanAuthorizationsPage";
import { PushAuthorizationsPage } from "./modules/agent-push/PushAuthorizationsPage";
import { PushCredentialScopesPage } from "./modules/agent-push/PushCredentialScopesPage";
import { PushRunPage } from "./modules/agent-push/PushRunPage";
import { PushRunsPage } from "./modules/agent-push/PushRunsPage";
import { ImportPage as IpamImportPage } from "./modules/ipam/ImportPage";
import { AiSettingsPage } from "./modules/ai-admin/AiSettingsPage";
import { ChatPanel } from "./modules/ai-admin/ChatPanel";
import { AiActionPage, AiActionsPage } from "./modules/ai-automation/ActionsPage";
import { AutonomyDialPage } from "./modules/ai-automation/AutonomyDialPage";
import { DashboardPage } from "./modules/dashboard/DashboardPage";
import { AuditPage } from "./modules/audit/AuditPage";
import { AgentReleasesPage } from "./modules/agent-releases/AgentReleasesPage";
import { EnrollmentPage } from "./modules/enrollment/EnrollmentPage";
import { ProfilePage } from "./modules/profile/ProfilePage";
import { SettingsPage } from "./modules/settings/SettingsPage";
import { SetupPage, fetchSetupStatus } from "./modules/setup/SetupPage";
import { MyTicketsPage } from "./modules/portal/MyTicketsPage";
import { NewTicketPage } from "./modules/portal/NewTicketPage";
import { PortalTicketDetailPage } from "./modules/portal/PortalTicketDetailPage";

// Exported so a test that renders `<App />` more than once can reset it. The
// client is module-level — one per browser tab, which is right in production
// and wrong in a test file, where a cached answer from the previous case
// (`setup/status`, held with `staleTime: Infinity`) would satisfy the next one
// without a request ever being made.
//
// A change of the top bar's client empties it (`queryClient.clear()`, in
// `lib/clientScope.tsx`'s provider, which reaches this same client through
// `useQueryClient`): nothing read for one client is shown under another.
export const queryClient = new QueryClient({
  // No retry in fixture mode: a `FixtureMissing` rejects the same way on the
  // second attempt, and the harmony walk should reach the page's error state
  // in one step rather than after a back-off.
  defaultOptions: { queries: { retry: FIXTURE_MODE ? false : 1, staleTime: 5000 } },
});

/**
 * The OIDC redirect lands here, and it lands here for TWO different journeys:
 * an ordinary sign-in, and a credential step the user started from inside ITMS
 * (`startAccountAction`). The second one is the reason this reads `state`.
 *
 * A CREDENTIAL STEP THAT DUMPS YOU ON THE DEVICE LIST IS A HALF-KEPT PROMISE.
 * The whole argument for an application-initiated action over a link to the
 * identity provider's own account pages is that the user never leaves ITMS --
 * and "never leaves" includes coming back to the tab they were on, not to the
 * app's front door. `startAccountAction` records that page in the OIDC `state`;
 * this reads it back.
 *
 * `safeReturnTo` IS NOT OPTIONAL AND IS NOT A FORMATTING DETAIL. The value
 * arrives from the identity provider, so it is untrusted input, and
 * `navigate()` on a protocol-relative path (`//evil.example`) leaves the origin
 * -- an open redirect out of the auth callback, which is the one route in the
 * application a phishing chain most wants. The filter is in `lib/auth.ts` with
 * its whitelist stated; the refusals are tested in `App.auth.test.tsx`.
 */
export function CallbackPage() {
  const t = useT();
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    completeSignIn()
      .then((user) => navigate(safeReturnTo(user?.state), { replace: true }))
      .catch((err: unknown) => setError(String(err)));
  }, [navigate]);
  // `role="alert"` because this screen is the END of a redirect chain: the
  // browser has just navigated back into the console on its own, so nobody's
  // focus is anywhere useful and a sighted user is looking at a page that
  // changed under them. Auditor A found the untreated version of exactly this
  // -- "the browser ends up back at /login showing the identical card. No
  // error text, no banner, no toast, no reason" -- and an unannounced
  // paragraph is that same silence for anyone not watching the pixels.
  if (error)
    return (
      <p className="p-8 text-[var(--red)]" role="alert">
        {t("shell.app.signInFailed", { error })}
      </p>
    );
  return <p className="p-8 text-[var(--muted)]">{t("shell.app.completingSignIn")}</p>;
}

/**
 * The chrome every authenticated route renders inside.
 *
 * EXPORTED FOR ONE REASON: `App.crumb.test.tsx`. Everything this component
 * decides -- which crumb, which link, which tab title -- was reachable only by
 * rendering the whole router, so none of it was asserted anywhere. It is not
 * exported to be rendered by anything but `RequireAuth`.
 */
export function Shell({ children }: { children: React.ReactNode }) {
  // Subscribes the shell to a language switch: the crumb and the palette's page
  // list below are read from the nav model, not from props that would change.
  const { language } = useLanguage();
  const t = useT();
  const location = useLocation();
  const api = useApi();
  const me = useQuery({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const org = useQuery({
    queryKey: ["orgs", "current"],
    queryFn: () => api.getCurrentOrg(),
    staleTime: 300_000,
  });
  const permissions = me.data?.permissions ?? [];
  const navigate = useNavigate();

  // The saved language arrives with `/me`, after the provider has rendered, so
  // the provider's `initial` (applied once, at first render) cannot carry it.
  // Keyed on the saved value only: a refetch that returns the same locale must
  // not undo a choice the person has just made and is still saving.
  const savedLocale = me.data?.locale;
  useEffect(() => {
    if (savedLocale === undefined) return;
    setActiveLanguage(resolveLanguage(savedLocale, readStoredLanguage(), navigator.language));
  }, [savedLocale]);

  /**
   * WHAT THE HEALTH GLYPH AND THE RAIL'S BADGE ARE MADE OF, and until now the
   * answer was nothing at all.
   *
   * `AppShell` has always accepted `badges` and `Sidebar` has always read it,
   * and NO CALLER EVER PASSED IT -- `grep -rn 'badges=' src` returned only the
   * shell's own tests and the preview page. So `alertCount` was structurally 0
   * on every deployment: the rail's /health badge and the top bar's counter dot
   * had never once rendered for an operator, and the top bar's icon was a
   * static alert triangle beside them. The same class of defect as the search
   * field that searched nothing -- a component waiting for a prop.
   *
   * ONE READING, TWO SURFACES. The rail's badge and the glyph are built from
   * the same object, so they cannot disagree about how many alerts are open,
   * and every request behind it is gated on the permission its route requires
   * (`shell/healthSignal.ts`).
   */
  const health = useHealthReading(permissions);
  // ABSENT, NOT ZERO, when nothing measured it. `Sidebar` renders no badge for
  // a missing key, which is the honest picture for an operator whose alert
  // query was refused -- a "0" pill would be the console asserting a fact it
  // never established.
  //
  // Computers waiting for review, for someone who can approve them (install-
  // link spec §1.5). Same rule as the alert count: ABSENT, not zero, when
  // nothing measured it.
  const reviewCount = useReviewCount(permissions);
  const badges = useMemo(() => {
    const counts: Record<string, number> = {};
    if (health.openAlerts !== null) counts["/health"] = health.openAlerts;
    if (reviewCount !== null) counts["/devices"] = reviewCount;
    return Object.keys(counts).length > 0 ? counts : undefined;
  }, [health.openAlerts, reviewCount]);

  // The session notice, subscribed once. `isSessionExpiring()` seeds it so a
  // renewal that failed while this component was unmounted is not missed.
  const [expiring, setExpiring] = useState(isSessionExpiring);
  useEffect(() => onSessionExpiring(({ expiring: value }) => setExpiring(value)), []);

  // Breadcrumb and page title come from the nav model rather than a second
  // hand-maintained route->title map -- and the derivation itself lives in
  // `shell/nav.ts` beside that model, as a pure function, because a rule
  // buried in this component was a rule no test could reach. `crumbFor`'s
  // header states what it decides; `nav.test.ts` holds it to it, including
  // the `sectionTo` that silently degrades to dead text when a group is
  // renamed.
  const crumb = crumbFor(location.pathname);

  // WHAT THE SEARCH FIELD SEARCHES. It searched NOTHING until now: `AppShell`
  // has always accepted `searchItems` and defaulted it to `[]`, and no caller
  // ever passed it, so every query in the deployed console reached "No records
  // match that" however well it was spelled. The Ask half worked, because
  // `onAsk` below was wired; the Find half was a component waiting for a prop.
  //
  // Destinations, drawn from the SAME nav model the rail and the breadcrumb
  // use, filtered by the same permissions — so search can never offer a page
  // the rail would have hidden, and there is one list to keep correct rather
  // than a second one that drifts.
  //
  // Records, too: `useRecordSearch` asks the list APIs this account can read,
  // debounced, and its hits go first. The placeholder and the empty state are
  // built from the same permission checks, so neither promises a record kind
  // the search will not look up.
  const [searchQuery, setSearchQuery] = useState("");
  const records = useRecordSearch(searchQuery, permissions);
  const searchItems = useMemo(
    () => [
      ...records.hits.map((hit) => ({
        id: hit.id,
        label: hit.label,
        sub: hit.sub,
        type: hit.type,
        icon: hit.icon,
        prefiltered: true,
        onSelect: () => navigate(hit.to),
      })),
      ...searchDestinations(permissions).map((page) => ({
        id: page.to,
        label: page.label,
        sub: page.within,
        type: t("shell.app.pageType"),
        icon: page.icon,
        onSelect: () => navigate(page.to),
      })),
    ],
    [records.hits, permissions, navigate, language, t],
  );
  const portalOnly = isPortalOnly(permissions);
  const prompt = searchPrompt(permissions, portalOnly);
  // A requester's crumb names the page and no operator section.
  const shownCrumb = portalOnly ? portalCrumbFor(location.pathname) : crumb;

  // A REQUESTER ON AN OPERATOR PAGE IS SENT TO THE PORTAL. Every such page
  // would answer them with permission errors; the server refuses the data
  // either way, so this is a courtesy, not a control (invariant 6).
  if (portalOnly && !location.pathname.startsWith("/portal") && location.pathname !== "/profile") {
    return <Navigate to="/portal" replace />;
  }

  return (
    <AppShell
      permissions={permissions}
      clientSelector={<ClientSelector />}
      searchItems={searchItems}
      onSearchQueryChange={setSearchQuery}
      searchPending={records.pending}
      searchPlaceholder={prompt.placeholder}
      searchHint={prompt.hint}
      badges={badges}
      health={health}
      // THE ORG'S REAL NAME, from `GET /orgs/current`. It was the literal
      // string "Operations" on every deployment, for every tenant -- which is
      // also why both UX auditors reported the tenant control as reading
      // "Operations": it was never showing anything else.
      orgName={org.data?.name ?? (me.data?.org_id ? "\u2026" : "\u2014")}
      /* THE ASK ROW'S DESTINATION, and it is omitted rather than disabled for
         an operator without `ai.use`: an affordance that opens a 403 is worse
         than one that was never offered. The question travels in the URL so
         the assistant is reachable with it by any other route too — a pasted
         link, a saved bookmark. */
      onAsk={
        permissions.includes("ai.use")
          ? (question) => navigate(`/assistant?q=${encodeURIComponent(question)}`)
          : undefined
      }
      section={shownCrumb.section}
      // The rail entry the page lives under, so "Admin" goes to the Admin
      // landing page. An orphan route has none, and gets a plain span.
      sectionTo={shownCrumb.sectionTo}
      title={shownCrumb.title}
      user={{
        name: me.data?.display_name ?? "\u2026",
        email: me.data?.email,
        // WAS THE CONSTANT "Operator", under every user's name, on every
        // deployment. A super-admin was labelled Operator and so was a
        // read-only auditor. The chip's job at that spot is to answer "who am
        // I acting as" before somebody acts on a whole fleet, and an invented
        // answer is worse than none because it is believed.
        // `roleChipName`: as stored in English, the seeded role's own name in another language.
        role: roleChipName(me.data?.roles?.[0] ?? ""),
      }}
      onSignOut={() => void signOut()}
      sessionExpiring={expiring}
      onSignIn={() => void signIn()}
    >
      {children}
    </AppShell>
  );
}

/** Where the root and any unknown path send this account. */
export function homeFor(permissions: readonly string[]): string {
  if (isPortalOnly(permissions)) return "/portal";
  return permissions.includes("devices.read") ? "/devices" : "/dashboard";
}

/**
 * THE ROOT AND EVERY UNKNOWN PATH. It sent everyone to `/devices`, so a
 * requester signed in to a fleet page they cannot read. Waits for `/me` so the
 * choice is made on the account's real permissions.
 */
export function Landing() {
  const t = useT();
  const api = useApi();
  const me = useQuery({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  if (me.isPending) return <p className="p-8 text-[var(--muted)]">{t("shell.ui.loading")}</p>;
  return <Navigate to={homeFor(me.data?.permissions ?? [])} replace />;
}

/**
 * The guard. An unauthenticated visitor goes to Keycloak immediately: there is
 * no ITMS launcher any more (docs/modules/console-auth/spec.md §3.4).
 *
 * THE LAUNCHER WAS COVERING TWO FAILURES, and removing it means handling both.
 * A page with a button renders whether or not Keycloak is reachable; an
 * automatic redirect has nothing to render when the redirect itself fails. So
 * a rejected `signIn()` becomes a screen that NAMES the authority it tried --
 * `lib/auth.ts`'s header records what the alternative costs: a login that did
 * nothing, and no error anywhere.
 *
 * `redirecting` is a ref rather than state because `signinRedirect()` is a side
 * effect that navigates the whole page. Firing it from an effect that runs
 * twice -- which React strict mode does deliberately -- starts two
 * authorization requests, and the second overwrites the first's PKCE state in
 * sessionStorage. The symptom is a callback failing with "state mismatch"
 * after a login that looked fine.
 */
export function RequireAuth({ children }: { children: React.ReactNode }) {
  const t = useT();
  const [error, setError] = useState<string | null>(null);
  const redirecting = useRef(false);
  // FIXTURE MODE COUNTS AS SIGNED IN, and only there. The flag is `false` in
  // a production build (`fixtures/mode.ts`), so this is the review harness
  // rendering every route over invented data, never the console skipping its
  // identity provider. The frontend gates nothing (invariant 6): a session
  // that looks signed in here still holds no token, and a real API would
  // answer every request with 401.
  const authed = isAuthenticated() || FIXTURE_MODE;

  useEffect(() => {
    if (authed || redirecting.current) return;
    redirecting.current = true;
    signIn().catch((err: unknown) => setError(String(err)));
  }, [authed]);

  // INSIDE THE GUARD, AROUND THE SHELL: the client selection needs `/me`, which
  // only a signed-in session can ask, and a change of client starts the shell
  // and the page again (`lib/clientScope.tsx`).
  if (authed) {
    return (
      <ClientScopeProvider>
        <Shell>{children}</Shell>
      </ClientScopeProvider>
    );
  }
  if (error) {
    return (
      <div className="flex min-h-screen items-center justify-center p-8">
        {/* Announced, for the same reason as the callback failure above: this
            replaces a redirect that was expected to leave the page. */}
        <div
          className="w-full max-w-md rounded-lg border border-[var(--line)] p-8 shadow-sm"
          role="alert"
        >
          <h1 className="mb-2 text-lg font-semibold">{t("shell.app.signInUnavailable")}</h1>
          <p className="mb-4 text-sm text-[var(--muted)]">
            <Trans
              k="shell.app.identityProviderUnreachable"
              vars={{ authority: oidcAuthority }}
              components={{ mono: <span className="font-mono" /> }}
            />
          </p>
          <p className="text-xs text-[var(--muted)]">{error}</p>
        </div>
      </div>
    );
  }
  return <p className="p-8 text-[var(--muted)]">{t("shell.app.redirecting")}</p>;
}

/**
 * BEFORE THE AUTH GATE, AND THAT ORDER IS THE WHOLE POINT.
 *
 * `RequireAuth` sends an unauthenticated visitor straight to the identity
 * provider. On an installation that has never been set up there is nobody to
 * authenticate as, so that redirect is a dead end with no way out: a sign-in
 * page for a platform with zero accounts, and nothing on screen that explains
 * it. The first browser visit to a new appliance is exactly that case, so the
 * question "does this installation have an owner yet?" has to be answered
 * before any route can trigger a redirect.
 *
 * Three states, and the third is the one worth stating:
 *   * `needed` -> the setup screen, whatever the URL. There is no other usable
 *     page on an unclaimed installation.
 *   * not needed -> the application, untouched.
 *   * the call FAILED -> the application, untouched. A backend that cannot
 *     answer must not strand every operator on a setup form for a platform
 *     that was claimed months ago; only an explicit `true` opens this door.
 *     (The server refuses a claim on a populated platform regardless, so the
 *     cost of this choice is a redirect, never a takeover.)
 *
 * `retry: false` because the query client's default retries once and the
 * console must not sit blank while it does. `staleTime: Infinity` because the
 * answer cannot change under a running tab: claiming the platform is what this
 * very screen does, and it renders its own outcome.
 */
export function SetupGate({ children }: { children: React.ReactNode }) {
  const t = useT();
  const status = useQuery({
    queryKey: ["setup", "status"],
    queryFn: fetchSetupStatus,
    retry: false,
    staleTime: Infinity,
    // Fixture mode has no backend to ask, and a claimed installation is the
    // only one worth screenshotting. Skipped here rather than answered by a
    // fixture because this request deliberately bypasses `ApiClient`.
    enabled: !FIXTURE_MODE,
  });
  if (FIXTURE_MODE) return <>{children}</>;
  if (status.isPending) return <p className="p-8 text-[var(--muted)]">{t("shell.app.starting")}</p>;
  if (status.data?.needed === true) return <SetupPage />;
  return <>{children}</>;
}

/**
 * Owns the language for the whole tree: the provider, and the save.
 *
 * A change made in the account menu or on the profile page is written to the
 * person's account (`PATCH /me`), so the next sign-in, on any browser, opens in
 * the language they chose. The change itself has already happened by the time
 * this runs -- the screen is not held waiting on the network -- so a failed
 * save is reported rather than reverted: the person is reading Spanish, and
 * snapping them back to English because a request failed would be the worse
 * surprise.
 */
let toastSeq = 0;

export function LanguageBridge({ children }: { children: React.ReactNode }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const onChange = useCallback(
    (language: Language) => {
      api
        .updateMe({ locale: language })
        .then((saved) => {
          // Saved here, but the sign-in pages did not follow: not an error,
          // so not the red toast -- the language did change and was kept.
          if (saved.identity_locale_synced === false) {
            setToasts((current) => [
              ...current,
              { id: `lang-${++toastSeq}`, title: translate("profile.page.languageNotMirrored"), tone: "info" },
            ]);
          }
          // The cached `/me` carries the saved locale; left stale, the profile
          // page would seed its form from the old value and an unrelated save
          // could flip the language back.
          return queryClient.invalidateQueries({ queryKey: ["me"] });
        })
        .catch(() => {
          setToasts((current) => [
            ...current,
            { id: `lang-${++toastSeq}`, title: translate("shell.accountMenu.saveFailed"), tone: "error" },
          ]);
        });
    },
    [api, queryClient],
  );
  const dismiss = useCallback(
    (id: string) => setToasts((current) => current.filter((item) => item.id !== id)),
    [],
  );
  // What the SERVER worded -- report prose, check-type hints, probe names,
  // refusals -- sits in the query cache in the language it was asked in, and
  // no query key carries the language. So a switch, from the menu or from the
  // profile page alike, refetches what is cached. `/me` is left to the save
  // above: refetching it while that save is in flight would read the old
  // locale back.
  useEffect(
    () =>
      subscribe(() => {
        void queryClient.invalidateQueries({ predicate: (query) => query.queryKey[0] !== "me" });
      }),
    [queryClient],
  );
  return (
    <I18nProvider
      initial={resolveLanguage(undefined, readStoredLanguage(), navigator.language)}
      onChange={onChange}
    >
      {children}
      <Toaster toasts={toasts} onDismiss={dismiss} />
    </I18nProvider>
  );
}

export default function App() {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    // No identity provider is consulted in fixture mode: `RequireAuth` treats
    // the session as signed in and `ApiProvider` hands out the fixture proxy.
    if (FIXTURE_MODE) {
      setReady(true);
      return;
    }
    void initAuth().finally(() => setReady(true));
  }, []);
  if (!ready) return null;
  return (
    <QueryClientProvider client={queryClient}>
      <ApiProvider>
        <LanguageBridge>
        <BrowserRouter>
          <SetupGate>
          <Routes>
        {/* DEV ONLY. Vite substitutes `false` here in a production build, so
          * rollup drops both the route and `preview/PreviewPage` from the
          * bundle. `preview.test.ts` asserts the built assets contain none of
          * it -- "guarded by a flag" and "absent from the artifact" are
          * different claims, and only the second one is a guarantee. */}
        {import.meta.env.DEV ? (
          <Route path="/__preview" element={<PreviewPage />} />
        ) : null}
            <Route path="/auth/callback" element={<CallbackPage />} />
            <Route
              path="/devices"
              element={
                <RequireAuth>
                  <DevicesPage />
                </RequireAuth>
              }
            />
            {/* BEFORE `/devices/:deviceId`, and not by accident. React Router
                ranks a static segment above a dynamic one, so this would win
                either way -- but the IPAM block below already declares its
                static segments first and says why, and one ordering rule in
                this file beats two. */}
            <Route
              path="/devices/onboard"
              element={
                <RequireAuth>
                  <OnboardPage />
                </RequireAuth>
              }
            />
            {/* Push runs (design 2026-09-30 §1.7). Static segments, so React
                Router ranks them above `/devices/:deviceId` regardless. */}
            <Route
              path="/devices/push-runs"
              element={
                <RequireAuth>
                  <PushRunsPage />
                </RequireAuth>
              }
            />
            <Route
              path="/devices/push-runs/:runId"
              element={
                <RequireAuth>
                  <PushRunPage />
                </RequireAuth>
              }
            />
            <Route
              path="/devices/:deviceId"
              element={
                <RequireAuth>
                  <DeviceDetailPage />
                </RequireAuth>
              }
            />
            <Route
              path="/software"
              element={
                <RequireAuth>
                  <SoftwarePage />
                </RequireAuth>
              }
            />
            <Route
              path="/credentials"
              element={
                <RequireAuth>
                  <CredentialsPage />
                </RequireAuth>
              }
            />
            <Route
              path="/docs"
              element={
                <RequireAuth>
                  <DocsPage />
                </RequireAuth>
              }
            />
            <Route
              path="/security"
              element={
                <RequireAuth>
                  <SecurityPage />
                </RequireAuth>
              }
            />
            <Route
              path="/jobs"
              element={
                <RequireAuth>
                  <JobsPage />
                </RequireAuth>
              }
            />
            <Route
              path="/remote"
              element={
                <RequireAuth>
                  <SessionsPage />
                </RequireAuth>
              }
            />
            <Route
              path="/remote/:sessionId"
              element={
                <RequireAuth>
                  <ScreenViewer />
                </RequireAuth>
              }
            />
            <Route
              path="/remote/:sessionId/recording"
              element={
                <RequireAuth>
                  <RecordingPlayer />
                </RequireAuth>
              }
            />
            <Route
              path="/patch"
              element={
                <RequireAuth>
                  <FleetPatchPage />
                </RequireAuth>
              }
            />
            <Route
              path="/patch/policies"
              element={
                <RequireAuth>
                  <PatchPolicyEditor />
                </RequireAuth>
              }
            />
            <Route
              path="/deploy"
              element={
                <RequireAuth>
                  <DeployPage />
                </RequireAuth>
              }
            />
            <Route
              path="/deploy/packages"
              element={
                <RequireAuth>
                  <PackagesPage />
                </RequireAuth>
              }
            />
            <Route
              path="/deploy/scripts"
              element={
                <RequireAuth>
                  <ScriptsPage />
                </RequireAuth>
              }
            />
            <Route
              path="/backup"
              element={
                <RequireAuth>
                  <BackupPointsPage />
                </RequireAuth>
              }
            />
            <Route
              path="/backup/repositories"
              element={
                <RequireAuth>
                  <RepositoriesPage />
                </RequireAuth>
              }
            />
            <Route
              path="/backup/policies"
              element={
                <RequireAuth>
                  <BackupPolicyEditor />
                </RequireAuth>
              }
            />
            <Route
              path="/backup/coverage"
              element={
                <RequireAuth>
                  <BackupCoveragePage />
                </RequireAuth>
              }
            />
            <Route
              path="/backup/offsite"
              element={
                <RequireAuth>
                  <OffsitePage />
                </RequireAuth>
              }
            />
            <Route
              path="/provisioning"
              element={
                <RequireAuth>
                  <ProvisioningRunsPage />
                </RequireAuth>
              }
            />
            <Route
              path="/provisioning/images"
              element={
                <RequireAuth>
                  <ImagesPage />
                </RequireAuth>
              }
            />
            <Route
              path="/provisioning/profiles"
              element={
                <RequireAuth>
                  <ProfilesPage />
                </RequireAuth>
              }
            />
            <Route
              path="/changes"
              element={
                <RequireAuth>
                  <ChangesPage />
                </RequireAuth>
              }
            />
            <Route
              path="/changes/approvals"
              element={
                <RequireAuth>
                  <ApprovalQueue />
                </RequireAuth>
              }
            />
            <Route
              path="/changes/calendar"
              element={
                <RequireAuth>
                  <ChangeCalendar />
                </RequireAuth>
              }
            />
            <Route
              path="/changes/:changeId"
              element={
                <RequireAuth>
                  <ChangeDetail />
                </RequireAuth>
              }
            />
            <Route
              path="/bcp"
              element={
                <RequireAuth>
                  <PlansPage />
                </RequireAuth>
              }
            />
            <Route
              path="/bcp/plans/:planId"
              element={
                <RequireAuth>
                  <PlanDetail />
                </RequireAuth>
              }
            />
            <Route
              path="/bcp/report"
              element={
                <RequireAuth>
                  <BcpReport />
                </RequireAuth>
              }
            />
            <Route
              path="/bcp/tests/:testId"
              element={
                <RequireAuth>
                  <TestRecorder />
                </RequireAuth>
              }
            />
            <Route
              path="/bcp/tests/:testId/report"
              element={
                <RequireAuth>
                  <BcpReport />
                </RequireAuth>
              }
            />
            <Route
              path="/licenses/vendors"
              element={
                <RequireAuth>
                  <VendorsPage />
                </RequireAuth>
              }
            />
            <Route
              path="/licenses/products"
              element={
                <RequireAuth>
                  <ProductsPage />
                </RequireAuth>
              }
            />
            <Route
              path="/licenses/compliance"
              element={
                <RequireAuth>
                  <ComplianceDashboard />
                </RequireAuth>
              }
            />
            <Route
              path="/licenses/review-queue"
              element={
                <RequireAuth>
                  <ReviewQueue />
                </RequireAuth>
              }
            />
            <Route
              path="/licenses/renewals"
              element={
                <RequireAuth>
                  <RenewalCalendar />
                </RequireAuth>
              }
            />
            <Route
              path="/licenses"
              element={
                <RequireAuth>
                  <LicensesPage />
                </RequireAuth>
              }
            />
            <Route
              path="/licenses/:licenseId"
              element={
                <RequireAuth>
                  <LicenseDetail />
                </RequireAuth>
              }
            />
            <Route
              path="/subscriptions/spend"
              element={
                <RequireAuth>
                  <SpendDashboard />
                </RequireAuth>
              }
            />
            <Route
              path="/subscriptions/renewals"
              element={
                <RequireAuth>
                  <SubscriptionRenewalCalendar />
                </RequireAuth>
              }
            />
            <Route
              path="/subscriptions"
              element={
                <RequireAuth>
                  <SubscriptionsPage />
                </RequireAuth>
              }
            />
            <Route
              path="/subscriptions/:subscriptionId"
              element={
                <RequireAuth>
                  <SubscriptionDetail />
                </RequireAuth>
              }
            />
            {/* People (docs/modules/people/spec.md). NOT A PAGE OF ITS OWN
                SINCE 2026-09-23: the operator found it confusing beside
                Settings -> Users, so each device carries who it is assigned
                to and the list redirects to Devices -- a bookmark still lands
                somewhere real. The person page stays, unlisted: licences,
                seats and devices link to a holder, and it is where what they
                hold is shown. */}
            <Route path="/people" element={<Navigate to="/devices" replace />} />
            <Route
              path="/people/unmatched"
              element={
                <RequireAuth>
                  <UnmatchedPage />
                </RequireAuth>
              }
            />
            <Route
              path="/people/:personId"
              element={
                <RequireAuth>
                  <PersonPage />
                </RequireAuth>
              }
            />
            <Route
              path="/budget"
              element={
                <RequireAuth>
                  <BudgetDashboard />
                </RequireAuth>
              }
            />
            <Route
              path="/budget/planner"
              element={
                <RequireAuth>
                  <BudgetPlannerPage />
                </RequireAuth>
              }
            />
            <Route
              path="/budget/expenses"
              element={
                <RequireAuth>
                  <ExpensesPage />
                </RequireAuth>
              }
            />
            <Route
              path="/budget/confirm"
              element={
                <RequireAuth>
                  <ConfirmationQueuePage />
                </RequireAuth>
              }
            />
            <Route
              path="/budget/admin"
              element={
                <RequireAuth>
                  <BudgetAdminPage />
                </RequireAuth>
              }
            />
            <Route
              path="/service-desk"
              element={
                <RequireAuth>
                  <TicketListPage />
                </RequireAuth>
              }
            />
            <Route
              path="/service-desk/my-work"
              element={
                <RequireAuth>
                  <MyWorkPage />
                </RequireAuth>
              }
            />
            <Route
              path="/service-desk/admin"
              element={
                <RequireAuth>
                  <ServiceDeskAdminPage />
                </RequireAuth>
              }
            />
            <Route
              path="/service-desk/tickets/:ticketId"
              element={
                <RequireAuth>
                  <TicketDetailPage />
                </RequireAuth>
              }
            />
            <Route
              path="/notifications"
              element={
                <RequireAuth>
                  <NotificationAdminPage />
                </RequireAuth>
              }
            />
            <Route
              path="/reports"
              element={
                <RequireAuth>
                  <ReportCataloguePage />
                </RequireAuth>
              }
            />
            <Route
              path="/reports/:reportKey"
              element={
                <RequireAuth>
                  <ReportViewPage />
                </RequireAuth>
              }
            />
            <Route
              path="/site-servers"
              element={
                <RequireAuth>
                  <SiteServersPage />
                </RequireAuth>
              }
            />
            <Route
              path="/site-servers/:siteServerId"
              element={
                <RequireAuth>
                  <SiteServerDetailPage />
                </RequireAuth>
              }
            />
            <Route
              path="/sla"
              element={
                <RequireAuth>
                  <SlaAdminPage />
                </RequireAuth>
              }
            />
            <Route
              path="/health"
              element={
                <RequireAuth>
                  <HealthPage />
                </RequireAuth>
              }
            />
            <Route
              path="/platform-status"
              element={
                <RequireAuth>
                  <PlatformStatusPage />
                </RequireAuth>
              }
            />
            <Route
              path="/error-monitor"
              element={
                <RequireAuth>
                  <ErrorMonitorPage />
                </RequireAuth>
              }
            />
            {/* The assistant. Two routes: the chat panel, which every `ai.use`
                holder reaches, and the org's settings, which is `ai.admin`. The
                panel renders a disabled explanation rather than an error when
                the feature is off (spec §10.6), so this route is always
                reachable and never a dead end. */}
            <Route
              path="/assistant"
              element={
                <RequireAuth>
                  <ChatPanel />
                </RequireAuth>
              }
            />
            <Route
              path="/assistant/settings"
              element={
                <RequireAuth>
                  <AiSettingsPage />
                </RequireAuth>
              }
            />
            {/* Act mode (docs/modules/ai-automation.md §12). The static
                `/assistant/autonomy` segment is declared BEFORE
                `/assistant/actions/:actionId` so it can never be captured as an
                action id. Every one of these routes is gated server-side; the
                console decides only what renders. */}
            <Route
              path="/assistant/autonomy"
              element={
                <RequireAuth>
                  <AutonomyDialPage />
                </RequireAuth>
              }
            />
            <Route
              path="/assistant/actions"
              element={
                <RequireAuth>
                  <AiActionsPage />
                </RequireAuth>
              }
            />
            <Route
              path="/assistant/actions/:actionId"
              element={
                <RequireAuth>
                  <AiActionPage />
                </RequireAuth>
              }
            />
            {/* Agent push install, cycle B1 (`CLAUDE.md` invariant 2a). Two
                control-surface screens and no dispatch: nothing reachable from
                here pushes anything, because the job type and the agent
                capability are cycle B2. `/agent-push` redirects rather than
                rendering a third page -- an index that duplicated one of the
                two would give the same control two homes. */}
            <Route
              path="/agent-push"
              element={<Navigate to="/agent-push/authorizations" replace />}
            />
            <Route
              path="/agent-push/authorizations"
              element={
                <RequireAuth>
                  <PushAuthorizationsPage />
                </RequireAuth>
              }
            />
            <Route
              path="/agent-push/credential-scopes"
              element={
                <RequireAuth>
                  <PushCredentialScopesPage />
                </RequireAuth>
              }
            />
            {/* IPAM. The static segments are declared BEFORE /ipam/subnets/:id
                so "drift", "search", "registry", "scan-authorizations" and
"import" can never be captured as a subnet id. */}
            <Route
              path="/ipam"
              element={
                <RequireAuth>
                  <SubnetTreePage />
                </RequireAuth>
              }
            />
            <Route
              path="/ipam/drift"
              element={
                <RequireAuth>
                  <DriftPage />
                </RequireAuth>
              }
            />
            <Route
              path="/ipam/search"
              element={
                <RequireAuth>
                  <IpamSearchPage />
                </RequireAuth>
              }
            />
            <Route
              path="/ipam/registry"
              element={
                <RequireAuth>
                  <IpamRegistryPage />
                </RequireAuth>
              }
            />
            <Route
              path="/ipam/scan-authorizations"
              element={
                <RequireAuth>
                  <ScanAuthorizationsPage />
                </RequireAuth>
              }
            />
            <Route
              path="/ipam/import"
              element={
                <RequireAuth>
                  <IpamImportPage />
                </RequireAuth>
              }
            />
            <Route
              path="/ipam/subnets/:subnetId"
              element={
                <RequireAuth>
                  <SubnetDetailPage />
                </RequireAuth>
              }
            />
            <Route
              path="/email-intake"
              element={
                <RequireAuth>
                  <EmailIntakeAdminPage />
                </RequireAuth>
              }
            />
            {/* Where Microsoft returns after "Connect with Microsoft". Behind
                RequireAuth: completing it is the operator's own authenticated
                call (mail-oauth design §4.3). */}
            <Route
              path="/settings/email-intake/oauth/callback"
              element={
                <RequireAuth>
                  <EmailIntakeOAuthCallback />
                </RequireAuth>
              }
            />
            <Route
              path="/portal"
              element={
                <RequireAuth>
                  <MyTicketsPage />
                </RequireAuth>
              }
            />
            <Route
              path="/portal/new"
              element={
                <RequireAuth>
                  <NewTicketPage />
                </RequireAuth>
              }
            />
            <Route
              path="/portal/tickets/:ticketId"
              element={
                <RequireAuth>
                  <PortalTicketDetailPage />
                </RequireAuth>
              }
            />
            {/* The four screens the design had and the application did not
                (UI port spec §7). Each is a real page over real endpoints;
                where an endpoint does not exist the panel is absent rather
                than invented, and each file says which and why. */}
            <Route
              path="/dashboard"
              element={
                <RequireAuth>
                  <DashboardPage />
                </RequireAuth>
              }
            />
            <Route
              path="/audit"
              element={
                <RequireAuth>
                  <AuditPage />
                </RequireAuth>
              }
            />
            <Route
              path="/enrollment"
              element={
                <RequireAuth>
                  <EnrollmentPage />
                </RequireAuth>
              }
            />
            <Route
              path="/device-groups"
              element={
                <RequireAuth>
                  <GroupsPage />
                </RequireAuth>
              }
            />
            <Route
              path="/discovered"
              element={
                <RequireAuth>
                  <CandidatesPage />
                </RequireAuth>
              }
            />
            <Route
              path="/agent-packages"
              element={
                <RequireAuth>
                  <AgentReleasesPage />
                </RequireAuth>
              }
            />
            <Route
              path="/settings"
              element={
                <RequireAuth>
                  <SettingsPage />
                </RequireAuth>
              }
            />
            {/* The three areas' landing pages: the rail's second layer. */}
            <Route
              path="/admin"
              element={
                <RequireAuth>
                  <AreaPage to="/admin" />
                </RequireAuth>
              }
            />
            {/* A landing page until 2026-09-23, when its pages moved into the
                rail itself. Kept as a redirect so a bookmark still lands. */}
            <Route path="/governance" element={<Navigate to="/docs" replace />} />
            {/* A landing page until 2026-09-23, when its pages moved into the
                rail itself. Kept as a redirect so a bookmark still lands. */}
            <Route path="/assets" element={<Navigate to="/licenses" replace />} />
            {/* YOUR OWN RECORD, AND A ROUTE RATHER THAN A TAB. The credential
                steps this page hosts leave for the sign-in pages and return to
                a URL; a Settings tab lives in React state and is not in the
                URL, so every one of them used to land back on Settings'
                default tab. Gated on authentication and on nothing else --
                `PATCH /me` takes no user id, and a permission that could
                withhold a person's own display name from them would have no
                purpose. */}
            <Route
              path="/profile"
              element={
                <RequireAuth>
                  <ProfilePage />
                </RequireAuth>
              }
            />
            <Route
              path="*"
              element={
                <RequireAuth>
                  <Landing />
                </RequireAuth>
              }
            />
          </Routes>
          </SetupGate>
        </BrowserRouter>
        </LanguageBridge>
      </ApiProvider>
    </QueryClientProvider>
  );
}
