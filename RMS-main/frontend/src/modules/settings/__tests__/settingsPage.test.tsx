/**
 * Settings, at the page level: which tabs exist, and what the design has that
 * this deliberately does not.
 *
 * The per-tab behaviour (load, save, fail, secret, permission) lives in the
 * five sibling files. What is asserted here is the pair of properties that
 * belong to the page rather than to any one form:
 *
 *   1. every tab is a REAL form — the old read-only page asserted the OPPOSITE
 *      ("no input, select or textarea anywhere"), because the screen it
 *      replaced linked out to other editors. That assertion is inverted here on
 *      purpose, and it is the whole point of this rewrite;
 *   2. a control with no endpoint behind it is still absent. Rule 4 did not go
 *      away because the page became editable: a form that discards what an
 *      operator typed is worse than no form, and the mock's users, roles,
 *      integrations, security-defaults and deployment tabs have no route to
 *      write to.
 */

import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  apiClient,
  currentLocation,
  calls,
  EMAIL_UNCONFIGURED_FIXTURE,
  HEALTH_SETTINGS_FIXTURE,
  installDefaults,
  renderSettings,
  SLA_SETTINGS_FIXTURE,
} from "./harness";
import { settingsHref } from "../tabs";

// The OIDC client is not under test and constructs a UserManager (with a
// silent-renew timer) at import time. `../api.ts`'s org request only needs the
// bearer token, so the token source is stubbed.
vi.mock("../../../lib/auth", () => ({
  accessToken: () => "test-token",
  // A factory mock is a strict namespace: a missing export throws. This one
  // stayed after Profile moved to its own route, because other tabs still
  // reach for the authority when they name the identity provider.
  oidcAuthority: "https://id.example.test/realms/it-suite",
}));

// `apiClient` is referenced LAZILY — inside the returned closure, never while
// the factory runs. Importing the harness from within the factory instead
// deadlocks: the harness imports `SettingsPage`, which imports the very module
// this factory is mocking, so the import waits on a factory that is waiting on
// the import.
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => apiClient() }));

beforeEach(() => {
  installDefaults();
});

// PROFILE IS NOT HERE ANY MORE. It moved to `/profile` on 2026-08-20: it is
// the caller's own record rather than administration, and the credential steps
// it hosts return to a URL, which the active Settings tab never was. Its tests
// moved with it to `src/modules/profile/`.
const ALL_TABS = [
  "Organization",
  "Roles",
  "Users",
  "Assistant",
  "Notifications",
  "Service levels",
  "Health",
  "Budget",
];

// EVERY `{ name: label }` LOOKUP BELOW USES `new RegExp(label)`, NOT THE
// BARE STRING (Task 7). The nav now appends "Set up" / "Not set up" to a
// tab's own accessible name for Service levels and Health (and Email, off
// this list), so the exact string this array holds is only ever a PREFIX of
// the name once that tab's own settings query resolves -- and `getByRole`'s
// `name` option matches a string by whole-string equality regardless of
// `exact` (that option is text-query-only), so a plain string here would
// start failing the moment the query settles. A `RegExp` matches a
// substring, which is what "still this tab, plus whatever it now says about
// itself" actually needs.

