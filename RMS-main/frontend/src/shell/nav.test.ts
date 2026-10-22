/**
 * Guards on the sidebar's navigation model.
 *
 * The one that matters most: a destination added without a permission code
 * would show every operator a link that answers 403. Not a security hole --
 * the server decides, always -- but the kind of defect that ships silently.
 */

import { describe, expect, it } from "vitest";

import { ICON_NAMES } from "./icons";
import {
  activeNavTo,
  ADD_COMPUTER_TO,
  crumbFor,
  isArea,
  isPortalOnly,
  NAV_GROUPS,
  NAV_UNLISTED,
  navAreas,
  navItems,
  ORPHAN_SECTION,
  portalCrumbFor,
  searchDestinations,
  visibleGroups,
} from "./nav";

const ALL_ITEMS = navItems();
const RAIL = NAV_GROUPS.flatMap((group) => group.items);
const EVERY_PERMISSION = ALL_ITEMS.map((item) => item.permission).filter(
  (code): code is string => code !== null,
);

/** What an operator's rail shows, top level only. */
function railLabels(permissions: readonly string[]): string[] {
  return visibleGroups(permissions).flatMap((group) => group.items.map((entry) => entry.label));
}

describe("two layers", () => {
  it("puts the daily work first, the business modules shown, and Admin last", () => {
    expect(NAV_GROUPS.map((group) => group.label)).toEqual([
      "Daily",
      "Governance",
      "Assets & Spend",
      "Configure",
    ]);
    expect(NAV_GROUPS[0]?.items.map((entry) => entry.label)).toEqual([
      "Dashboard",
      "Devices",
      "Alerts",
      "Tickets",
      "Patching",
      "Software",
      "Backup",
      "Remote sessions",
      "Assistant",
    ]);
    expect(NAV_GROUPS[1]?.items.map((entry) => entry.label)).toEqual([
      "IT documentation",
      "Changes",
      "Continuity tests",
      "Reports",
    ]);
    expect(NAV_GROUPS[2]?.items.map((entry) => entry.label)).toEqual([
      "Licenses",
      "Subscriptions",
      "IT budget",
      "IP addresses",
    ]);
    expect(NAV_GROUPS[3]?.items.map((entry) => entry.label)).toEqual(["Admin"]);
  });

  it("folds only configuration away", () => {
    // 36 peers was the finding. Nine daily items (People left on 2026-09-23),
    // eight business pages under two headings, and one Admin entry standing
    // for twenty-odd more.
    expect(RAIL.length).toBeLessThanOrEqual(19);
    expect(navAreas().map((area) => area.to)).toEqual(["/admin"]);
    for (const group of NAV_GROUPS.slice(0, 3)) {
      expect(group.items.every((entry) => !isArea(entry))).toBe(true);
    }
  });

  it("puts every configuration and platform page in Admin", () => {
    const admin = navAreas().find((area) => area.to === "/admin");
    const labels = admin?.sections.flatMap((section) => section.items.map((item) => item.label));
    expect(labels).toEqual(
      expect.arrayContaining([
        "Notifications",
        "Email intake",
        "Service levels",
        "Site servers",
        "Agent packages",
        "Push install",
        "Enrollment",
        "AI automation",
        "Platform status",
        "Error monitor",
        "Settings",
        "Credentials",
        "Audit log",
        "Job history",
      ]),
    );
  });

  it("shows Governance and Assets & Spend pages as ordinary items", () => {
    // Folded on 2026-09-23 and unfolded the same day: the operator read the
    // folded pages as "small and without importance".
    const tos = (label: string) =>
      NAV_GROUPS.find((group) => group.label === label)?.items.map((entry) => entry.to);
    expect(tos("Governance")).toEqual(["/docs", "/changes", "/bcp", "/reports"]);
    expect(tos("Assets & Spend")).toEqual(["/licenses", "/subscriptions", "/budget", "/ipam"]);
  });

  it("names tasks, not machinery", () => {
    const labels = ALL_ITEMS.map((item) => item.label);
    for (const machinery of [
      "Signed Job Engine",
      "Immutable Audit",
      "SLA Engine",
      "Enrollment & Certificates",
      "AI Analyst",
    ]) {
      expect(labels).not.toContain(machinery);
    }
    expect(labels).toContain("Job history");
    expect(labels).toContain("Audit log");
    expect(labels).toContain("Service levels");
  });

  it("gives every area page a line saying what it is for", () => {
    const missing = navAreas()
      .flatMap((area) => area.sections.flatMap((section) => section.items))
      .filter((item) => !item.hint)
      .map((item) => item.label);
    expect(missing).toEqual([]);
  });
});

