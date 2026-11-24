/**
 * The rail's current item. On a nested route more than one destination is a
 * prefix of the path (`/assistant` and `/assistant/autonomy`), and only the
 * most specific one is where the operator is. On an Admin page the Admin
 * panel takes the main rail's place (redesign design §1.1, decision D10).
 */

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { navItems } from "./nav";
import { Sidebar } from "./Sidebar";

const EVERY_PERMISSION = navItems()
  .map((item) => item.permission)
  .filter((p): p is string => p !== null);

function renderAt(path: string, permissions: readonly string[] = EVERY_PERMISSION) {
  const { container } = render(
    <MemoryRouter initialEntries={[path]}>
      <Sidebar
        permissions={permissions}
        collapsed={false}
        onToggleCollapsed={() => {}}
        orgName="Acme"
      />
    </MemoryRouter>,
  );
  // THE WHOLE RAIL (redesign design §1.1): the main menu, the Admin panel
  // that replaces it on an Admin page, and the anchored Assistant and Admin.
  // The current item can be in any of the three now.
  return within(container.querySelector<HTMLElement>("#sidebar") as HTMLElement);
}

function currentAt(path: string, permissions?: readonly string[]) {
  return renderAt(path, permissions)
    .getAllByRole("link")
    .filter((link) => link.getAttribute("aria-current") === "page")
    .map((link) => link.getAttribute("title"));
}

describe("Sidebar current item", () => {
  it("marks only AI automation on /assistant/autonomy", () => {
    expect(currentAt("/assistant/autonomy")).toEqual(["AI automation"]);
  });

  it("keeps Devices current on Add a computer, which lives under it", () => {
    expect(currentAt("/devices/onboard")).toEqual(["Devices"]);
  });

  it("keeps the parent current on a detail route with no rail item of its own", () => {
    expect(currentAt("/devices/dev-1")).toEqual(["Devices"]);
  });

  it("does not match a sibling that merely shares a prefix", () => {
    expect(currentAt("/devices-archive")).toEqual([]);
  });

  it("falls back to the parent when the specific item is hidden", () => {
    expect(currentAt("/assistant/autonomy", ["ai.use"])).toEqual(["Assistant"]);
  });

  it("marks the area itself on its landing page", () => {
    expect(currentAt("/admin")).toEqual(["Admin"]);
  });
});

describe("the rail's folders and anchored entries (redesign design §1.1)", () => {
  // REPLACES "the second layer" (2026-09-23), whose Admin was a rail link
  // opening its pages under itself and whose Governance pages had no folder.
  // The two cases that still hold are kept, on the Admin panel now.
  it("opens the folder that holds the current page, and only that one", () => {
    const rail = renderAt("/subscriptions");
    expect(rail.getByRole("button", { name: "Assets" })).toHaveAttribute("aria-expanded", "true");
    expect(rail.getByRole("link", { name: "Subscriptions" })).toHaveAttribute("aria-current", "page");
    expect(rail.getByRole("button", { name: "More" })).toHaveAttribute("aria-expanded", "false");
  });

  it("opens and closes a folder in place", async () => {
    const rail = renderAt("/devices");
    const more = rail.getByRole("button", { name: "More" });
    await userEvent.click(more);
    expect(more).toHaveAttribute("aria-expanded", "true");
    expect(rail.getByRole("link", { name: "Changes" })).toBeVisible();
  });

  it("anchors the Assistant and Admin below the main rail, in their own navigation", () => {
    renderAt("/devices");
    const anchored = screen.getByRole("navigation", { name: "Assistant and Admin" });
    expect(within(anchored).getByRole("link", { name: "Assistant" })).toHaveAttribute("href", "/assistant");
    expect(within(anchored).getByRole("button", { name: "Admin" })).toBeInTheDocument();
  });

  it("marks the current page with its dot, which is decoration only", () => {
    const rail = renderAt("/devices");
    const current = rail.getByRole("link", { name: "Devices" });
    expect(current.querySelector(".nav-dot")).toHaveAttribute("aria-hidden", "true");
  });

  it("shows the brand: the symbol on its tile and the wordmark", () => {
    const rail = renderAt("/devices");
    expect(rail.getByRole("img", { name: "Novieri" })).toBeInTheDocument();
  });

  // Kept from "the second layer" (2026-09-23), on the Admin panel now.
  it("gives an open area's pages their own icons, like every other item", () => {
    const rail = renderAt("/notifications");
    expect(rail.getByRole("link", { name: "Email intake" }).querySelector("svg")).not.toBeNull();
  });

  // Kept from "the second layer" (2026-09-23), on the Admin panel now.
  it("opens only the pages this operator may reach", () => {
    const rail = renderAt("/enrollment", ["devices.read"]);
    expect(rail.getByRole("link", { name: "Device groups" })).toBeInTheDocument();
    expect(rail.queryByRole("link", { name: "Settings" })).toBeNull();
  });
});
