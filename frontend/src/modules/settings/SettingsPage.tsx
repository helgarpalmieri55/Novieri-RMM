/**
 * Settings — every configuration this platform serves, EDITED here.
 *
 * The operator's requirement is explicit: "Everything under the Settings menu
 * must be editable — all configurations will take place there and not directly
 * on the server files." The previous version of this file was a read-only
 * summary that linked out to five other editors; this one is ten forms.
 *
 * WHAT EACH TAB WRITES, AND THE ROUTE BEHIND IT:
 *
 * | Tab            | Read                          | Write                              | Permission (read / write)        |
 * |----------------|-------------------------------|------------------------------------|----------------------------------|
 * | Organization   | `GET /orgs/current`           | `PATCH /orgs/current`              | — / `orgs.manage`                |
 * | Email          | `GET /settings/email`         | `PUT /settings/email`, `POST /settings/email/test` | `orgs.manage` / `orgs.manage` |
 * | Roles          | `GET /roles`, `/permissions`  | `POST`/`PATCH`/`DELETE /roles`, `PUT /roles/{id}/permissions` | `roles.read` / `roles.manage` |
 * | Users          | `GET /users`, `/users/{id}/effective-permissions` | `POST /users`, role assign/revoke, disable/enable, the two mails, `DELETE` | `users.read` / `users.manage` |
 * | Assistant      | `GET /ai/settings`, `/ai/providers` | `PUT /ai/settings`           | `ai.admin` / `ai.admin`          |
 * | AI connections | `GET /orgs/current`, `GET /mcp/connections` | `PATCH /orgs/current` (`mcp_enabled` only), `POST /mcp/connections/{client_id}/revoke` | `mcp.use` OR `orgs.manage` / switch and "for everyone" `orgs.manage`, "for me" `mcp.use` |
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
 * THE LAYOUT IS v3's `settings` SCREEN: the head, then a two-column `.g2` of
 * `220px 1fr` with the section list on the left as a `.panel` of `.chip`
 * links and the active section's panels on the right. The list is a real
 * vertical tablist -- one tab stop, the arrow keys move within it, and the
 * selected chip carries `aria-selected` -- because the sections are tabs in
 * everything but their direction, and the tests that locate them by role
 * predate the design. (The kit's `Tabs` draws the horizontal strip only; a
 * vertical orientation on it is reported as a kit gap, and this list moves
 * onto it when it exists.)
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
 * | "Security defaults" (MFA, session timeout, recording, approval, retention) | Realm policy (Keycloak), remote-module policy and audit retention live in deployment configuration and separate modules. No settings endpoint accepts any of them |
 * | "Trust posture" panel | The five security invariants rendered as if a request had confirmed them. They hold because of how the platform is built, not because a screen checked |
 * | "Deployment profile" (gateway URL, object storage, identity broker, secrets service) and topology map | Deployment configuration. Nothing serves it, and nothing should accept it from a browser — a gateway URL writable from the console is a fleet-wide redirect primitive |
 * | The "Patch policy" body v3 draws under Settings | It is the patch module's own policy editor (`/patch/policies`), which owns the maintenance-window painter. One server-side policy must not have two editors |
 */

import { useMemo, type CSSProperties, type KeyboardEvent } from "react";
import type { MessageKey } from "../../i18n/core";
import { useSearchParams } from "react-router-dom";

import { useT } from "../../i18n/context";
import { PageHead } from "../../shell/components";
import type { TabItem } from "../../shell/components";
import { useEmailSettings, useHealthSettings, useMe, useSlaSettings } from "./api";
import { AI_ADMIN, AssistantTab } from "./AssistantTab";
import { AiConnectionsTab, MCP_USE } from "./AiConnectionsTab";
import { BUDGET_MANAGE, BUDGET_READ, BudgetTab } from "./BudgetTab";
import { EmailTab } from "./EmailTab";
import { HEALTH_ADMIN, HEALTH_READ, HealthTab } from "./HealthTab";
import {
  NOTIFICATION_ADMIN,
  NOTIFICATION_READ,
  NotificationsTab,
} from "./NotificationsTab";
import { ORGS_MANAGE, OrganizationTab } from "./OrganizationTab";
import { ROLES_MANAGE, ROLES_READ, RolesTab } from "./RolesTab";
import { USERS_MANAGE, USERS_READ, UsersTab } from "./UsersTab";
import { SLA_ADMIN, SLA_READ, ServiceLevelsTab } from "./ServiceLevelsTab";
import { DEFAULT_SETTINGS_TAB, isSettingsTab, type SettingsTabKey as TabKey } from "./tabs";

/**
 * v3's section list, as the design draws it: each entry is a `.chip` laid out
 * as a full-width row with no edge, and only the selected one keeps the chip's
 * accent wash. The two declarations are the design file's own inline styles
 * on `P['settings']`, transcribed rather than restyled.
 */
