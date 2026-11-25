/**
 * Settings — every configuration this platform serves, EDITED here.
 *
 * The operator's requirement is explicit: "Everything under the Settings menu
 * must be editable — all configurations will take place there and not directly
 * on the server files." The previous version of this file was a read-only
 * summary that linked out to five other editors; this one is twelve forms.
 *
 * WHAT EACH TAB WRITES, AND THE ROUTE BEHIND IT:
 *
 * | Tab            | Read                          | Write                              | Permission (read / write)        |
 * |----------------|-------------------------------|------------------------------------|----------------------------------|
 * | Organization   | `GET /orgs/current`           | `PATCH /orgs/current`              | — / `orgs.manage`                |
 * | Email          | `GET /settings/email`         | `PUT /settings/email`, `POST /settings/email/test` | `orgs.manage` / `orgs.manage` |
 * | Roles          | `GET /roles`, `/permissions`  | `POST`/`PATCH`/`DELETE /roles`, `PUT /roles/{id}/permissions` | `roles.read` / `roles.manage` |
 * | Users          | `GET /users`, `/users/{id}/effective-permissions` | `POST /users/invite`, role assign/revoke, disable/enable, resend/cancel an invitation, reset second factors, sign out everywhere, `DELETE` | `users.read` / `users.manage` |
 * | Sessions       | `GET /settings/sessions`      | `PUT /settings/sessions`           | `orgs.manage` / `orgs.manage`    |
 * | Microsoft sign-in | `GET /settings/microsoft-sign-in` | `PUT /settings/microsoft-sign-in` | `orgs.manage` / `orgs.manage` |
 * | Assistant      | `GET /ai/settings`, `/ai/providers` | `PUT /ai/settings`           | `ai.admin` / `ai.admin`          |
 * | AI connections | `GET /orgs/current`, `GET /mcp/connections` | `PATCH /orgs/current` (`mcp_enabled` only), `POST /mcp/connections/{app_id}/revoke` | `mcp.use` OR `orgs.manage` / switch and "for everyone" `orgs.manage`, "for me" `mcp.use` |
 * | Notifications  | `GET /notifications/channels`, `/rules` | `POST`/`PATCH` both, `POST .../test` | `notification.read` / `notification.admin` |
 * | Service levels | `GET /sla/settings`           | `PUT /sla/settings`                | `sla.read` / `sla.admin`         |
 * | Health         | `GET /health/settings`        | `PUT /health/settings`             | `health.read` / `health.admin`   |
 * | Budget         | `GET /budget/settings`        | `PUT /budget/settings`             | `budget.read` / `budget.manage`  |
 *
 * PERMISSIONS DECIDE VISIBILITY; THE SERVER DECIDES EVERYTHING (invariant 6).
 * A tab whose READ permission the operator does not hold is not rendered and
 * its query never fires — showing a tab that can only 403 is a worse screen,
 * not a safer one. A form whose WRITE permission they lack renders with every
 * control disabled and the required permission code named. Neither is
 * security: the server rejects an unauthorized call regardless of what this
 * console drew, and it is the only thing that does.
 *
 * NO SECRET IS EVER RENDERED (invariant 3). Notification channel credentials
 * are write-only — `ChannelOut` cannot carry one, the box is `type="password"`,
 * always empty, and "leave blank to keep current" describes what the server
 * does with an omitted `secret`. AI provider key material and base URLs are
 * server configuration that no response returns and no field here accepts.
 *
 * NO OPTIMISTIC SUCCESS (rule 3). "Saved." appears only after a 2xx, and only
 * while the form matches what the server returned; a failure shows the
 * server's own message, including the `detail` string it wrote for that exact
 * rejection.
 *
 * THE LAYOUT IS THE KIT'S FORM PATTERN (redesign, Receta FO; Atera's "Account
 * settings", whose tabs do not change the URL): the head, then ONE ROW OF TABS
 * (`UrlTabs`, a strip that scrolls sideways and never wraps into two rows) in
 * `?tab=`, and the active tab's form under it. Each tab is its own form with
 * the kit's one `SaveBar` and an `UnsavedGuard` that names `tab` (decision
 * D16): switching tab with an unsaved change asks first, because the tab IS
 * what is being edited. Only one tab is mounted, so only one guard can hold
 * changes. The left-hand list of chips v3 drew is gone with the redesign.
 *
 * WHAT THE DESIGN'S SETTINGS VIEW HAS THAT THIS DOES NOT, AND WHY. Rule 4: a
 * control with no endpoint behind it is omitted rather than faked, because a
 * form that discards what an operator typed is worse than no form.
 *
 * | Design control | What it would need |
 * |---|---|
 * (Two rows LEFT this table when the RBAC cycle landed: "Users and roles" /
 * "Invite user" and "Role catalog". They said "no user-directory endpoint" and
 * "no role or permission-catalogue endpoint", and both are now false —
 * `/api/v1/users`, `/api/v1/roles` and `/api/v1/permissions` exist and the
 * Users and Roles tabs are forms over them. Rule 4 has not moved; the list
 * below is what still has nothing behind it.)
 * | Organization slug | A `slug` field on the org endpoint and in its settings allowlist. Nothing serves or accepts one |
 * | Default timezone, data residency | No endpoint carries either. They will appear on the Organization tab automatically if they enter the server's org-settings allowlist — the form renders the keys the server returns |
 * | "Authorization completeness" panel | A claim about backend route coverage, measured by `router_inspect.py` in the BACKEND's suite. Restating it here would be this console vouching for something it cannot observe |
 * | "Integrations" cards (AD, Sentinel, Odoo, Power BI, S3) with Connected/Available | No integration registry endpoint; every state in the mock is a literal. SMTP is the one that is real — it is a notification CHANNEL, and it is editable on the Notifications tab |
 * | "Security defaults" (MFA, recording, approval, retention) | The second factor is required of every technician (own authentication design §2.2), and how long a sign-in lasts is the Sessions tab. Remote-module policy and audit retention live in separate modules; no settings endpoint accepts them |
 * | "Trust posture" panel | The five security invariants rendered as if a request had confirmed them. They hold because of how the platform is built, not because a screen checked |
 * | "Deployment profile" (gateway URL, object storage, identity broker, secrets service) and topology map | Deployment configuration. Nothing serves it, and nothing should accept it from a browser — a gateway URL writable from the console is a fleet-wide redirect primitive |
 * | The "Patch policy" body v3 draws under Settings | It is the patch module's own policy editor (`/patch/policies`), which owns the maintenance-window painter. One server-side policy must not have two editors |
 */

