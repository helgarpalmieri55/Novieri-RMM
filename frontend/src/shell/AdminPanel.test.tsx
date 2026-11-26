import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { setActiveLanguage } from "../i18n/core";
import { renderPage } from "../test/renderPage";
import { Sidebar } from "./Sidebar";
import { navItems } from "./nav";

const EVERYTHING = navItems()
  .map((item) => item.permission)
  .filter((code): code is string => code !== null);

function rail(path: string) {
  return renderPage(path, <Sidebar permissions={EVERYTHING} collapsed={false} onToggleCollapsed={() => {}} orgName="Novieri" />);
}

describe("Admin, as Atera draws it (redesign design §1.1)", () => {
  it("is a button at the foot of the rail that swaps the rail for its panel, without navigating", async () => {
    const { router } = rail("/devices");
    await userEvent.click(screen.getByRole("button", { name: "Admin" }));
    expect(router.state.location.pathname).toBe("/devices");
    const panel = screen.getByRole("navigation", { name: "Admin" });
    expect(within(panel).getByRole("button", { name: "Back to the menu" })).toBeInTheDocument();
    expect(within(panel).getByRole("searchbox", { name: "Search Admin" })).toBeInTheDocument();
  });

  it("opens by itself on an Admin page, with that page's section open and the page current", () => {
    rail("/audit");
    const panel = screen.getByRole("navigation", { name: "Admin" });
    expect(within(panel).getByRole("button", { name: /Security/ })).toHaveAttribute("aria-expanded", "true");
    expect(within(panel).getByRole("button", { name: /Service desk/ })).toHaveAttribute("aria-expanded", "false");
    expect(within(panel).getByRole("link", { name: "Audit log" })).toHaveAttribute("aria-current", "page");
  });

  it("opens the section of an Admin page reached from outside the panel (critique 2026-11-24, P2)", async () => {
    // The panel stays mounted between two Admin pages; reached by a link it
    // does not own (search, an /admin card), the new page's section opens, as
    // Atera's panel does (walkthrough, line 343).
    const { router } = rail("/audit");
    await act(() => router.navigate("/notifications"));
    const panel = screen.getByRole("navigation", { name: "Admin" });
    expect(within(panel).getByRole("button", { name: /Service desk/ })).toHaveAttribute("aria-expanded", "true");
    expect(within(panel).getByRole("link", { name: "Notifications" })).toHaveAttribute("aria-current", "page");
  });

  it("opens no section on /admin, where no page is current", () => {
    // The coordinator's answer to the critique's question 3 (2026-11-25): on
    // the index no page is current, so the panel opens nothing for you.
    rail("/admin");
    const panel = screen.getByRole("navigation", { name: "Admin" });
    expect(within(panel).queryAllByRole("button", { expanded: true })).toEqual([]);
    // NON-VACUITY: the five sections are there, shut.
    expect(within(panel).getAllByRole("button", { expanded: false })).toHaveLength(5);
  });

  it("opens no section when opened from a page outside Admin", async () => {
    rail("/devices");
    await userEvent.click(screen.getByRole("button", { name: "Admin" }));
    const panel = screen.getByRole("navigation", { name: "Admin" });
    expect(within(panel).getByRole("button", { name: /Service desk/ })).toHaveAttribute("aria-expanded", "false");
    expect(within(panel).queryAllByRole("button", { expanded: true })).toEqual([]);
  });

  it("goes back to the main rail and stays on the page", async () => {
    const { router } = rail("/audit");
    await userEvent.click(screen.getByRole("button", { name: "Back to the menu" }));
    expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/audit");
  });

  it("finds a page by any part of its name, accents ignored", async () => {
    setActiveLanguage("es");
    try {
      rail("/settings");
      await userEvent.type(screen.getByRole("searchbox", { name: "Buscar en Administración" }), "auditoria");
      const panel = screen.getByRole("navigation", { name: "Administración" });
      expect(within(panel).getByRole("link", { name: "Registro de auditoría" })).toBeVisible();
      expect(within(panel).queryByRole("link", { name: "Configuración" })).toBeNull();
    } finally {
      setActiveLanguage("en");
    }
  });
});
