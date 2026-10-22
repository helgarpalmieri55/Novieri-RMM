/**
 * The shared form-field class, and why it is not the search one.
 *
 * `docs/design/itms-prototype.html` defines THREE input classes that differ in
 * exactly one respect (`shell/components.css`): `.search-field`,
 * `.select-field` and `.input-field` share every border, height, radius and
 * focus rule, and only `.search-field` adds `min-width: 230px; flex: 1`.
 *
 * That geometry is right for a search bar sitting in a flex filter row and
 * wrong everywhere else. `inputClass` pointed at `.search-field` and is used by
 * ~300 form fields, so every field in every dialog was laid out as though it
 * were a search bar — reported from the first live deployment as the job
 * dispatch dialog "not matching the page style".
 *
 * Real search inputs are unaffected: they carry `className="search-field"`
 * literally (see `modules/devices/DevicesPage.tsx`), never through this
 * constant.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { inputClass } from "./ui";

describe("inputClass", () => {
  it("is the form field class, not the search one", () => {
    expect(inputClass).toBe("input-field");
    expect(inputClass).not.toBe("search-field");
  });

  it("names a class the stylesheet actually defines", () => {
    // Without this, pointing inputClass at a typo'd or absent class would
    // silently strip the border, height and focus ring from every form field
    // in the application, and no test that renders a component would notice —
    // jsdom does not apply stylesheets.
    const css = readFileSync(
      join(__dirname, "..", "shell", "components.css"),
      "utf8",
    );
    expect(css).toContain(`.${inputClass}`);
  });

  it("does not inherit the search bar's flex geometry", () => {
    // The one rule that separates the three classes. If `.input-field` ever
    // gains it, form fields go back to stretching like search bars.
    const css = readFileSync(
      join(__dirname, "..", "shell", "components.css"),
      "utf8",
    );
    const ownRules = css
      .split("\n")
      .filter((line) => line.trimStart().startsWith(`.${inputClass} {`))
      .join("\n");
    expect(ownRules).not.toContain("flex: 1");
    expect(ownRules).not.toContain("min-width: 230px");
  });
});

/**
 * `.field.full` NEEDS A GRID, AND THE GRID NEEDS TO HOLD MORE THAN FIELDS.
 *
 * `.form-grid` was the fifth rule this port left behind (712bc8d): every
 * dialog in the product stacked its fields flush, because `.field`'s 6px gap
 * separates a caption from its own control and nothing separated one field
 * from the next. The rule is back; these are the two extensions the design
 * file could not supply, because its dialog bodies contain nothing but
 * `formField(...)` calls and this product's contain prose, callouts and a
 * combobox as well.
 *
 * jsdom applies no stylesheet, so nothing here says a dialog LOOKS right.
 * What it says is that the rules a correct dialog names are present — the
 * same claim `base-styles.test.ts` makes, for the same reason.
 */
describe("the dialog grid's non-field children", () => {
  const css = () => readFileSync(join(__dirname, "..", "shell", "components.css"), "utf8");

  it("spans a paragraph across the grid rather than seating it in a cell", () => {
    // A hint or an intro in a 50% cell reads as a caption for the field
    // beside it. Every prose element in these dialogs is a <p>.
    expect(css()).toContain(".form-grid > p,");
  });

  it("spans `.notice` and anything explicitly marked `.full`", () => {
    const rules = css();
    expect(rules).toContain(".form-grid > .notice,");
    expect(rules).toContain(".form-grid > .full { grid-column: 1 / -1;");
  });

  it("spans `.picker` and drops the margin it carries for block flow", () => {
    // `.picker { margin-bottom: 16px }` is geometry for the block flow it was
    // written in. In the grid the gap does that job, and a leftover margin
    // makes the row holding a picker taller than the row beside it.
    const rule = css()
      .split("\n")
      .find((line) => line.trimStart().startsWith(".form-grid > .picker {"));
    expect(rule, "no `.form-grid > .picker` rule").toBeDefined();
    expect(rule).toContain("grid-column: 1 / -1");
    expect(rule).toContain("margin-bottom: 0");
  });
});
