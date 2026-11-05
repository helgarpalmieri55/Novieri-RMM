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
import { ClientScopeProvider, useEnabledModules } from "./lib/clientScope";
import { ClientSelector, useClientMark } from "./shell/ClientSelector";
import { I18nProvider, readStoredLanguage, useLanguage, useT } from "./i18n/context";
import { setActiveLanguage, subscribe, translate } from "./i18n/core";
import { resolveLanguage } from "./i18n/resolve";
import type { Language } from "./i18n/types";
import { Toaster, type ToastItem } from "./shell/Toaster";
import { FIXTURE_MODE } from "./fixtures/mode";
import { PreviewPage } from "./preview/PreviewPage";
import { goToSignIn, hasSession, signOut, useSession } from "./lib/session";
import { describeSubmitError } from "./components/form";
import { KIND_ICON } from "./shell/components";
import { Icon } from "./shell/icons";
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
// Your own sign-in security (`/profile/security`), not the fleet's security
// posture above (`modules/security`): two pages with the same component name.
import { SecurityPage as YourSecurityPage } from "./modules/profile/SecurityPage";
import { SettingsPage } from "./modules/settings/SettingsPage";
import { ClientDetailPage } from "./modules/clients/ClientDetailPage";
import { ClientsPage } from "./modules/clients/ClientsPage";
import { SetupPage, fetchSetupStatus } from "./modules/setup/SetupPage";
import { EnrollPage } from "./modules/auth/EnrollPage";
import { FactorPage } from "./modules/auth/FactorPage";
import { ForgotPasswordPage } from "./modules/auth/ForgotPasswordPage";
import { InvitePage } from "./modules/auth/InvitePage";
import { RecoverPage } from "./modules/auth/RecoverPage";
import { RecoveryCodesPage } from "./modules/auth/RecoveryCodesPage";
import { ResetPasswordPage } from "./modules/auth/ResetPasswordPage";
import { SignInPage } from "./modules/auth/SignInPage";
import { MyTicketsPage } from "./modules/portal/MyTicketsPage";
import { NewTicketPage } from "./modules/portal/NewTicketPage";
import { PortalTicketDetailPage } from "./modules/portal/PortalTicketDetailPage";

