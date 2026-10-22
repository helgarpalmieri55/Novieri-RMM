/**
 * The sidebar's navigation model, in two layers.
 *
 * THE DAILY WORK FIRST: about ten destinations a technician uses every day.
 * THEN THE BUSINESS MODULES, SHOWN: Governance and Assets & Spend, four pages
 * each, as ordinary full-size items under their own headings. THEN ONE AREA,
 * Admin: twenty-odd configuration pages behind one rail entry whose route is a
 * landing page, opening in place (with its section headings) while the
 * operator is inside it. The review of 2026-09-23 (reviewer 2, §1) counted 36
 * peers in seven groups, with configuration spread across four of them and
 * Settings below the fold.
 *
 * WHY ONLY ADMIN IS FOLDED. The first two-layer version (2026-09-23) folded
 * Governance and Assets & Spend too, and the operator's verdict the same day
 * was that their pages "look so small now and without importance". They are
 * modules this product is sold on, visited by the people who own them, and
 * eight items do not need hiding. Configuration does: it is visited rarely
 * and by few.
 *
 * NAMES ARE THE TASK'S, NOT THE MACHINERY'S: "Job history", not "Signed Job
 * Engine"; "Audit log", not "Immutable Audit"; "Service levels", not "SLA
 * Engine". The AI feature has one name, "Assistant".
 *
 * WHAT IS ADDED HERE IS AUTHORIZATION, AND IT IS COSMETIC. Every item carries
 * the permission code its route already enforces SERVER-SIDE, so a technician
 * is not offered a link that answers 403. Hiding is a courtesy, never a
 * control: invariant 6 says the frontend never gates security.
 *
 * `permission: null` marks a destination every authenticated operator may
 * reach, and means landing on it issues no request that can be refused.
 * `nav.test.ts` holds the ungated set to an exact list.
 */

import { translate, type MessageKey } from "../i18n/core";

/**
 * LABELS ARE LOOKED UP WHEN READ, NOT WHEN THIS MODULE LOADS. The model below
 * is a module-level constant, so a plain string would freeze the language the
 * page started in. Each label is an accessor over the catalogue instead: the
 * English is read back unchanged, and a language switch shows in the rail, the
 * crumb, the tab title and the palette on their next render. Anything that
 * COPIES an entry (`{ ...entry }`) reads it at that moment, which is what
 * `visibleGroups` wants -- it runs on every render of the components that call
 * `useT()`.
 */
function named<T extends object, K extends string>(base: T, texts: Readonly<Record<K, MessageKey>>): T & Readonly<Record<K, string>> {
  const out = { ...base } as T & Record<K, string>;
  for (const [field, key] of Object.entries<MessageKey>(texts)) {
    Object.defineProperty(out, field, { enumerable: true, get: () => translate(key) });
  }
  return out;
}

/** One page of the model: a destination, its permission, and its words. */
function page(
  to: string,
  label: MessageKey,
  icon: string,
  permission: string | null,
  extra: { badgeTone?: "gold" | "red"; hint?: MessageKey } = {},
): NavItem {
  const { hint, ...rest } = extra;
  const texts: Record<string, MessageKey> = { label };
  if (hint) texts.hint = hint;
  return named({ to, icon, permission, ...rest }, texts) as unknown as NavItem;
}

function group(label: MessageKey, items: readonly RailEntry[]): NavGroup {
  return named({ items }, { label });
}

function section(label: MessageKey, items: readonly NavItem[]): NavSection {
  return named({ items }, { label });
}

export interface NavItem {
  /** Route path, matching a `<Route>` in `App.tsx`. */
  readonly to: string;
  /** The task's name, not the machinery's. */
  readonly label: string;
  /** Icon key in `shell/icons.tsx`. */
  readonly icon: string;
  /**
   * Permission code gating VISIBILITY only. `null` = visible to every
   * authenticated operator, stated deliberately.
   */
  readonly permission: string | null;
  /** Badge tone, when this item carries a live count. */
  readonly badgeTone?: "gold" | "red";
  /** One line on an area's landing page: what the page is for. */
  readonly hint?: string;
}

/** A titled run of pages inside an area. */
export interface NavSection {
  readonly label: string;
  readonly items: readonly NavItem[];
}

/**
 * One rail entry standing for many pages. It has no permission of its own:
 * it is visible exactly when one of its pages is, so it never leads to an
 * empty list.
 */
export interface NavArea {
  readonly to: string;
  readonly label: string;
  readonly icon: string;
  /** The landing page's subtitle. */
  readonly description: string;
  readonly sections: readonly NavSection[];
}

export type RailEntry = NavItem | NavArea;

export interface NavGroup {
  readonly label: string;
  readonly items: readonly RailEntry[];
}

