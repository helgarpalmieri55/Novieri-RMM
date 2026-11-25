/**
 * WHY THIS FILE EXISTS.
 *
 * A consistency sweep across the console found 37 raw `<button>` elements
 * against 15 uses of this component, and 22 files that never imported it at
 * all. The reason was not laziness: `Btn` could not express four things the
 * design system documents or the pages genuinely need, so every caller that
 * needed one of them dropped out of the design system to get it — and took the
 * height, radius, weight, hover, focus ring and disabled treatment with it.
 *
 * Those four are pinned here, because a capability nobody can see is a
 * capability the next caller works around again.
 */
import { render, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { Btn } from "./components";

describe("Btn", () => {
  it("carries the ghost variant DESIGN.md documents and .btn.ghost implements", () => {
    render(<Btn label="Dismiss" variant="ghost" />);
    expect(screen.getByRole("button", { name: "Dismiss" })).toHaveClass("btn", "ghost");
  });

  it("can be disabled, so a pending action need not leave the design system", () => {
    render(<Btn label="Send wake" disabled />);
    expect(screen.getByRole("button", { name: "Send wake" })).toBeDisabled();
  });

  it("takes a name for assistive technology that the visible label cannot carry", () => {
    // Two hundred rows whose every action reads "Copy" is two hundred
    // identical buttons to a screen reader.
    render(<Btn label="Copy" ariaLabel="Copy full chain hash for event 41f2" />);
    expect(
      screen.getByRole("button", { name: "Copy full chain hash for event 41f2" }),
    ).toBeInTheDocument();
  });

  it("hides an icon-only button's label visually but never from the accessibility tree", () => {
    render(<Btn label="Delete row" icon="warning" iconOnly />);
    const button = screen.getByRole("button", { name: "Delete row" });
    expect(button).toHaveClass("icon-only");
    expect(button).not.toHaveTextContent("Delete row");
  });

  it("leaves aria-label off a normal button, where the visible text is the name", () => {
    render(<Btn label="Enroll device" />);
    expect(screen.getByRole("button", { name: "Enroll device" })).not.toHaveAttribute("aria-label");
  });

  it("still defaults to a plain secondary button", () => {
    render(<Btn label="Cancel" />);
    const button = screen.getByRole("button", { name: "Cancel" });
    expect(button).toHaveClass("btn");
    expect(button).not.toBeDisabled();
    expect(button.className.trim()).toBe("btn");
  });
});

/** A stylesheet with comments stripped, and one rule's body by selector. */
const css = (path: string) => readFileSync(resolve(import.meta.dirname, path), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
function rule(text: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\>]/g, "\\$&");
  return new RegExp(`(?:^|[},])\\s*${escaped}\\s*\\{([^}]*)\\}`, "m").exec(text)?.[1] ?? "";
}

describe("Btn wide (kit requests batch 2, K4)", () => {
  it("takes .wide when asked, so a step's one action can run across its card", () => {
    render(<Btn label="Sign in" variant="primary" wide />);
    expect(screen.getByRole("button", { name: "Sign in" })).toHaveClass("btn", "primary", "wide");
  });

  it("is not wide by default", () => {
    render(<Btn label="Sign in" variant="primary" />);
    expect(screen.getByRole("button", { name: "Sign in" })).not.toHaveClass("wide");
  });

  it("fills its container's width with its content centred (components.css)", () => {
    const wide = rule(css("./components.css"), ".btn.wide");
    expect(wide).toMatch(/width:\s*100%/);
    expect(wide).toMatch(/justify-content:\s*center/);
  });

  it("is on /__preview, so the browser's a11y pass measures it", () => {
    const preview = readFileSync(resolve(import.meta.dirname, "../preview/PreviewPage.tsx"), "utf8");
    expect(preview).toMatch(/<Btn\b[^>]*\bwide\b/);
  });
});