describe("navigation model", () => {
  it("declares a permission code, or explicit null, on every single item", () => {
    const undeclared = ALL_ITEMS.filter((item) => item.permission === undefined).map(
      (item) => item.label,
    );
    expect(undeclared, `no permission declared: ${undeclared.join(", ")}`).toEqual([]);
  });

  it("only lets a destination be ungated if landing on it really asks nothing", () => {
    // Exact, both ways. Dashboard's only unconditional call is `/me`; every
    // panel is enabled by a permission `/me` returns.
    const ungated = ALL_ITEMS.filter((item) => item.permission === null).map((item) => item.label);
    expect(ungated).toEqual(["Dashboard"]);
  });

  it("gates Platform status on the code its route actually requires", () => {
    const item = ALL_ITEMS.find((entry) => entry.to === "/platform-status");
    expect(item?.permission).toBe("platform.status.read");
  });

  it("routes every item and area somewhere absolute", () => {
    const bad = [...ALL_ITEMS, ...navAreas()].filter((item) => !item.to.startsWith("/"));
    expect(bad).toEqual([]);
  });

  it("uses only icons the design actually defines", () => {
    const unknown = [...ALL_ITEMS, ...navAreas()]
      .filter((item) => !ICON_NAMES.includes(item.icon))
      .map((item) => `${item.label} -> ${item.icon}`);
    expect(unknown).toEqual([]);
  });

  it("never lists the same destination twice", () => {
    const tos = [...ALL_ITEMS, ...navAreas()].map((item) => item.to);
    const doubled = tos.filter((to, index) => tos.indexOf(to) !== index);
    expect(doubled).toEqual([]);
  });

  it("has no Discovered item, because Add a computer is the way in", () => {
    const tos = ALL_ITEMS.map((item) => item.to);
    expect(tos).not.toContain("/discovered");
  });
});

describe("visibleGroups", () => {
  const held = ["devices.read", "patch.read"];

  it("keeps only the items whose permission the operator holds", () => {
    const labels = railLabels(held);
    expect(labels).toContain("Devices");
    expect(labels).toContain("Patching");
    expect(labels).not.toContain("Backup");
  });

  it("shows an area when any of its pages is visible, holding only those pages", () => {
    // `devices.read` opens Enrollment and Device groups, both in Admin.
    const admin = visibleGroups(held)
      .flatMap((group) => group.items)
      .find((entry) => entry.to === "/admin");
    expect(admin && isArea(admin)).toBe(true);
    const pages =
      admin && isArea(admin)
        ? admin.sections.flatMap((section) => section.items.map((item) => item.label))
        : [];
    expect(pages).toEqual(["Enrollment", "Device groups"]);
    // And the section headings over nothing are gone.
    expect(admin && isArea(admin) ? admin.sections.map((s) => s.label) : []).toEqual(["Computers"]);
  });

  it("drops an area none of whose pages is visible", () => {
    const labels = railLabels(held);
    expect(labels).not.toContain("Governance");
    expect(labels).not.toContain("Assets & Spend");
  });

  it("keeps items explicitly marked ungated, and nothing else, for no permissions", () => {
    expect(railLabels([])).toEqual(["Dashboard"]);
  });

  it("shows Platform status to an operator holding its code, and to nobody else", () => {
    const pages = (permissions: string[]) => searchDestinations(permissions).map((d) => d.label);
    expect(pages(["platform.status.read"])).toContain("Platform status");
    expect(pages(["devices.read", "jobs.read"])).not.toContain("Platform status");
  });

  it("never returns a group or area with nothing in it", () => {
    for (const group of visibleGroups(["devices.read"])) {
      expect(group.items.length).toBeGreaterThan(0);
      for (const entry of group.items) {
        if (isArea(entry)) expect(entry.sections.every((s) => s.items.length > 0)).toBe(true);
      }
    }
  });

  it("is not vacuous: an operator holding everything sees every page", () => {
    const groups = visibleGroups(EVERY_PERMISSION);
    expect(groups).toHaveLength(NAV_GROUPS.length);
    expect(navItems(groups)).toHaveLength(ALL_ITEMS.length);
  });
});