export function isArea(entry: RailEntry): entry is NavArea {
  return "sections" in entry;
}

export const NAV_GROUPS: readonly NavGroup[] = [
  group("shell.nav.group.daily", [
    page("/dashboard", "shell.nav.item.dashboard", "dashboard", null),
    page("/devices", "shell.nav.item.devices", "devices", "devices.read"),
    page("/health", "shell.nav.item.alerts", "health", "health.read", { badgeTone: "gold" }),
    page("/service-desk", "shell.nav.item.tickets", "ticket", "ticket.read"),
    page("/patch", "shell.nav.item.patching", "patch", "patch.read"),
    page("/deploy", "shell.nav.item.software", "package", "deploy.read"),
    page("/backup", "shell.nav.item.backup", "backup", "backup.read"),
    page("/remote", "shell.nav.item.remoteSessions", "remote", "remote.control"),
    page("/assistant", "shell.nav.item.assistant", "sparkles", "ai.use"),
  ]),
  group("shell.nav.group.governance", [
    page("/docs", "shell.nav.item.itDocumentation", "book", "docs.read", { hint: "shell.nav.hint.docs" }),
    page("/changes", "shell.nav.item.changes", "changes", "change.read", { hint: "shell.nav.hint.changes" }),
    page("/bcp", "shell.nav.item.continuityTests", "bcp", "bcp.read", { hint: "shell.nav.hint.bcp" }),
    page("/reports", "shell.nav.item.reports", "chart", "reporting.read", { hint: "shell.nav.hint.reports" }),
  ]),
  group("shell.nav.group.assetsSpend", [
    page("/licenses", "shell.nav.item.licenses", "license", "licenses.read", { hint: "shell.nav.hint.licenses" }),
    page("/subscriptions", "shell.nav.item.subscriptions", "subscription", "subscriptions.read", { hint: "shell.nav.hint.subscriptions" }),
    page("/budget", "shell.nav.item.itBudget", "budget", "budget.read", { hint: "shell.nav.hint.budget" }),
    page("/ipam", "shell.nav.item.ipAddresses", "network", "ipam.read", { hint: "shell.nav.hint.ipam" }),
  ]),
  group("shell.nav.group.configure", [
    named(
      {
        to: "/admin",
        icon: "adjustments",
        sections: [
          section("shell.nav.section.serviceDesk", [
            page("/notifications", "shell.nav.item.notifications", "bell", "notification.read", { hint: "shell.nav.hint.notifications" }),
            page("/email-intake", "shell.nav.item.emailIntake", "mail", "email_intake.read", { hint: "shell.nav.hint.emailIntake" }),
            page("/sla", "shell.nav.item.serviceLevels", "clock", "sla.read", { hint: "shell.nav.hint.sla" }),
            page("/portal", "shell.nav.item.endUserPortal", "portal", "portal.ticket.read", { hint: "shell.nav.hint.portal" }),
          ]),
          section("shell.nav.section.computers", [
            page("/enrollment", "shell.nav.item.enrollment", "certificate", "devices.read", { hint: "shell.nav.hint.enrollment" }),
            page("/agent-packages", "shell.nav.item.agentPackages", "package", "agent.release.read", { hint: "shell.nav.hint.agentPackages" }),
            // The scope code, not `agent.push.run`: both screens behind this
            // entry require it server-side.
            page("/agent-push/authorizations", "shell.nav.item.pushInstall", "shield", "agent.push.scope.manage", { hint: "shell.nav.hint.pushInstall" }),
            page("/device-groups", "shell.nav.item.deviceGroups", "building", "devices.read", { hint: "shell.nav.hint.deviceGroups" }),
            page("/site-servers", "shell.nav.item.siteServers", "server", "site.read", { hint: "shell.nav.hint.siteServers" }),
            page("/provisioning", "shell.nav.item.imaging", "image", "provisioning.read", { hint: "shell.nav.hint.imaging" }),
          ]),
          section("shell.nav.section.automation", [
            page("/assistant/autonomy", "shell.nav.item.aiAutomation", "automation", "ai.admin", { hint: "shell.nav.hint.aiAutomation" }),
            page("/jobs", "shell.nav.item.jobHistory", "jobs", "jobs.read", { hint: "shell.nav.hint.jobHistory" }),
          ]),
          section("shell.nav.section.security", [
            page("/credentials", "shell.nav.item.credentials", "lock", "credentials.read", { hint: "shell.nav.hint.credentials" }),
            page("/audit", "shell.nav.item.auditLog", "audit", "audit.read", { hint: "shell.nav.hint.auditLog" }),
            page("/security", "shell.nav.item.securityLogs", "logs", "audit.read", { hint: "shell.nav.hint.securityLogs" }),
          ]),
          section("shell.nav.section.platform", [
            // Not `null`: the page calls `GET /platform/status` on mount,
            // which requires this code.
            page("/platform-status", "shell.nav.item.platformStatus", "status", "platform.status.read", { hint: "shell.nav.hint.platformStatus" }),
            page("/error-monitor", "shell.nav.item.errorMonitor", "warning", "platform.errors.read", { hint: "shell.nav.hint.errorMonitor" }),
            page("/settings", "shell.nav.item.settings", "settings", "orgs.manage", { hint: "shell.nav.hint.settings" }),
          ]),
        ],
      },
      { label: "shell.nav.item.admin", description: "shell.nav.adminDescription" },
    ),
  ]),
];

