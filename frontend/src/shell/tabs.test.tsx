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
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

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

/**
 * A STRIP THAT SCROLLS SHOWS ITS SELECTED TAB (Task 5 review, fix round 1):
 * `/clients/<id>?tab=alerts` at 390px opened on Summary to Tickets with no
 * selected tab in sight. jsdom lays nothing out, so the boxes are given: a
 * 300px strip, and tabs 100px wide side by side, moved by the strip's scroll.
 */
describe("Tabs that scroll", () => {
  const scrolled = new WeakMap<Element, number>();
  const WIDE = [
    { key: "summary", label: "Summary" },
    { key: "sites", label: "Sites" },
    { key: "contacts", label: "Contacts" },
    { key: "devices", label: "Devices" },
    { key: "tickets", label: "Tickets" },
    { key: "alerts", label: "Alerts" },
  ];

  function layOut() {
    vi.spyOn(HTMLElement.prototype, "scrollLeft", "get").mockImplementation(function (this: HTMLElement) {
      return scrolled.get(this) ?? 0;
    });
    vi.spyOn(HTMLElement.prototype, "scrollLeft", "set").mockImplementation(function (this: HTMLElement, value: number) {
      scrolled.set(this, value);
    });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      if (this.getAttribute("role") === "tablist") return DOMRect.fromRect({ x: 0, y: 0, width: 300, height: 40 });
      const strip = this.parentElement as HTMLElement;
      const index = Array.from(strip.children).indexOf(this);
      return DOMRect.fromRect({ x: index * 100 - (scrolled.get(strip) ?? 0), y: 0, width: 100, height: 40 });
    });
  }

  afterEach(() => vi.restoreAllMocks());

  it("brings the selected tab into view when it opens past the strip's edge", () => {
    layOut();
    render(<Tabs items={WIDE} active="alerts" onSelect={() => {}} scroll label="Client" />);
    const strip = screen.getByRole("tablist", { name: "Client" });
    // Alerts spans 500-600; the strip shows 0-300: scrolled by 300 plus the 16px margin.
    expect(strip.scrollLeft).toBe(316);
  });

  it("leaves the strip alone when the selected tab is already in view, and a strip that wraps", () => {
    layOut();
    const { unmount } = render(<Tabs items={WIDE} active="sites" onSelect={() => {}} scroll label="Client" />);
    expect(screen.getByRole("tablist", { name: "Client" }).scrollLeft).toBe(0);
    unmount();
    render(<Tabs items={WIDE} active="alerts" onSelect={() => {}} label="Client" />);
    expect(screen.getByRole("tablist", { name: "Client" }).scrollLeft).toBe(0);
  });
});

/**
 * A TAB'S STATE BESIDE ITS NAME (kit requests batch 3, B3-1; O7's K1):
 * Settings' "Set up" / "Not set up" was folded into the label ("Email · Set
 * up"), because a tab had no third slot.
 */
describe("a tab's note", () => {
  const SETTINGS = [
    { key: "organization", label: "Organization" },
    { key: "email", label: "Email", note: "Set up" },
  ];

  it("is drawn after the label, apart from it, in its own quieter span", () => {
    render(<Tabs items={SETTINGS} active="organization" onSelect={() => {}} label="Settings" />);
    const email = screen.getByRole("tab", { name: "Email Set up" });
    const note = email.querySelector(".tab-note");
    expect(note).toHaveTextContent("Set up");
    // The label is still the tab's own first text, not "Email · Set up".
    expect(email.firstChild?.textContent).toBe("Email");
  });

  it("draws nothing for a tab without one", () => {
    render(<Tabs items={SETTINGS} active="organization" onSelect={() => {}} label="Settings" />);
    expect(screen.getByRole("tab", { name: "Organization" }).querySelector(".tab-note")).toBeNull();
  });

  it("is quieter than the label: muted, the small step, regular weight (components.css)", () => {
    const css = readFileSync(resolve(import.meta.dirname, "./components.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
    const rule = /(?:^|[},])\s*\.tab-note\s*\{([^}]*)\}/m.exec(css)?.[1] ?? "";
    expect(rule).toMatch(/color:\s*var\(--muted\)/);
    expect(rule).toMatch(/font-size:\s*var\(--t-sm\)/);
    expect(rule).toMatch(/font-weight:\s*400/);
  });

  it("is on /__preview, so the browser's a11y pass measures it", () => {
    const preview = readFileSync(resolve(import.meta.dirname, "../preview/PreviewPage.tsx"), "utf8");
    expect(preview).toMatch(/note:\s*key === "b"/);
  });
});

/**
 * THE SELECTED TAB STAYS IN VIEW WHEN A LABEL BEFORE IT GROWS (kit requests
 * batch 3, B3-3; O7's K3). Settings on a phone: "Email · Set up" arrived after
 * the strip had scrolled Sessions into view, pushed it past the edge, and the
 * strip did not follow, because it re-scrolled only on a change of tab.
 * jsdom lays nothing out, so a tab here is 20px per character of its text, side
 * by side in a 300px strip, moved by the strip's scroll.
 */