const NAV_ITEM: CSSProperties = {
  display: "flex",
  width: "100%",
  marginBottom: 4,
  border: 0,
  cursor: "pointer",
  // THE THIRD DECLARATION IS THE CONSOLE'S, AND IT EXISTS TO MATCH THE DESIGN
  // RATHER THAN DEPART FROM IT. v3's own section list on this screen runs a
  // 34px pitch (rows at y=156/190/224/258/292/326/360); these rows measured
  // 30.5px, so nine tap targets sat about 3px apart on a handset -- above the
  // 24px WCAG floor, so the gate passed them, and below both the design and
  // the 44px the spec holds the top bar to. The 3px is the kit's `.chip`
  // adding `line-height: 1.2`, which v3's `.chip` does not declare: the
  // design's rows take the body's line-height and are that much taller. A
  // list row is not an inline filter chip, so the padding is set here rather
  // than by taking 12 other chip groups with it.
  padding: "8px 11px",
};
const NAV_ITEM_OFF: CSSProperties = { ...NAV_ITEM, background: "transparent", color: "var(--ink-2)" };

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
 * THE DOT V3 DOES NOT DRAW. v3's only `.dot` is `.top .ib .dot` -- an
 * absolutely-positioned top-bar badge with its own `top/right` offsets and a
 * `--surface` cutout border, meaningless as a static inline mark and matching
 * no rule at all inside a `.chip` (a class name that happens to also be
 * `.dot` renders unstyled and invisible, the same trap an earlier task hit on
 * help text that matched nothing). This repo is design-file-first
 * (`base-styles.test.ts`, `tokens.test.ts`): a hue-only pip here would need a
 * new rule added to `docs/design/itms-prototype-v3.html` FIRST and ported to
 * `shell.css` second, for a mark that `shell.css:295-320`'s own rule ("THREE
 * CARRIERS, NEVER ONE... colour being the only one" -- SC 1.4.1) says cannot
 * be the thing that carries the state anyway. The word is the carrier; a dot
 * would be decoration on top of it. So there is no dot: `.k`, the kit's
 * existing unscoped caption rule (`--t-cap`, uppercase, muted -- already
 * identical in the design file and `components.css`, so nothing new needs
 * porting), carries "Set up" / "Not set up" as text, which is what the
 * falsifier below actually tests.
 */
const STATE_LABEL: Record<SectionState, MessageKey> = {
  set: "settings.page.stateSet",
  unset: "settings.page.stateUnset",
};

/** The one panel every section renders into, and the prefix each tab's id
 *  carries. Constants so the two halves of `aria-controls` /
 *  `aria-labelledby` cannot drift apart. */
const SECTION_PANEL_ID = "settings-section-panel";
const SECTION_TAB_ID = "settings-section-tab";

function SettingsNav({
  items,
  active,
  onSelect,
}: {
  items: readonly NavItem[];
  active: string;
  onSelect: (key: string) => void;
}) {
  const t = useT();
  // ROVING TABINDEX, VERTICALLY: a tablist is one tab stop and the arrow keys
  // move within it. Up and Down here because the list is a column; Home and
  // End jump to the ends; the ring wraps.
  const move = (event: KeyboardEvent<HTMLDivElement>) => {
    const step = event.key === "ArrowDown" ? 1 : event.key === "ArrowUp" ? -1 : 0;
    const jump = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 : -1;
    if (step === 0 && jump < 0) return;
    event.preventDefault();
    const current = items.findIndex((item) => item.key === active);
    const next = jump >= 0 ? jump : (current + step + items.length) % items.length;
    const target = items[next];
    if (!target) return;
    onSelect(target.key);
    event.currentTarget.querySelector<HTMLButtonElement>(`[data-tab="${target.key}"]`)?.focus();
  };

  // NOT A `<nav>` LANDMARK. A tablist inside a navigation landmark is two
  // structures claiming the same nine controls, and assistive technology
  // announces the landmark on the way in to a widget that is not a set of
  // links. The `.panel` box is the same; the accessible name moved onto the
  // tablist itself, which is the element that owns these tabs.
  return (
    <div className="panel" style={{ padding: 8 }}>
      <div
        role="tablist"
        aria-label={t("settings.page.sections")}
        aria-orientation="vertical"
        onKeyDown={move}
      >
        {items.map((item) => {
          const on = item.key === active;
          return (
            <button
              type="button"
              key={item.key}
              role="tab"
              data-tab={item.key}
              // THE TAB NAMES THE PANEL IT SWITCHES, AND THE PANEL NAMES IT
              // BACK. Without these a screen reader says "tab, selected" and
              // there is nothing to move into: the section panel had no
              // `role="tabpanel"`, no id, and no tab pointed at it. One panel
              // element holds whichever section is active, so one id is the
              // whole relationship.
              id={`${SECTION_TAB_ID}-${item.key}`}
              aria-controls={SECTION_PANEL_ID}
              aria-selected={on}
              tabIndex={on ? 0 : -1}
              className={`${on ? "chip on" : "chip"} focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]`}
              style={on ? NAV_ITEM : NAV_ITEM_OFF}
              onClick={() => onSelect(item.key)}
            >
              {item.label}
              {item.state ? (
                <span className="k" style={{ marginLeft: "auto" }}>
                  {t(STATE_LABEL[item.state])}
                </span>
              ) : null}
            </button>
          );
        })}
      </div>
    </div>
  );
}