/** Every page item in these groups: rail items and every area's pages. */
export function navItems(groups: readonly NavGroup[] = NAV_GROUPS): NavItem[] {
  return groups.flatMap((group) =>
    group.items.flatMap((entry) =>
      isArea(entry) ? entry.sections.flatMap((section) => section.items) : [entry],
    ),
  );
}

/** Every area in these groups. */
export function navAreas(groups: readonly NavGroup[] = NAV_GROUPS): NavArea[] {
  return groups.flatMap((group) => group.items.filter(isArea));
}

/**
 * ROUTES THAT ARE REAL BUT LISTED NOWHERE, and the rail entry each sits under.
 *
 * Reached from a link on another page, so no item matches them; without this
 * the crumb fell back to nothing and no rail entry was lit. `under` is a rail
 * entry's `to`, and `nav.test.ts` holds each one to an entry that exists.
 */
export interface NavUnlisted {
  readonly to: string;
  readonly label: string;
  /** The `to` of the rail entry (item or area) this page belongs to. */
  readonly under: string;
}

function unlisted(to: string, label: MessageKey, under: string): NavUnlisted {
  return named({ to, under }, { label });
}

export const NAV_UNLISTED: readonly NavUnlisted[] = [
  // The single "Add a computer" entry: the top bar and the Devices head both
  // lead here, and it links on to packages, enrollment and push install.
  unlisted("/devices/onboard", "shell.nav.page.addComputer", "/devices"),
  // Subnet push runs (design 2026-09-30 §1.7): reached from Devices.
  unlisted("/devices/push-runs", "shell.nav.page.pushRuns", "/devices"),
  unlisted("/software", "shell.nav.page.installedSoftware", "/deploy"),
  unlisted("/discovered", "shell.nav.page.discovered", "/devices"),
  unlisted("/agent-push", "shell.nav.item.pushInstall", "/admin"),
  // PEOPLE LEFT THE RAIL ON 2026-09-23: the operator read it as a second
  // Settings -> Users, and a device's holder is now set on the device. The
  // person page stays -- licences, seats and a device's "Assigned to" link to
  // it -- and it sits under Devices, which is where a holder is chosen.
  // `/people` itself redirects to `/devices`.
  unlisted("/people", "shell.nav.page.person", "/devices"),
  unlisted("/people/unmatched", "shell.nav.page.needsPerson", "/devices"),
];

/** Where "Add a computer" goes, from every surface that offers it. */
export const ADD_COMPUTER_TO = "/devices/onboard";

/**
 * A ROUTE THAT BELONGS TO NO SECTION AT ALL, and is not a defect: `/profile`
 * is reached from the account menu, and there is no page behind "Your
 * account" to link to.
 */
export const ORPHAN_SECTION: Readonly<Record<string, string>> = named({}, { "/profile": "shell.nav.yourAccount" });

/** What the shell's breadcrumb and the tab title are made of, for one route. */
export interface Crumb {
  /** The section segment's text; `undefined` for a route in no section. */
  readonly section?: string;
  /** Where that segment links; `undefined` renders a plain span. */
  readonly sectionTo?: string;
  /** The page's own name, and what goes before "· ITMS" in the tab title. */
  readonly title: string;
}

interface Destination {
  readonly to: string;
  readonly label: string;
  /** The rail entry lit while this page is open. */
  readonly owner: RailEntry;
}

/** Every route the model can name, with the rail entry that owns it. */
function destinations(groups: readonly NavGroup[]): Destination[] {
  const entries = groups.flatMap((group) => group.items);
  const own: Destination[] = entries.flatMap((entry): Destination[] =>
    isArea(entry)
      ? [
          { to: entry.to, label: entry.label, owner: entry },
          ...entry.sections.flatMap((section) =>
            section.items.map((item) => ({ to: item.to, label: item.label, owner: entry })),
          ),
        ]
      : [{ to: entry.to, label: entry.label, owner: entry }],
  );
  const unlisted = NAV_UNLISTED.flatMap((page) => {
    const owner = entries.find((entry) => entry.to === page.under);
    return owner ? [{ to: page.to, label: page.label, owner }] : [];
  });
  return [...own, ...unlisted];
}

