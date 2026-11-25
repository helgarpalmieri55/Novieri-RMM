import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { renderPage } from "../test/renderPage";
import { NewMenu } from "./NewMenu";
import type { NewMenuEntry } from "./newMenu";

const ENTRIES: NewMenuEntry[] = [
  { kind: "ticket", to: "/service-desk?new=ticket", icon: "ticket" },
  { kind: "device", to: "/devices/onboard", icon: "devices" },
];

describe("NewMenu", () => {
  it("is a menu button in the primary voice, and draws nothing when there is nothing to create", () => {
    renderPage("/x", <NewMenu entries={ENTRIES} />);
    const button = screen.getByRole("button", { name: "New" });
    expect(button).toHaveAttribute("aria-haspopup", "menu");
    expect(button).toHaveClass("primary");
    const empty = renderPage("/y", <NewMenu entries={[]} />);
    expect(empty.container.querySelector("button")).toBeNull();
  });

  it("opens on the first entry, and each entry goes to its destination", async () => {
    const { router } = renderPage("/x", <NewMenu entries={ENTRIES} />);
    await userEvent.click(screen.getByRole("button", { name: "New" }));
    expect(screen.getByRole("menuitem", { name: "Ticket" })).toHaveFocus();
    await userEvent.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "Computer" })).toHaveFocus();
    await userEvent.keyboard("{Enter}");
    expect(router.state.location.pathname).toBe("/devices/onboard");
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("closes when focus moves on with Tab, as a menu button does (critique 2026-11-24, P2)", async () => {
    // The items are not tab stops (arrows move between them), so Tab leaves the
    // menu for whatever follows it in the bar, and the menu closes behind it.
    renderPage(
      "/x",
      <>
        <NewMenu entries={ENTRIES} />
        <input aria-label="After the menu" />
      </>,
    );
    await userEvent.click(screen.getByRole("button", { name: "New" }));
    expect(screen.getByRole("menuitem", { name: "Ticket" })).toHaveFocus();
    await userEvent.tab();
    expect(screen.getByRole("textbox", { name: "After the menu" })).toHaveFocus();
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("closes on Escape and gives focus back to the button", async () => {
    renderPage("/x", <NewMenu entries={ENTRIES} />);
    await userEvent.click(screen.getByRole("button", { name: "New" }));
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).toBeNull();
    expect(screen.getByRole("button", { name: "New" })).toHaveFocus();
  });
});