import { useLayoutEffect, useMemo, useRef } from "react";
import type { MessageKey } from "../../i18n/core";

import { useT } from "../../i18n/context";
import { useEverywherePermissions } from "../../lib/mePermissions";
import { PageHead, UrlTabs, useUrlTab } from "../../shell/components";
import type { TabItem } from "../../shell/components";
import { useEmailSettings, useHealthSettings, useMe, useSlaSettings } from "./api";
import { AI_ADMIN, AssistantTab } from "./AssistantTab";
import { AiConnectionsTab, MCP_USE } from "./AiConnectionsTab";
import { BUDGET_MANAGE, BUDGET_READ, BudgetTab } from "./BudgetTab";
import { EmailTab } from "./EmailTab";
import { HEALTH_ADMIN, HEALTH_READ, HealthTab } from "./HealthTab";
import { MicrosoftTab } from "./MicrosoftTab";
import {
  NOTIFICATION_ADMIN,
  NOTIFICATION_READ,
  NotificationsTab,
} from "./NotificationsTab";
import { ORGS_MANAGE, OrganizationTab } from "./OrganizationTab";
import { ROLES_MANAGE, ROLES_READ, RolesTab } from "./RolesTab";
import { USERS_MANAGE, USERS_READ, UsersTab } from "./UsersTab";
import { SessionsTab } from "./SessionsTab";
import { SLA_ADMIN, SLA_READ, ServiceLevelsTab } from "./ServiceLevelsTab";
import { DEFAULT_SETTINGS_TAB, SETTINGS_TABS, type SettingsTabKey as TabKey } from "./tabs";