/** The longest `to` that is the path or a parent of it. */
function match(pathname: string, groups: readonly NavGroup[]): Destination | undefined {
  return destinations(groups)
    .filter(({ to }) => pathname === to || pathname.startsWith(`${to}/`))
    .sort((a, b) => b.to.length - a.to.length)[0];
}

/**
 * ONE ROUTE -> ONE CRUMB. The section is the rail entry the page lives under,
 * linked to that entry's route, so "ITMS > Admin" goes to the Admin landing
 * page and "ITMS > Devices" to the fleet list. The longest match wins, so
 * `/assistant/autonomy` is AI automation (Admin), not the Assistant.
 */
export function crumbFor(pathname: string): Crumb {
  const found = match(pathname, NAV_GROUPS);
  if (!found) return { section: ORPHAN_SECTION[pathname], title: "ITMS" };
  return { section: found.owner.label, sectionTo: found.owner.to, title: found.label };
}

/**
 * The groups an operator may actually see. An area keeps only the pages the
 * operator may open; an empty section, area or group is dropped entirely,
 * because a heading over nothing reads as a broken render.
 */
export function visibleGroups(
  permissions: readonly string[],
  groups: readonly NavGroup[] = NAV_GROUPS,
): NavGroup[] {
  // A requester is offered the portal's rail, never the operator groups.
  const source = groups === NAV_GROUPS && isPortalOnly(permissions) ? PORTAL_GROUPS : groups;
  const held = new Set(permissions);
  const allowed = (item: NavItem) => item.permission === null || held.has(item.permission);
  return source
    .map((group) => ({
      label: group.label,
      items: group.items.flatMap((entry): RailEntry[] => {
        if (!isArea(entry)) return allowed(entry) ? [entry] : [];
        const sections = entry.sections
          .map((section) => ({ label: section.label, items: section.items.filter(allowed) }))
          .filter((section) => section.items.length > 0);
        return sections.length > 0 ? [{ ...entry, sections }] : [];
      }),
    }))
    .filter((group) => group.items.length > 0);
}

/** A page the search field can offer: every visible destination, flat. */
export interface SearchDestination {
  readonly to: string;
  readonly label: string;
  readonly icon: string;
  /** Where it sits, as the operator sees it in the rail. */
  readonly within: string;
}

export function searchDestinations(permissions: readonly string[]): SearchDestination[] {
  return visibleGroups(permissions).flatMap((group) =>
    group.items.flatMap((entry): SearchDestination[] =>
      isArea(entry)
        ? [
            { to: entry.to, label: entry.label, icon: entry.icon, within: group.label },
            ...entry.sections.flatMap((section) =>
              section.items.map((item) => ({
                to: item.to,
                label: item.label,
                icon: item.icon,
                within: entry.label,
              })),
            ),
          ]
        : [{ to: entry.to, label: entry.label, icon: entry.icon, within: group.label }],
    ),
  );
}

/**
 * AN ACCOUNT THAT HOLDS ONLY `portal.*` CODES IS A REQUESTER, not an operator
 * with a short rail. Such an account gets the portal's own destinations
 * instead of the operator groups; holding any operator code at all keeps the
 * operator console.
 */
export function isPortalOnly(permissions: readonly string[]): boolean {
  return permissions.length > 0 && permissions.every((code) => code.startsWith("portal."));
}

/** The requester's whole rail. Gated like the portal's own pages. */
export const PORTAL_GROUPS: readonly NavGroup[] = [
  group("shell.nav.group.help", [
    page("/portal", "shell.nav.item.myTickets", "ticket", "portal.ticket.read"),
    page("/portal/new", "shell.nav.item.newTicket", "plus", "portal.ticket.write"),
  ]),
];

/**
 * The requester's crumb: the page's own name and no section. The operator
 * crumb for /portal links to Admin -- a page a requester cannot open.
 */
export function portalCrumbFor(pathname: string): Crumb {
  const to = activeNavTo(pathname, PORTAL_GROUPS);
  const item = navItems(PORTAL_GROUPS).find((entry) => entry.to === to);
  return { title: item?.label ?? translate("shell.nav.group.help") };
}

/**
 * The one rail entry the operator is on, among the groups given (normally the
 * visible ones): the owner of the longest matching destination. A page whose
 * own item is hidden falls back to the nearest visible parent, so
 * `/assistant/autonomy` without `ai.admin` lights the Assistant.
 */
export function activeNavTo(pathname: string, groups: readonly NavGroup[]): string | undefined {
  return match(pathname, groups)?.owner.to;
}
