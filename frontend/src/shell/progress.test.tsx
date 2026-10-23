/**
 * The progress bar, which said nothing to a screen reader at all.
 *
 * The browser pass, MEDIUM: `Progress` rendered
 * `<div class="progress"><span style="width:94%"/></div>` -- no role, no
 * `aria-valuenow`, no `aria-label`, no text. In the device table it is the
 * entire contents of the Patch column, so that cell was empty to assistive
 * technology and the only expression of "94%" was the pixel width of a fill.
 * WCAG 2.2 SC 1.1.1.
 *
 * WHY THIS FILE EXISTS AT ALL. jsdom could have caught this on any day of the
 * programme -- nothing about it needs a cascade or a layout. Nothing did,
 * because no test had ever been written against this primitive. That is the
 * gap being closed, not the browser's.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Progress } from "./components";

describe("Progress", () => {
  it("is a progress bar with a value, not an anonymous div", () => {
    render(<Progress value={94} label="Patch level, ws-alpha" />);
    const bar = screen.getByRole("progressbar", { name: "Patch level, ws-alpha" });
    expect(bar).toHaveAttribute("aria-valuenow", "94");
    expect(bar).toHaveAttribute("aria-valuemin", "0");
    expect(bar).toHaveAttribute("aria-valuemax", "100");
  });

  it("says the value in words as well as in numbers", () => {
    // `aria-valuenow` alone is announced by some screen readers as a bare
    // "94", which is a number without a unit.
    render(<Progress value={94} />);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuetext", "94%");
  });

  it("carries a name even when the caller forgot one", () => {
    // A progress bar with no accessible name is an axe `aria-progressbar-name`
    // failure. The default is deliberately dull and deliberately present.
    render(<Progress value={40} />);
    expect(screen.getByRole("progressbar", { name: "Progress" })).toBeInTheDocument();
  });

  it("clamps out-of-range values rather than drawing past the track", () => {
    const { container, rerender } = render(<Progress value={140} />);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "100");
    expect(container.querySelector(".progress > i")).toHaveStyle({ width: "100%" });

    rerender(<Progress value={-20} />);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "0");
    expect(container.querySelector(".progress > i")).toHaveStyle({ width: "0%" });
  });

  it("keeps drawing the unrounded value, so describing it did not redraw it", () => {
    const { container } = render(<Progress value={94.4} />);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "94");
    expect(container.querySelector(".progress > i")).toHaveStyle({ width: "94.4%" });
  });

  it("keeps the design's tone modifier", () => {
    const { container } = render(<Progress value={52} tone="red" />);
    expect(container.querySelector(".progress")).toHaveClass("red");
  });
});
