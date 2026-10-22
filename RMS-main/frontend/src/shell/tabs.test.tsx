/**
 * The tab strip's keyboard contract.
 *
 * `role="tablist"` and `aria-selected` were already here before this cycle.
 * What was missing is the behaviour those roles PROMISE: a tablist is one tab
 * stop, and the arrow keys move within it. A screen-reader user who hears
 * "tab, 1 of 5" and then has to press Tab five times to reach the panel has
 * been told a structure exists and handed a row of unrelated buttons.
 *
 * The audit filed the naked version of this against /budget — "five tabs are
 * plain buttons, role null, aria-selected null, parent role null... Arrow-key
 * navigation between tabs (the expected pattern) is unavailable" — and the
 * half-dressed version against /devices, which had the roles and not the
 * wiring.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { Tabs } from "./components";

const user = userEvent.setup();

const ITEMS = [
  { key: "all", label: "All devices", count: 12 },
  { key: "online", label: "Online", count: 9 },
  { key: "offline", label: "Offline", count: 3 },
];

function Harness({ panelId }: { panelId?: string }) {
  const [active, setActive] = useState("all");
  return (
    <>
      <Tabs items={ITEMS} active={active} onSelect={setActive} panelId={panelId} />
      <div id="panel" role="tabpanel">
        {active}
      </div>
    </>
  );
}

describe("Tabs", () => {
  it("is one tab stop, not one per tab", () => {
    render(<Harness />);
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((tab) => tab.getAttribute("tabindex"))).toEqual(["0", "-1", "-1"]);
  });

  it("moves selection with the arrow keys", async () => {
    render(<Harness />);
    screen.getByRole("tab", { name: /All devices/ }).focus();

    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: /Online/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: /Online/ })).toHaveFocus();

    await user.keyboard("{ArrowLeft}");
    expect(screen.getByRole("tab", { name: /All devices/ })).toHaveAttribute("aria-selected", "true");
  });

  it("wraps at both ends, because a tablist is a ring", async () => {
    render(<Harness />);
    screen.getByRole("tab", { name: /All devices/ }).focus();

    await user.keyboard("{ArrowLeft}");
    expect(screen.getByRole("tab", { name: /Offline/ })).toHaveAttribute("aria-selected", "true");

    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: /All devices/ })).toHaveAttribute("aria-selected", "true");
  });

  it("jumps to the ends with Home and End", async () => {
    render(<Harness />);
    screen.getByRole("tab", { name: /All devices/ }).focus();

    await user.keyboard("{End}");
    expect(screen.getByRole("tab", { name: /Offline/ })).toHaveAttribute("aria-selected", "true");

    await user.keyboard("{Home}");
    expect(screen.getByRole("tab", { name: /All devices/ })).toHaveAttribute("aria-selected", "true");
  });

  it("leaves other keys to the page", async () => {
    render(<Harness />);
    screen.getByRole("tab", { name: /All devices/ }).focus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("tab", { name: /All devices/ })).toHaveAttribute("aria-selected", "true");
  });

  it("names the panel when the page has one", () => {
    render(<Harness panelId="panel" />);
    expect(screen.getByRole("tab", { name: /Online/ })).toHaveAttribute("aria-controls", "panel");
  });

  it("omits aria-controls rather than pointing it at nothing", () => {
    // An `aria-controls` naming an element that does not exist is worse than
    // no `aria-controls`: the screen reader offers "move to controlled
    // element" and then lands nowhere.
    render(<Harness />);
    expect(screen.getByRole("tab", { name: /Online/ })).not.toHaveAttribute("aria-controls");
  });

  it("does not submit a form it happens to sit inside", async () => {
    // Every <button> in a <form> is a submit control unless it says otherwise,
    // which is how a filter tab becomes a save.
    let submitted = false;
    render(
      <form
        onSubmit={(event) => {
          event.preventDefault();
          submitted = true;
        }}
      >
        <Harness />
      </form>,
    );
    await user.click(screen.getByRole("tab", { name: /Online/ }));
    expect(submitted).toBe(false);
  });
});