export function SettingsPage() {
  const t = useT();
  const me = useMe();
  const held = useMemo(() => new Set(me.data?.permissions ?? []), [me.data]);

  const canManageOrg = held.has(ORGS_MANAGE);
  const canReadRoles = held.has(ROLES_READ);
  const canManageRoles = held.has(ROLES_MANAGE);
  const canReadUsers = held.has(USERS_READ);
  const canManageUsers = held.has(USERS_MANAGE);
  const canAiAdmin = held.has(AI_ADMIN);
  const canUseMcp = held.has(MCP_USE);
  const canReadNotifications = held.has(NOTIFICATION_READ);
  const canAdminNotifications = held.has(NOTIFICATION_ADMIN);
  const canReadSla = held.has(SLA_READ);
  const canAdminSla = held.has(SLA_ADMIN);
  const canReadHealth = held.has(HEALTH_READ);
  const canAdminHealth = held.has(HEALTH_ADMIN);
  const canReadBudget = held.has(BUDGET_READ);
  const canManageBudget = held.has(BUDGET_MANAGE);

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
  // to one (`settingsHref` in ./tabs.ts). An unknown key reads as the default.
  // Selecting a tab REPLACES the history entry rather than pushing one: Back
  // leaves Settings instead of walking back through every tab clicked, and
  // any other query key on the URL is kept.
  const [searchParams, setSearchParams] = useSearchParams();
  const requested = searchParams.get("tab");
  const active: TabKey = isSettingsTab(requested) ? requested : DEFAULT_SETTINGS_TAB;
  const select = (key: TabKey) => {
    setSearchParams(
      (previous) => {
        const next = new URLSearchParams(previous);
        if (key === DEFAULT_SETTINGS_TAB) next.delete("tab");
        else next.set("tab", key);
        return next;
      },
      { replace: true },
    );
  };
  // A tab can vanish between renders (permissions load after the first paint),
  // so the active key is validated against what is actually offered. The URL
  // is left alone meanwhile: `?tab=users` opens Users once `/me` has answered.
  const current: TabKey = tabs.some((tab) => tab.key === active) ? active : DEFAULT_SETTINGS_TAB;

  return (
    <>
      <PageHead
        title={t("settings.page.title")}
        // AUDIT [MEDIUM]: "The header promises 'Every configuration this
        // platform serves' ... /platform-status lists 25 registered modules"
        // against nine tabs, "so a user hunting for (say) IPAM or backup
        // configuration will search here and conclude it doesn't exist." The
        // description no longer claims to serve everything, and the line under
        // the nav says where the rest lives — softening the claim without
        // saying where would leave the same person looking just as long.
        description={t("settings.page.description")}
      />

      <div className="g2" style={{ gridTemplateColumns: "220px 1fr" }}>
        <div>
          <SettingsNav items={tabs} active={current} onSelect={(key) => (isSettingsTab(key) ? select(key) : undefined)} />
          {/* The audit's "conclude it doesn't exist" finding, kept as one line
              beside the list it is about rather than in the page description. */}
          <p className="note" style={{ marginTop: 10 }}>
            {t("settings.page.otherModules")}
          </p>
        </div>

        <div
          role="tabpanel"
          id={SECTION_PANEL_ID}
          aria-labelledby={`${SECTION_TAB_ID}-${current}`}
          // A tabpanel is a focus target from the tablist, and a section whose
          // permission is missing renders a sentence and no control at all --
          // so the panel itself has to be reachable.
          tabIndex={0}
        >
          {current === "organization" ? <OrganizationTab canManage={canManageOrg} /> : null}
          {current === "email" ? <EmailTab canManage={canManageOrg} /> : null}
          {current === "roles" ? (
            <RolesTab canRead={canReadRoles} canManage={canManageRoles} />
          ) : null}
          {current === "users" ? (
            <UsersTab
              canRead={canReadUsers}
              canManage={canManageUsers}
              canReadEmail={canManageOrg}
            />
          ) : null}
          {current === "assistant" ? <AssistantTab canAdmin={canAiAdmin} /> : null}
          {current === "ai-connections" ? (
            <AiConnectionsTab canUse={canUseMcp} canManageOrg={canManageOrg} />
          ) : null}
          {current === "notifications" ? (
            <NotificationsTab canRead={canReadNotifications} canAdmin={canAdminNotifications} />
          ) : null}
          {current === "sla" ? (
            <ServiceLevelsTab canRead={canReadSla} canAdmin={canAdminSla} />
          ) : null}
          {current === "health" ? (
            <HealthTab canRead={canReadHealth} canAdmin={canAdminHealth} />
          ) : null}
          {current === "budget" ? (
            <BudgetTab
              canRead={canReadBudget}
              canManage={canManageBudget}
              canReadUsers={canReadUsers}
            />
          ) : null}
        </div>
      </div>
    </>
  );
}
