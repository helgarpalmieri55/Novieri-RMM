/**
 * WHY THIS FILE EXISTS. v3's own settings screen names its sections --
 * `<div style="font-weight:500"><icon> Maintenance window</div>` -- and a
 * styled div is exactly the defect `panel.test.tsx` already pins for `Panel`:
 * a screen reader has nothing to navigate by. `FieldGroup` is the same fix
 * one level down, inside a panel's body rather than at page level, so it
 * renders an `h3` -- a titled `Panel` has already spent the `h2` above it.
 *
 * The glyph next to the title is decorative -- it repeats what the words
 * already say -- so it must not join the heading's accessible name or a
 * screen reader announces "plug icon Connection" for a purely visual cue.
 * `Icon` already renders every glyph `aria-hidden="true"` unconditionally
 * (see `./icons.tsx`), so the second assertion below is also a guard against
 * that ever becoming conditional.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { FieldGroup } from "./components";

describe("FieldGroup", () => {
  it("names the group as a heading under the panel's own", () => {
    render(
      <FieldGroup title="Connection" icon="network">
        <p>x</p>
      </FieldGroup>,
    );
    // h3, because a titled Panel renders the h2 above it.
    expect(screen.getByRole("heading", { name: "Connection", level: 3 })).toBeInTheDocument();
  });

  it("hides the glyph from the accessible name", () => {
    render(
      <FieldGroup title="Connection" icon="network">
        <p>x</p>
      </FieldGroup>,
    );
    const heading = screen.getByRole("heading", { name: "Connection" });
    expect(heading.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });
});