describe("Tabs that scroll, when a label changes (B3-3)", () => {
  const scrolled = new WeakMap<Element, number>();

  function layOutByText() {
    vi.spyOn(HTMLElement.prototype, "scrollLeft", "get").mockImplementation(function (this: HTMLElement) {
      return scrolled.get(this) ?? 0;
    });
    vi.spyOn(HTMLElement.prototype, "scrollLeft", "set").mockImplementation(function (this: HTMLElement, value: number) {
      scrolled.set(this, value);
    });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      if (this.getAttribute("role") === "tablist") return DOMRect.fromRect({ x: 0, y: 0, width: 300, height: 40 });
      const strip = this.parentElement as HTMLElement;
      const before = Array.from(strip.children).slice(0, Array.from(strip.children).indexOf(this));
      const x = before.reduce((sum, tab) => sum + (tab.textContent ?? "").length * 20, 0);
      return DOMRect.fromRect({ x: x - (scrolled.get(strip) ?? 0), y: 0, width: (this.textContent ?? "").length * 20, height: 40 });
    });
  }

  afterEach(() => vi.restoreAllMocks());

  const settings = (email: string, note?: string) => [
    { key: "org", label: "Org" },
    { key: "email", label: email, ...(note === undefined ? {} : { note }) },
    { key: "sessions", label: "Sessions" },
  ];

  it("follows the selected tab when a label before it grows", () => {
    layOutByText();
    const { rerender } = render(<Tabs items={settings("Email")} active="sessions" onSelect={() => {}} scroll label="Settings" />);
    const strip = screen.getByRole("tablist", { name: "Settings" });
    // Sessions spans 160-320 in a 0-300 strip: scrolled by 20 plus the 16px margin.
    expect(strip.scrollLeft).toBe(36);
    rerender(<Tabs items={settings("Email · Set up")} active="sessions" onSelect={() => {}} scroll label="Settings" />);
    // "Email · Set up" is 280px: Sessions is at 340-500, 304-464 on screen; it follows by 164 + 16.
    expect(strip.scrollLeft).toBe(216);
  });

  it("follows it when a note arrives beside a tab before it", () => {
    layOutByText();
    const { rerender } = render(<Tabs items={settings("Email")} active="sessions" onSelect={() => {}} scroll label="Settings" />);
    const strip = screen.getByRole("tablist", { name: "Settings" });
    rerender(<Tabs items={settings("Email", "Set up")} active="sessions" onSelect={() => {}} scroll label="Settings" />);
    // "Email Set up" is 240px: Sessions is at 300-460, 264-424 on screen; it follows by 124 + 16.
    expect(strip.scrollLeft).toBe(176);
  });

  /*
   * ONLY THE TEXT THAT CAN MOVE THE SELECTED TAB (P3 adoption pass; batch 3
   * review, Minor 2). A tab after the selected one cannot push it anywhere, so
   * a count refetched there must not undo a person's sideways look along the
   * strip: the strip jumped back to the selected tab on every such change.
   * Its own text, and any tab's before it, still re-scroll (above).
   */
  const fleet = (users: number, own?: number) => [
    { key: "org", label: "Org" },
    { key: "email", label: "Email", ...(own === undefined ? {} : { count: own }) },
    { key: "sessions", label: "Sessions" },
    { key: "users", label: "Users", count: users },
  ];

  it("leaves a strip the person scrolled alone when a tab after the selected one changes", () => {
    layOutByText();
    const { rerender } = render(<Tabs items={fleet(3)} active="email" onSelect={() => {}} scroll label="Settings" />);
    const strip = screen.getByRole("tablist", { name: "Settings" });
    // Email spans 60-160 in a 0-300 strip: already in view.
    expect(strip.scrollLeft).toBe(0);
    // The person looks along the strip; Email is now off its left edge.
    strip.scrollLeft = 160;
    rerender(<Tabs items={fleet(4)} active="email" onSelect={() => {}} scroll label="Settings" />);
    expect(strip.scrollLeft).toBe(160);
  });

  it("still follows the selected tab when its own text changes", () => {
    layOutByText();
    const { rerender } = render(<Tabs items={fleet(3)} active="email" onSelect={() => {}} scroll label="Settings" />);
    const strip = screen.getByRole("tablist", { name: "Settings" });
    strip.scrollLeft = 160;
    rerender(<Tabs items={fleet(3, 2)} active="email" onSelect={() => {}} scroll label="Settings" />);
    // "Email 2" spans 60-200, -100-40 on screen: back by 100 + 16.
    expect(strip.scrollLeft).toBe(44);
  });
});