// THE ACTIVE TAB IS IN THE URL (`?tab=<key>`), so another page can link
// straight to Settings -> Users, Email or Health (docs/design/
// cross-module-guidance.md §0.1). Before this, every such link landed on
// Organization, because the tab was React state and nothing read the query.
describe("SettingsPage ?tab= deep link", () => {
  it("opens the tab the link names", async () => {
    renderSettings("/settings?tab=users");
    const users = await screen.findByRole("tab", { name: "Users" });
    expect(users).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Organization" })).toHaveAttribute("aria-selected", "false");
  });

  it("writes the tab an operator selects into the URL, keeping the rest of the query", async () => {
    const user = userEvent.setup();
    renderSettings("/settings?from=dashboard");
    await user.click(await screen.findByRole("tab", { name: /Health/ }));
    expect(screen.getByRole("tab", { name: /Health/ })).toHaveAttribute("aria-selected", "true");
    const search = new URLSearchParams(currentLocation.value?.search);
    expect(search.get("tab")).toBe("health");
    expect(search.get("from")).toBe("dashboard");

    // Back to the default: the key comes off rather than naming the default.
    await user.click(screen.getByRole("tab", { name: "Organization" }));
    expect(new URLSearchParams(currentLocation.value?.search).has("tab")).toBe(false);
  });

  it("builds the link other pages use, and that link opens the tab", async () => {
    expect(settingsHref("organization")).toBe("/settings");
    expect(settingsHref("email")).toBe("/settings?tab=email");
    renderSettings(settingsHref("health"));
    expect(await screen.findByRole("tab", { name: /Health/ })).toHaveAttribute("aria-selected", "true");
  });

  it("falls back to Organization for a key it does not know", async () => {
    renderSettings("/settings?tab=integrations");
    await screen.findByRole("tab", { name: "Budget" });
    expect(screen.getByRole("tab", { name: "Organization" })).toHaveAttribute("aria-selected", "true");
  });

  it("falls back to Organization for a tab this account is not offered", async () => {
    // The link is no way round the tab list: Email is offered to `orgs.manage`
    // only, and naming it in the URL does not open it for anyone else.
    installDefaults(["notification.read"]);
    renderSettings("/settings?tab=email");
    await screen.findByRole("tab", { name: "Notifications" });
    expect(screen.queryByRole("tab", { name: /Email/ })).toBeNull();
    expect(screen.getByRole("tab", { name: "Organization" })).toHaveAttribute("aria-selected", "true");
  });
});