// Exported so a test that renders `<App />` more than once can reset it. The
// client is module-level — one per browser tab, which is right in production
// and wrong in a test file, where a cached answer from the previous case
// (`setup/status`, held with `staleTime: Infinity`) would satisfy the next one
// without a request ever being made.
//
// A change of the top bar's client empties it (`emptyForNewClient`, in
// `lib/clientScope.tsx`'s provider, which reaches this same client through
// `useQueryClient`): nothing read for one client is shown under another.
// `SetupGate`'s query is the one kept: it names no client.
export const queryClient = new QueryClient({
  // No retry in fixture mode: a `FixtureMissing` rejects the same way on the
  // second attempt, and the harmony walk should reach the page's error state
  // in one step rather than after a back-off.
  defaultOptions: { queries: { retry: FIXTURE_MODE ? false : 1, staleTime: 5000 } },
});

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
  const modules = useEnabledModules();
  // The client chosen, for the phone's drawer toggle (operator decision 4).
  const clientMark = useClientMark();
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

  // THE SESSION NOTICE (own-auth design §2.4). A 401 after `/me` answered is
  // a session that ended while the person was here -- the 30 minutes of
  // inactivity, the 12-hour maximum, an administrator. The page stays drawn
  // and the shell says so; the person copies what they were writing and
  // chooses when to sign in again (auditor A, HIGH: "the session drops
  // mid-task ... total loss of context").
  const session = useSession();
  // A SIGN-OUT THE SERVER NEVER HEARD IS SAID OUT LOUD. Shown the sign-in page
  // while the session stays open, a person on a shared computer walks away
  // from it (`lib/session.ts::signOut`).
  const [signOutFailed, setSignOutFailed] = useState(false);

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
      ...searchDestinations(permissions, modules).map((page) => ({
        id: page.to,
        label: page.label,
        sub: page.within,
        type: t("shell.app.pageType"),
        icon: page.icon,
        onSelect: () => navigate(page.to),
      })),
    ],
    [records.hits, permissions, modules, navigate, language, t],
  );
  const portalOnly = isPortalOnly(permissions);
  const prompt = searchPrompt(permissions, portalOnly);
  // A requester's crumb names the page and no operator section.
  const shownCrumb = portalOnly ? portalCrumbFor(location.pathname) : crumb;

  // A REQUESTER ON AN OPERATOR PAGE IS SENT TO THE PORTAL. Every such page
  // would answer them with permission errors; the server refuses the data
  // either way, so this is a courtesy, not a control (invariant 6). Their own
  // profile and their own security are every person's.
  if (
    portalOnly &&
    !location.pathname.startsWith("/portal") &&
    location.pathname !== "/profile" &&
    location.pathname !== "/profile/security"
  ) {
    return <Navigate to="/portal" replace />;
  }

  return (
    <AppShell
      permissions={permissions}
      modules={modules}
      clientSelector={<ClientSelector />}
      clientMark={clientMark}
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
      onSignOut={() => {
        setSignOutFailed(false);
        signOut(api).catch(() => setSignOutFailed(true));
      }}
      sessionExpiring={session.status === "signed_out"}
      onSignIn={() => goToSignIn(location.pathname + location.search)}
    >
      {signOutFailed ? (
        <p role="alert" className="notice red">
          <Icon name={KIND_ICON.crit} size={16} />
          <span>{t("shell.app.signOutFailed")}</span>
        </p>
      ) : null}
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
 * The guard (own-auth design §1.1, §6.5). A tab with no session goes to the
 * console's own sign-in page, carrying the page it asked for as `?next=`.
 *
 * WHAT DECIDES IS `GET /me` (`lib/session.ts::useSession`). An answer renders
 * the page. A 401 in a tab that never had a session sends it to sign in --
 * once: `leaving` is a ref because the navigation replaces the document, and
 * React strict mode runs this effect twice on purpose. A 401 AFTER the person
 * was here keeps the shell and the page drawn and raises the shell's notice
 * instead (`Shell`). A failure that is not a 401 -- the server down, a proxy's
 * 502 -- is said, inside an alert, with its reason: this screen replaces a
 * page the person expected, and a redirect to sign in would blame their
 * session for the server's outage.
 *
 * FIXTURE MODE NEEDS NO BRANCH HERE. Its client answers `/me`
 * (`fixtures/core.ts`), so the review harness renders every route signed in;
 * a real API would still answer every request it makes with 401, because the
 * console gates nothing (invariant 6).
 */
export function RequireAuth({ children }: { children: React.ReactNode }) {
  const t = useT();
  const location = useLocation();
  const session = useSession();
  const leaving = useRef(false);
  const mustSignIn = session.status === "signed_out" && session.me === undefined;
  const here = location.pathname + location.search;

  useEffect(() => {
    if (!mustSignIn || leaving.current) return;
    leaving.current = true;
    goToSignIn(here);
  }, [mustSignIn, here]);

  // INSIDE THE GUARD, AROUND THE SHELL: the client selection needs `/me`, which
  // only a signed-in session can ask, and a change of client starts the shell
  // and the page again (`lib/clientScope.tsx`).
  if (session.me !== undefined) {
    return (
      <ClientScopeProvider>
        <Shell>{children}</Shell>
      </ClientScopeProvider>
    );
  }
  if (session.failure !== undefined) {
    return (
      <div className="flex min-h-screen items-center justify-center p-8">
        <div
          className="w-full max-w-md rounded-lg border border-[var(--line)] p-8 shadow-sm"
          role="alert"
        >
          <h1 className="mb-2 text-lg font-semibold">{t("shell.app.signInUnavailable")}</h1>
          <p className="text-sm text-[var(--muted)]">
            {t("shell.app.serverUnreachable", { reason: describeSubmitError(session.failure) })}
          </p>
        </div>
      </div>
    );
  }
  if (mustSignIn) return <p className="p-8 text-[var(--muted)]">{t("shell.app.redirecting")}</p>;
  return <p className="p-8 text-[var(--muted)]">{t("shell.ui.loading")}</p>;
}

/**
 * BEFORE THE AUTH GATE, AND THAT ORDER IS THE WHOLE POINT.
 *
 * `RequireAuth` sends a visitor with no session straight to the sign-in
 * page. On an installation that has never been set up there is nobody to
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
      // ONLY WITH A SESSION (plan decision D20). The bridge sits outside
      // `RequireAuth`, so it is also on the sign-in pages, where there is no
      // account to save to: the request would only answer 401. The choice is
      // kept in this browser either way (`itms.lang`), and the next sign-in
      // opens in it.
      if (!hasSession(queryClient)) return;
      api
        .updateMe({ locale: language })
        .then(() =>
          // The cached `/me` carries the saved locale; left stale, the profile
          // page would seed its form from the old value and an unrelated save
          // could flip the language back. The sign-in pages are this console's
          // own (own authentication phase 1): there is no second place the
          // language must reach, and nothing more to tell the person.
          queryClient.invalidateQueries({ queryKey: ["me"] }),
        )
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

/**
 * NOTHING IS AWAITED BEFORE THE FIRST PAINT. `lib/auth.ts`'s `initAuth()` read
 * the OIDC user out of `sessionStorage` before anything rendered; the session
 * is a cookie now, and the one question about it is `/me`, which `RequireAuth`
 * asks behind a loading line. So a run with no backend at all -- the
 * accessibility pass over `/__preview` (`scripts/a11y-browser.mjs`) -- and
 * fixture mode render at once, and no network call can hold the page blank.
 */
export default function App() {
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
            {/* THE PAGES REACHED WITHOUT A SESSION (own-auth Task 11), and NOT
                inside RequireAuth: there, a person with no session would be
                sent to sign in from the page where they sign in. They are
                `lib/session.ts::PUBLIC_PATHS`; `modules/auth/__tests__/
                routes.test.ts` holds the two lists, and the harmony walk,
                together. The emailed links carry their token in the
                fragment (`/invite#<token>`), never in the path. Still inside
                SetupGate: an installation with no owner shows its setup
                screen at these addresses too. */}
            <Route path="/sign-in" element={<SignInPage />} />
            <Route path="/sign-in/factor" element={<FactorPage />} />
            <Route path="/sign-in/enroll" element={<EnrollPage />} />
            <Route path="/sign-in/recovery-codes" element={<RecoveryCodesPage />} />
            <Route path="/forgot-password" element={<ForgotPasswordPage />} />
            <Route path="/reset-password" element={<ResetPasswordPage />} />
            <Route path="/invite" element={<InvitePage />} />
            <Route path="/recover" element={<RecoverPage />} />
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
            {/* Clients (MSP clients phase 3, design §4.1). */}
            <Route
              path="/clients"
              element={
                <RequireAuth>
                  <ClientsPage />
                </RequireAuth>
              }
            />
            <Route
              path="/clients/:clientId"
              element={
                <RequireAuth>
                  <ClientDetailPage />
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
            {/* YOUR OWN RECORD, AND A ROUTE RATHER THAN A TAB: it is not
                administration. Gated on authentication and on nothing else --
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
            {/* YOUR SECURITY: password, passkeys, the authenticator app,
                recovery codes, remembered devices and sessions (own
                authentication design §2). Gated like `/profile`, for the same
                reason: every route behind it answers for the session's own
                person. */}
            <Route
              path="/profile/security"
              element={
                <RequireAuth>
                  <YourSecurityPage />
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
