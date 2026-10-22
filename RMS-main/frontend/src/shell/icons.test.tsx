/**
 * `Icon` renders Tabler outline glyphs from the sprite in `./tabler.ts`, one
 * console-facing alias at a time. Three things can silently break this:
 *
 * 1. An alias the console actually uses (in nav, or anywhere else) pointing
 *    at a Tabler id the sprite does not carry -- the icon simply vanishes.
 * 2. The rendered `<svg>` drifting off the design's contract (`fill="none"`,
 *    `stroke="currentColor"`, `stroke-width="1.75"`) -- a filled or
 *    differently-stroked glyph reads as a bug even though every path is
 *    correct.
 * 3. An unknown name failing loudly (throwing) or silently in a way nobody
 *    notices (warning every render, flooding the console).
 */
import { render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Icon, ICON_NAMES } from "./icons";
import { navAreas, navItems } from "./nav";
import { TABLER } from "./tabler";

const NAV_ICON_NAMES = [
  ...new Set([...navItems(), ...navAreas()].map((item) => item.icon)),
];

describe("Icon", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it.each(ICON_NAMES)("resolves alias/id %s to a non-empty glyph", (name) => {
    const { container } = render(<Icon name={name} />);
    const svg = container.querySelector("svg");
    expect(svg).not.toBeNull();
    expect(svg?.innerHTML.trim()).not.toBe("");
  });

  it.each(NAV_ICON_NAMES)("resolves the nav icon %s", (name) => {
    const { container } = render(<Icon name={name} />);
    const svg = container.querySelector("svg");
    expect(svg).not.toBeNull();
    expect(svg?.innerHTML.trim()).not.toBe("");
  });

  it("renders the Tabler contract: fill none, currentColor stroke, 1.75 stroke width", () => {
    const { container } = render(<Icon name="devices" />);
    const svg = container.querySelector("svg");
    expect(svg).toHaveAttribute("fill", "none");
    expect(svg).toHaveAttribute("stroke", "currentColor");
    expect(svg).toHaveAttribute("stroke-width", "1.75");
    expect(svg).toHaveAttribute("stroke-linecap", "round");
    expect(svg).toHaveAttribute("stroke-linejoin", "round");
    expect(svg).toHaveAttribute("viewBox", "0 0 24 24");
    expect(svg).toHaveAttribute("aria-hidden", "true");
  });

  it("carries the base 'ic' class the design's icon rule targets", () => {
    const { container } = render(<Icon name="devices" />);
    expect(container.querySelector("svg")).toHaveClass("ic");
  });

  it("renders an alias to the same paths as the Tabler id it resolves to", () => {
    const { container } = render(<Icon name="warning" />);
    const svg = container.querySelector("svg");
    const rendered = [...(svg?.querySelectorAll("path") ?? [])].map((p) => p.getAttribute("d"));
    const alertTriangle = TABLER["alert-triangle"] ?? "";
    const expected = [...alertTriangle.matchAll(/d="([^"]*)"/g)].map((m) => m[1]);
    expect(rendered).toEqual(expected);
  });

  it("renders nothing for an unknown name", () => {
    const { container } = render(<Icon name="not-a-real-icon" />);
    expect(container.querySelector("svg")).toBeNull();
  });

  it("warns once for an unknown name, not once per render", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    render(<Icon name="still-not-a-real-icon" />);
    render(<Icon name="still-not-a-real-icon" />);
    render(<Icon name="still-not-a-real-icon" />);
    expect(warn).toHaveBeenCalledTimes(1);
  });
});