/**
 * Whether a section is set up, so the nav can say so before the tab is ever
 * opened. `undefined` (no `state` at all) means "not asserted" and MUST
 * render nothing -- an unanswered question is not the same as a "no".
 *
 * `"forbidden"` is not a third member here. `tabs` below is built by
 * filtering out every section the operator's read permission does not cover
 * (`canReadRoles ? [...] : []` and its siblings) -- a tab the operator may
 * not read never reaches this component at all, so a state that fires only
 * for a tab already in the list can never observe "no access". If a reachable
 * case ever needs it, it is a new member and a new predicate, not this one
 * doing double duty.
 */
type SectionState = "set" | "unset";

interface NavItem extends TabItem {
  state?: SectionState;
}

/**
 * `configured` from a section's own settings response, gated the same way
 * the section's own query is: `undefined` while the permission is withheld,
 * the query has not resolved, or it failed -- never inferred from a loading
 * or absent read. "An unknown state must not read as 'set'" governs the
 * `!data` branch as much as the disabled one.
 */
function sectionState(enabled: boolean, data: { configured: boolean } | undefined): SectionState | undefined {
  if (!enabled || !data) return undefined;
  return data.configured ? "set" : "unset";
}

/**
 * THE STATE IS WORDS IN THE TAB, NEVER A DOT. "Set up" / "Not set up" follows
 * the tab's own name in its label ("Email · Not set up"), so it is real text
 * inside the tab and part of its accessible name: colour is never the only
 * carrier (SC 1.4.1), and the kit's `TabItem` carries a label and a count, no
 * third slot (a `note` is asked of the kit in the wave's report).
 */
const STATE_LABEL: Record<SectionState, MessageKey> = {
  set: "settings.page.stateSet",
  unset: "settings.page.stateUnset",
};

