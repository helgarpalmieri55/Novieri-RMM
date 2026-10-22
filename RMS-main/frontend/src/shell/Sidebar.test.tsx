/**
 * The rail's current item. On a nested route more than one destination is a
 * prefix of the path (`/assistant` and `/assistant/autonomy`), and only the
 * most specific one is where the operator is. An area opens its pages under
 * itself while the operator is inside it.
 */

import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { navItems } from "./nav";
import { Sidebar } from "./Sidebar";

const EVERY_PERMISSION = navItems()
  .map((item) => item.permission)
  .filter((p): p is string => p !== null);

function renderAt(path: string, permissions: readonly string[] = EVERY_PERMISSION) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Sidebar
        permissions={permissions}
        collapsed={false}
        onToggleCollapsed={() => {}}
        orgName="Acme"
      />
    </MemoryRouter>,
  );
  return within(screen.getByRole("navigation", { name: "Main" }));
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

describe("the second layer", () => {
  it("lists only the rail entries while no area is open", () => {
    const nav = renderAt("/devices");
    expect(nav.queryByRole("link", { name: "Notifications" })).toBeNull();
    expect(nav.getByRole("link", { name: "Admin" })).toHaveAttribute("href", "/admin");
  });

  it("opens an area's pages under it while the operator is inside", () => {
    const nav = renderAt("/notifications");
    expect(nav.getByRole("link", { name: "Admin" })).toHaveClass("active");
    expect(nav.getByRole("link", { name: "Email intake" })).toHaveAttribute("href", "/email-intake");
    expect(nav.getByRole("link", { name: "Notifications" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    // Under their section headings, so an Admin page is not an anonymous
    // indented line.
    expect(nav.getByText("Service desk")).toHaveClass("grp", "sub");
    expect(nav.getByText("Computers")).toHaveClass("grp", "sub");
  });

  it("gives an open area's pages their own icons, like every other item", () => {
    const nav = renderAt("/notifications");
    expect(nav.getByRole("link", { name: "Email intake" }).querySelector("svg")).not.toBeNull();
  });

  it("shows the Governance and Assets & Spend pages without opening anything", () => {
    const nav = renderAt("/devices");
    expect(nav.getByRole("link", { name: "Changes" })).toHaveAttribute("href", "/changes");
    expect(nav.getByRole("link", { name: "Licenses" })).toHaveAttribute("href", "/licenses");
    expect(nav.getByRole("link", { name: "Changes" })).not.toHaveClass("sub");
  });

  it("opens only the pages this operator may reach", () => {
    const nav = renderAt("/enrollment", ["devices.read"]);
    expect(nav.getByRole("link", { name: "Device groups" })).toBeInTheDocument();
    expect(nav.queryByRole("link", { name: "Settings" })).toBeNull();
  });
});