describe("SettingsPage", () => {
  it("offers one tab per writable surface", async () => {
    renderSettings();
    expect(screen.getByRole("heading", { name: "Settings" })).toBeInTheDocument();
    // The tab strip is built from the session's permission codes, so it is
    // complete only once `/me` has answered — waiting on the heading (which is
    // static) would assert against a strip that has not been built yet.
    await screen.findByRole("tab", { name: "Budget" });
    for (const label of ALL_TABS) {
      expect(screen.getByRole("tab", { name: new RegExp(label) })).toBeInTheDocument();
    }
  });

  it("claims only the configuration it actually serves", async () => {
    // AUDIT [MEDIUM]: the header promised "Every configuration this platform
    // serves" over nine tabs, against 25 registered modules — "a user hunting
    // for (say) IPAM or backup configuration will search here and conclude it
    // doesn't exist". Falsified against that sentence, which this rejects.
    renderSettings();
    await screen.findByRole("tab", { name: "Organization" });
    // RE-POINTED, design review 2026-09-11 (`docs/design/settings-language.md`
    // glossary: "organization-wide, org-wide scope" -> "everywhere"): the
    // description carried the internal word this pass removes everywhere it
    // appears. The genuine target is the rewritten sentence, same claim.
    screen.getByText(/Configuration that applies everywhere in this tenant\./i);
    expect(document.body.textContent ?? "").not.toContain("Organization-wide");
    // The finding's remedy — say where the rest lives — is still on the page,
    // but as one line under the nav rather than inside the page description.
    screen.getByText(/Other modules are configured from their own page\./i);
    expect(document.body.textContent ?? "").not.toContain(
      "Every configuration this platform serves",
    );
  });

  it("is a form, not a summary: every tab has controls and a save", async () => {
    const user = userEvent.setup();
    renderSettings();
    for (const label of ALL_TABS) {
      await user.click(await screen.findByRole("tab", { name: new RegExp(label) }));
      const controls = document.querySelectorAll("input, select, textarea");
      expect(controls.length, `${label} must be editable`).toBeGreaterThan(0);
      expect(
        screen.getAllByRole("button", { name: /save|create/i }).length,
        `${label} must be able to write`,
      ).toBeGreaterThan(0);
    }
  });

  it("hides the tabs whose read permission the operator lacks, and fetches nothing for them", async () => {
    installDefaults(["notification.read"]);
    renderSettings();
    expect(await screen.findByRole("tab", { name: "Notifications" })).toBeInTheDocument();
    // The organization tab is always offered: every session belongs to an org.
    expect(screen.getByRole("tab", { name: "Organization" })).toBeInTheDocument();
    for (const label of ["Assistant", "Service levels", "Health", "Budget", "Roles", "Users"]) {
      expect(screen.queryByRole("tab", { name: new RegExp(label) })).not.toBeInTheDocument();
    }
    expect(calls.listRoles).not.toHaveBeenCalled();
    expect(calls.listPermissions).not.toHaveBeenCalled();
    expect(calls.listUsers).not.toHaveBeenCalled();
    expect(calls.getAiSettings).not.toHaveBeenCalled();
    expect(calls.getSlaSettings).not.toHaveBeenCalled();
    expect(calls.getHealthSettings).not.toHaveBeenCalled();
    expect(calls.getBudgetSettings).not.toHaveBeenCalled();
  });

  it("builds none of the design's controls that have no endpoint behind them", async () => {
    const user = userEvent.setup();
    renderSettings();
    await screen.findByRole("tab", { name: "Organization" });

    // Each of these is a control in the prototype's `renderSettings` with
    // nothing on the API to read it from or write it to. The reasons are
    // tabulated in `SettingsPage.tsx`'s header.
    // "Users and roles", "Invite user" and "Role catalog" USED to be on this
    // list. They are not any more: `/api/v1/roles`, `/api/v1/permissions` and
    // `/api/v1/users` exist, so the Roles and Users tabs are real forms over
    // real routes. Rule 4 has not moved — the list below is what still has no
    // endpoint behind it.
    for (const absent of [
      /Authorization completeness/i,
      /Trust posture/i,
      /Security defaults/i,
      /Deployment profile/i,
      /Topology summary/i,
    ]) {
      expect(screen.queryByText(absent), `${String(absent)} has no endpoint`).not.toBeInTheDocument();
      expect(screen.queryByRole("tab", { name: absent })).not.toBeInTheDocument();
    }

    // And specifically: no field for anything the server does not accept.
    for (const label of [
      /Organization slug/i,
      /Data residency/i,
      /Session timeout/i,
      /Agent gateway URL/i,
      /Multi-factor authentication/i,
      /Audit retention/i,
    ]) {
      expect(screen.queryByLabelText(label)).not.toBeInTheDocument();
    }

    // Non-vacuity: the tabs that DO have endpoints are all reachable.
    for (const label of ALL_TABS) {
      await user.click(screen.getByRole("tab", { name: new RegExp(label) }));
      expect(screen.getByRole("tab", { name: new RegExp(label) })).toHaveAttribute("aria-selected", "true");
    }
  });

  // ----------------------------------------------------------------------
  // The tablist had no panel. A screen-reader user was told "tab, selected"
  // with nothing to move into: the section container carried no
  // `role="tabpanel"`, no id and no tab pointed at it, and the whole widget
  // sat inside a `<nav>` landmark as well.
  // ----------------------------------------------------------------------

  it("points every tab at the panel it switches, and the panel back at the tab", async () => {
    const user = userEvent.setup();
    renderSettings();
    await screen.findByRole("tab", { name: "Organization" });

    const panel = screen.getByRole("tabpanel");
    expect(panel.id, "the panel needs an id for aria-controls to name").not.toBe("");

    for (const label of ALL_TABS) {
      await user.click(screen.getByRole("tab", { name: new RegExp(label) }));
      const tab = screen.getByRole("tab", { name: new RegExp(label) });
      expect(tab).toHaveAttribute("aria-controls", panel.id);
      // …and the panel says which tab it belongs to, so the relationship reads
      // in both directions.
      expect(screen.getByRole("tabpanel")).toHaveAttribute("aria-labelledby", tab.id);
      expect(tab.id).not.toBe("");
    }
  });

  it("does not wrap the tablist in a navigation landmark", async () => {
    // Two structures claiming the same nine controls: the landmark is
    // announced on the way into a widget that is not a set of links.
    renderSettings();
    await screen.findByRole("tab", { name: "Organization" });
    const tablist = screen.getByRole("tablist");
    expect(tablist).toHaveAccessibleName("Settings sections");
    expect(tablist.closest("nav")).toBeNull();
  });

  // ----------------------------------------------------------------------
  // Task 7: the nav reports each section's state before you click into it.
  //
  // Scoped to the three tabs whose existing query already answers "is this
  // set up?" honestly — Email, Service levels and Health each fetch a
  // `configured: boolean` from their own settings endpoint. The rest (
  // Organization, Roles, Users, Assistant, Notifications, Budget) have no
  // such signal in what they already fetch, so they render no state at all
  // rather than one invented for this task.
  // ----------------------------------------------------------------------

  it("says which sections are not set up, before you click them", async () => {
    calls.getEmailSettings.mockResolvedValue(EMAIL_UNCONFIGURED_FIXTURE);
    renderSettings();
    const tab = await screen.findByRole("tab", { name: /Email/ });
    // NOT COLOUR ALONE: the state is real text inside the tab, not a hue.
    expect(await within(tab).findByText(/not set up/i)).toBeInTheDocument();
    expect(tab).toHaveAccessibleName(/not set up/i);
  });

  it("says a configured section is set up, before you click it", async () => {
    // The default fixtures (installDefaults) already answer `configured:
    // true` for Email, Service levels and Health — the state this platform
    // is normally in once someone has filled the form in once.
    renderSettings();
    const email = await screen.findByRole("tab", { name: /Email/ });
    const sla = screen.getByRole("tab", { name: /Service levels/ });
    const health = screen.getByRole("tab", { name: /Health/ });
    // The tab node itself exists as soon as `/me` answers, one render before
    // its OWN settings query (a separate request) has necessarily resolved
    // -- so the "Set up" text is awaited per tab rather than asserted the
    // instant the tab appears.
    for (const tab of [email, sla, health]) {
      expect(await within(tab).findByText("Set up")).toBeInTheDocument();
      expect(tab).toHaveAccessibleName(/(?<!not )set up/i);
    }
  });

  it("reports service levels and health independently of email", async () => {
    calls.getSlaSettings.mockResolvedValue({ ...SLA_SETTINGS_FIXTURE, configured: false });
    calls.getHealthSettings.mockResolvedValue({ ...HEALTH_SETTINGS_FIXTURE, configured: false });
    renderSettings();
    const sla = await screen.findByRole("tab", { name: /Service levels/ });
    const health = screen.getByRole("tab", { name: /Health/ });
    const email = screen.getByRole("tab", { name: /Email/ });
    expect(await within(sla).findByText("Not set up")).toBeInTheDocument();
    expect(await within(health).findByText("Not set up")).toBeInTheDocument();
    // Email's own query was left on the happy-path fixture: its state must
    // not have been derived from a sibling tab's answer.
    expect(await within(email).findByText("Set up")).toBeInTheDocument();
    expect(sla).toHaveAccessibleName(/not set up/i);
    expect(health).toHaveAccessibleName(/not set up/i);
    expect(email).toHaveAccessibleName(/(?<!not )set up/i);
  });

  it("renders no state for a tab with no completeness signal to report", async () => {
    renderSettings();
    // Waiting on Budget (permission-gated), not Organization (always
    // present from the first render before `/me` has answered) -- the same
    // synchronization point the file's very first test uses, and for the
    // same reason.
    const budget = await screen.findByRole("tab", { name: "Budget" });
    const organization = screen.getByRole("tab", { name: "Organization" });
    for (const tab of [organization, budget]) {
      expect(within(tab).queryByText(/set up/i)).not.toBeInTheDocument();
      expect(tab).not.toHaveAccessibleName(/set up/i);
    }
  });

  it("does not claim a section is set up while its own query is still pending", async () => {
    // An unresolved promise: the request is in flight and has not answered
    // yet. An unknown state must not read as "set" -- or as "unset".
    calls.getHealthSettings.mockImplementation(() => new Promise(() => {}));
    renderSettings();
    const health = await screen.findByRole("tab", { name: "Health" });
    expect(within(health).queryByText(/set up/i)).not.toBeInTheDocument();
    expect(health).not.toHaveAccessibleName(/set up/i);
  });

  it("gates the state queries on the same permission each tab already uses", async () => {
    // sla.read and health.read withheld: Service levels and Health are not
    // offered at all, and their settings are never fetched just to paint a
    // nav badge nobody is allowed to see the tab for.
    installDefaults(["orgs.manage"]);
    renderSettings();
    await screen.findByRole("tab", { name: /Email/ });
    expect(screen.queryByRole("tab", { name: "Service levels" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Health" })).not.toBeInTheDocument();
    expect(calls.getSlaSettings).not.toHaveBeenCalled();
    expect(calls.getHealthSettings).not.toHaveBeenCalled();
  });
});
