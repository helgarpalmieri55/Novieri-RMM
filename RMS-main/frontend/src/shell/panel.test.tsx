/**
 * WHY THIS FILE EXISTS. A panel title used to be a styled `div`. It looked
 * exactly right and it broke the document outline on every page that carried a
 * panel: the page renders an `h1`, empty states and sub-sections render `h3`s,
 * and with no `h2` between them the outline jumps a level. The browser
 * accessibility pass measured that as axe `heading-order` (WCAG 1.3.1) at all
 * six viewport/state combinations it exercised -- and no jsdom test saw it,
 * because each assertion looked at one component at a time and the defect is a
 * property of the whole document.
 *
 * The change is semantic only. That is the part a future reader is most likely
 * to undo by "tidying" the element back to a div, so both halves are pinned
 * here: it IS a heading, and it carries no styling of its own that the class
 * did not already carry.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Panel } from "./components";

describe("Panel", () => {
  it("renders its title as a level-2 heading, the rung between the page h1 and any h3", () => {
    render(<Panel title="Recent jobs">body</Panel>);
    const heading = screen.getByRole("heading", { level: 2, name: "Recent jobs" });
    expect(heading.tagName).toBe("H2");
  });

  it("keeps the styling on the class, so the element carries no inline size or margin", () => {
    render(<Panel title="Recent jobs">body</Panel>);
    const heading = screen.getByRole("heading", { level: 2, name: "Recent jobs" });
    expect(heading).toHaveClass("panel-title");
    expect(heading.getAttribute("style")).toBeNull();
  });

  it("renders no heading at all when the panel has no title", () => {
    render(<Panel>body</Panel>);
    expect(screen.queryByRole("heading")).toBeNull();
  });

  it("still renders the subtitle beside it, which is not a heading", () => {
    render(
      <Panel title="Recent jobs" subtitle="Last 24 hours">
        body
      </Panel>,
    );
    expect(screen.getByText("Last 24 hours")).toBeInTheDocument();
    expect(screen.getAllByRole("heading")).toHaveLength(1);
  });
});