describe("search destinations", () => {
  it("offers every visible page, including the ones inside areas", () => {
    const found = searchDestinations(EVERY_PERMISSION);
    expect(found.map((d) => d.to)).toEqual(
      expect.arrayContaining(["/devices", "/admin", "/notifications", "/jobs", "/licenses"]),
    );
    expect(found.find((d) => d.to === "/jobs")?.within).toBe("Admin");
  });

  it("offers nothing the rail would hide", () => {
    const found = searchDestinations(["devices.read"]).map((d) => d.to);
    expect(found).not.toContain("/jobs");
    expect(found).not.toContain("/governance");
  });
});

describe("NAV_UNLISTED", () => {
  it("is not empty, so the guards below are not vacuous", () => {
    expect(NAV_UNLISTED.length).toBeGreaterThan(0);
  });

  it.each(NAV_UNLISTED.map((entry) => [entry.to, entry.under] as const))(
    "%s sits under a rail entry that exists: %s",
    (to, under) => {
      expect(RAIL.map((entry) => entry.to), `${to} claims ${under}`).toContain(under);
      const crumb = crumbFor(to);
      expect(crumb.sectionTo).toBe(under);
    },
  );

  it("never lists a route the model already covers", () => {
    const named = new Set([...ALL_ITEMS, ...navAreas()].map((item) => item.to));
    expect(NAV_UNLISTED.filter((entry) => named.has(entry.to))).toEqual([]);
  });

  it("keeps a person's page reachable, under Devices, with People off the rail", () => {
    // Operator decision 2026-09-23: People is not a menu entry; a device's
    // holder is set from the device. Licences, seats and devices still link
    // to a person, so the crumb has to name somewhere real.
    expect(ALL_ITEMS.map((item) => item.to)).not.toContain("/people");
    expect(crumbFor("/people/p1")).toEqual({
      section: "Devices",
      sectionTo: "/devices",
      title: "Person",
    });
    expect(crumbFor("/people/unmatched").title).toBe("Needs a person");
    expect(activeNavTo("/people/p1", visibleGroups(EVERY_PERMISSION))).toBe("/devices");
  });

  it("makes Add a computer the one install entry point, under Devices", () => {
    expect(ADD_COMPUTER_TO).toBe("/devices/onboard");
    expect(crumbFor(ADD_COMPUTER_TO)).toEqual({
      section: "Devices",
      sectionTo: "/devices",
      title: "Add a computer",
    });
  });
});

describe("ORPHAN_SECTION", () => {
  it("names routes that are in no section, and only those", () => {
    const paths = Object.keys(ORPHAN_SECTION);
    expect(paths.length).toBeGreaterThan(0);
    const named = new Set([
      ...[...ALL_ITEMS, ...navAreas()].map((item) => item.to),
      ...NAV_UNLISTED.map((entry) => entry.to),
    ]);
    expect(paths.filter((path) => named.has(path))).toEqual([]);
  });

  it("gives each one a name and no link", () => {
    for (const [path, label] of Object.entries(ORPHAN_SECTION)) {
      const crumb = crumbFor(path);
      expect(crumb.section).toBe(label);
      expect(crumb.sectionTo).toBeUndefined();
    }
  });
});