export function SettingsPage() {
  const t = useT();
  const me = useMe();
  const held = useMemo(() => new Set(me.data?.permissions ?? []), [me.data]);

  const canManageOrg = held.has(ORGS_MANAGE);
  const canReadRoles = held.has(ROLES_READ);
  const canReadUsers = held.has(USERS_READ);
  const canAiAdmin = held.has(AI_ADMIN);
  const canUseMcp = held.has(MCP_USE);
  const canReadNotifications = held.has(NOTIFICATION_READ);
  const canReadSla = held.has(SLA_READ);
  const canReadHealth = held.has(HEALTH_READ);
  const canReadBudget = held.has(BUDGET_READ);
  const canManageBudget = held.has(BUDGET_MANAGE);
  // CONFIGURATION EVERY CLIENT SHARES (phase 3 design §2). Organization,
  // email, roles, users, the assistant, notifications, service levels and
  // health settings are written through routes that need the code on EVERY
  // client (`GLOBAL_WRITES`): a tab is offered with the code (`held`), its
  // writes with the code everywhere. Budget settings are one client's, and
  // keep `held`.
  const everywhere = useEverywherePermissions();

  // THE NAV'S "IS THIS SET UP?" STATE, FOR THE THREE TABS THAT CAN ANSWER IT
  // HONESTLY. Email, Service levels and Health each already fetch a
  // `configured: boolean` for their own form; consuming those same hooks
  // here (never new ones -- `api.ts` gains nothing from this task) surfaces
  // that answer in the nav before the tab is ever opened, gated on the exact
  // permission the tab itself gates on so a permission the operator lacks
  // never fires a request just to paint a badge they cannot follow into a
  // tab. Organization, Roles, Users, Assistant, Notifications and Budget have
  // no such flag in what they already fetch -- inventing one is the kind of
  // furniture rule 4 already forbids, so their nav items carry no `state`.
  const emailSettings = useEmailSettings(canManageOrg);
  const slaSettings = useSlaSettings(canReadSla);
  const healthSettings = useHealthSettings(canReadHealth);
  const emailState = sectionState(canManageOrg, emailSettings.data);
  const slaState = sectionState(canReadSla, slaSettings.data);
  const healthState = sectionState(canReadHealth, healthSettings.data);

  // The Organization tab is always offered: every session belongs to an org,
  // and `GET /orgs/current` is the org's own profile. Whether it can be
  // WRITTEN is `orgs.manage`, and whether it exists at all is the server's
  // answer — a 404 renders the disabled "not available" state rather than an
  // editable form that posts nowhere.
  // THE PROFILE TAB IS GONE FROM HERE, and lives at `/profile` (reached from
  // the account menu behind your own avatar). It was never administration:
  // it edits the caller's own record and `PATCH /me` takes no user id. Being
  // a tab also broke the credential steps it hosts -- they leave for the
  // sign-in pages and return to a URL, and a tab kept in React state is not in
  // the URL, so every one of them landed back on Organization.
  // The Email tab is offered only to `orgs.manage`, and unlike Organization it
  // is not offered at all without it: its READ is the same code as its write,
  // so rendering it for anyone else would be a form that can only 403. The
  // banner it drives on the Users tab is gated the same way — pointing someone
  // at a tab they cannot open is not a warning, it is a dead end.
  const tabs: NavItem[] = [
    { key: "organization", label: t("settings.page.tabs.organization") },
    ...(canManageOrg ? [{ key: "email", label: t("settings.page.tabs.email"), state: emailState }] : []),
    ...(canReadRoles ? [{ key: "roles", label: t("settings.page.tabs.roles") }] : []),
    ...(canReadUsers ? [{ key: "users", label: t("settings.page.tabs.users") }] : []),
    // SESSIONS IS OFFERED WITH `orgs.manage`, like Email: its read is the
    // same code, and it is one setting for the whole organization.
    ...(canManageOrg ? [{ key: "sessions", label: t("settings.page.tabs.sessions") }] : []),
    // MICROSOFT SIGN-IN IS OFFERED WITH `orgs.manage` too, like Sessions: its
    // read is the same code, and it is one setting for the whole organization.
    ...(canManageOrg ? [{ key: "microsoft", label: t("settings.page.tabs.microsoft") }] : []),
    ...(canAiAdmin ? [{ key: "assistant", label: t("settings.page.tabs.assistant") }] : []),
    // AI connections is offered for EITHER code: the switch is an org-admin's
    // whether or not they use an AI app, and a person who uses one owns their
    // own revoke whether or not they administer the org (MCP design §8).
    ...(canUseMcp || canManageOrg
      ? [{ key: "ai-connections", label: t("settings.page.tabs.ai-connections") }]
      : []),
    ...(canReadNotifications ? [{ key: "notifications", label: t("settings.page.tabs.notifications") }] : []),
    ...(canReadSla ? [{ key: "sla", label: t("settings.page.tabs.sla"), state: slaState }] : []),
    ...(canReadHealth ? [{ key: "health", label: t("settings.page.tabs.health"), state: healthState }] : []),
    ...(canReadBudget ? [{ key: "budget", label: t("settings.page.tabs.budget") }] : []),
  ];

  // THE ACTIVE TAB IS `?tab=<key>`, NOT REACT STATE, so another page can link
  // to one (`settingsHref` in ./tabs.ts). An unknown key reads as the default,
  // and the default writes no key. A change is a NEW history entry (the kit's
  // `useUrlTab`), so Back undoes a tab change; any other query key is kept.
  const [active, select] = useUrlTab<TabKey>(SETTINGS_TABS, DEFAULT_SETTINGS_TAB);
  // A tab can vanish between renders (permissions load after the first paint),
  // so the active key is validated against what is actually offered. The URL
  // is left alone meanwhile: `?tab=users` opens Users once `/me` has answered.
  const current: TabKey = tabs.some((tab) => tab.key === active) ? active : DEFAULT_SETTINGS_TAB;
  const items: { key: TabKey; label: string }[] = tabs.map((tab) => ({
    key: tab.key as TabKey,
    label: tab.state ? `${tab.label} · ${t(STATE_LABEL[tab.state])}` : tab.label,
  }));
  // THE SELECTED TAB STAYS IN VIEW WHEN A LABEL GROWS. Three labels lengthen
  // when their section's own query answers ("Email · Set up"), and the kit's
  // `Tabs` brings the selected tab into view only when the selection changes:
  // on a phone `?tab=sessions` opened with Sessions in view and then lost it
  // to the longer Email label. This repeats the kit's own sideways scroll when
  // the labels change (a kit request in the wave's report asks `Tabs` to do it
  // itself, and this effect then goes).
  const stripHolder = useRef<HTMLDivElement>(null);
  const labels = items.map((item) => item.label).join("|");
  useLayoutEffect(() => {
    const list = stripHolder.current?.querySelector<HTMLElement>('[role="tablist"]');
    const tab = list?.querySelector<HTMLElement>(`[data-tab="${current}"]`);
    if (!list || !tab) return;
    const box = list.getBoundingClientRect();
    const own = tab.getBoundingClientRect();
    const margin = 16;
    if (own.left < box.left) list.scrollLeft -= box.left - own.left + margin;
    else if (own.right > box.right) list.scrollLeft += own.right - box.right + margin;
  }, [labels, current]);

  return (
    <>
      <PageHead
        title={t("settings.page.title")}
        // AUDIT [MEDIUM]: "The header promises 'Every configuration this
        // platform serves' ... /platform-status lists 25 registered modules"
        // against nine tabs, "so a user hunting for (say) IPAM or backup
        // configuration will search here and conclude it doesn't exist." The
        // description no longer claims to serve everything, and its second
        // sentence says where the rest lives — softening the claim without
        // saying where would leave the same person looking just as long. (It
        // sat under the section list until that list became the tab strip.)
        description={
          <>
            <span>{t("settings.page.description")}</span> <span>{t("settings.page.otherModules")}</span>
          </>
        }
      />

      <div ref={stripHolder}>
        <UrlTabs
          label={t("settings.page.sections")}
          items={items}
          active={current}
          onSelect={select}
        >
          {current === "organization" ? <OrganizationTab canManage={everywhere.has(ORGS_MANAGE)} /> : null}
          {current === "email" ? (
            <EmailTab canManage={canManageOrg} canEdit={everywhere.has(ORGS_MANAGE)} />
          ) : null}
          {current === "roles" ? (
            <RolesTab canRead={canReadRoles} canManage={everywhere.has(ROLES_MANAGE)} />
          ) : null}
          {current === "users" ? (
            <UsersTab
              canRead={canReadUsers}
              canManage={everywhere.has(USERS_MANAGE)}
              canReadEmail={canManageOrg}
            />
          ) : null}
          {current === "sessions" ? (
            <SessionsTab canRead={canManageOrg} canEdit={everywhere.has(ORGS_MANAGE)} />
          ) : null}
          {current === "microsoft" ? (
            <MicrosoftTab canRead={canManageOrg} canEdit={everywhere.has(ORGS_MANAGE)} />
          ) : null}
          {current === "assistant" ? (
            <AssistantTab canAdmin={canAiAdmin} canEdit={everywhere.has(AI_ADMIN)} />
          ) : null}
          {current === "ai-connections" ? (
            <AiConnectionsTab
              canUse={canUseMcp}
              canManageOrg={canManageOrg}
              canSwitch={everywhere.has(ORGS_MANAGE)}
            />
          ) : null}
          {current === "notifications" ? (
            <NotificationsTab canRead={canReadNotifications} canAdmin={everywhere.has(NOTIFICATION_ADMIN)} />
          ) : null}
          {current === "sla" ? (
            <ServiceLevelsTab canRead={canReadSla} canAdmin={everywhere.has(SLA_ADMIN)} />
          ) : null}
          {current === "health" ? (
            <HealthTab canRead={canReadHealth} canAdmin={everywhere.has(HEALTH_ADMIN)} />
          ) : null}
          {current === "budget" ? (
            <BudgetTab
              canRead={canReadBudget}
              canManage={canManageBudget}
              canReadUsers={canReadUsers}
            />
          ) : null}
        </UrlTabs>
      </div>
    </>
  );
}