describe("crumbFor", () => {
  it("names a daily page after itself, linked to itself", () => {
    expect(crumbFor("/devices")).toEqual({
      section: "Devices",
      sectionTo: "/devices",
      title: "Devices",
    });
  });

  it("keeps a child route under its parent", () => {
    expect(crumbFor("/devices/9f2c-aa11")).toEqual({
      section: "Devices",
      sectionTo: "/devices",
      title: "Devices",
    });
  });

  it("puts an area's page under the area, linked to its landing page", () => {
    expect(crumbFor("/notifications")).toEqual({
      section: "Admin",
      sectionTo: "/admin",
      title: "Notifications",
    });
    // An Assets & Spend page is a rail item of its own now, so it is named
    // after itself, like a daily page.
    expect(crumbFor("/ipam/subnets/s1")).toEqual({
      section: "IP addresses",
      sectionTo: "/ipam",
      title: "IP addresses",
    });
  });

  it("names an area's landing page after the area", () => {
    expect(crumbFor("/admin")).toEqual({ section: "Admin", sectionTo: "/admin", title: "Admin" });
  });

  it("lets the longest match win across layers", () => {
    // `/assistant` is a daily item and `/assistant/autonomy` an Admin page.
    expect(crumbFor("/assistant/autonomy").section).toBe("Admin");
    expect(crumbFor("/assistant/actions").section).toBe("Assistant");
  });

  it("resolves an unlisted page's own child routes", () => {
    expect(crumbFor("/agent-push/credential-scopes")).toEqual({
      section: "Admin",
      sectionTo: "/admin",
      title: "Push install",
    });
    expect(crumbFor("/agent-push/authorizations").title).toBe("Push install");
  });

  it("draws a bare crumb for a route in no model at all", () => {
    expect(crumbFor("/no-such-page")).toEqual({
      section: undefined,
      sectionTo: undefined,
      title: "ITMS",
    });
  });

  it("gives every page a section that links to a rail entry", () => {
    const rail = RAIL.map((entry) => entry.to);
    for (const item of ALL_ITEMS) {
      const crumb = crumbFor(item.to);
      expect(crumb.title).toBe(item.label);
      expect(rail, `${item.to}'s section links to ${String(crumb.sectionTo)}`).toContain(
        crumb.sectionTo,
      );
    }
  });
});

describe("activeNavTo", () => {
  const visible = visibleGroups(EVERY_PERMISSION);

  it("lights the area on any of its pages", () => {
    expect(activeNavTo("/notifications", visible)).toBe("/admin");
    expect(activeNavTo("/changes/chg-1", visible)).toBe("/changes");
  });

  it("lights one entry, the most specific one", () => {
    expect(activeNavTo("/assistant/autonomy", visible)).toBe("/admin");
    expect(activeNavTo("/devices/onboard", visible)).toBe("/devices");
    expect(activeNavTo("/software", visible)).toBe("/deploy");
  });

  it("falls back to the parent when the specific page is hidden", () => {
    expect(activeNavTo("/assistant/autonomy", visibleGroups(["ai.use"]))).toBe("/assistant");
  });

  it("does not match a sibling that merely shares a prefix", () => {
    expect(activeNavTo("/devices-archive", visible)).toBeUndefined();
  });
});

/* A REQUESTER IS NOT AN OPERATOR WITH FEWER LINKS. */
describe("portal-only accounts", () => {
  const requester = ["portal.ticket.read", "portal.ticket.write"];

  it("recognises an account holding only portal codes", () => {
    expect(isPortalOnly(requester)).toBe(true);
    expect(isPortalOnly([])).toBe(false);
    expect(isPortalOnly([...requester, "ticket.read"])).toBe(false);
  });

  it("gets the portal's destinations and no operator group", () => {
    const groups = visibleGroups(requester);
    expect(navItems(groups).map((item) => item.to)).toEqual(["/portal", "/portal/new"]);
    expect(groups.map((group) => group.label)).not.toContain("Daily");
  });

  it("marks New ticket, not My tickets, as current on /portal/new", () => {
    expect(activeNavTo("/portal/new", visibleGroups(requester))).toBe("/portal/new");
    expect(activeNavTo("/portal/tickets/t1", visibleGroups(requester))).toBe("/portal");
  });

  it("names portal pages without an operator section to link to", () => {
    expect(portalCrumbFor("/portal")).toEqual({ title: "My tickets" });
    expect(portalCrumbFor("/portal/new")).toEqual({ title: "New ticket" });
    expect(portalCrumbFor("/portal/tickets/t1")).toEqual({ title: "My tickets" });
  });
});
