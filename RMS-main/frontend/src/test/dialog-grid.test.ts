/**
 * EVERY DIALOG FIELD SITS IN A `.form-grid`, AND NOTHING ELSE CAN SAY SO.
 *
 * `.form-grid` is the design's dialog field container. It was left out of the
 * port (712bc8d), so `.field.full`'s `grid-column: 1 / -1` named a grid that
 * existed nowhere and every dialog in the product stacked its fields flush --
 * `.field`'s 6px gap is INTERNAL, between a caption and its own control, so
 * with no grid there was nothing at all between one input and the next
 * field's caption. Fifty-four dialogs across forty-one files were laid out
 * that way and no test saw it.
 *
 * WHAT THIS CAN AND CANNOT PROVE. jsdom applies no stylesheet and lays
 * nothing out, so no assertion anywhere in this suite can say a dialog LOOKS
 * right -- that claim belongs to the Chromium pass and to screenshots. What
 * IS testable is the structural property the layout rests on: a field inside
 * a dialog has a `.form-grid` ancestor. A field that does not is invisible in
 * jsdom, invisible to `tsc`, and flush against its neighbour in a browser.
 *
 * WHY IT READS SOURCE. Fifty-two dialogs cannot all be rendered -- most need
 * a query client, a router, a permission context and a server. Reading the
 * JSX is the only way to hold ALL of them rather than the handful some test
 * happens to mount.
 *
 * ITS ONE ASSUMPTION, STATED. Ancestry is read from indentation, because this
 * tree has no JSX parser available (`typescript@7` ships no compiler API).
 * The codebase is uniformly formatted, so this holds; a file that is not will
 * fail loudly here rather than pass quietly, which is the right direction for
 * a guard to be wrong in.
 *
 * ITS ONE BLIND SPOT, ALSO STATED, AND NOTHING ELSE COVERS IT. A dialog that
 * delegates its fields to a component defined elsewhere in the file --
 * `CalendarFields`, `PolicyFields`, `PromoteDialog` -- contains no `<Field>`
 * between its own `<Dialog>` and `</Dialog>` for this to find, so it is
 * skipped silently. Those grids were placed by hand and checked in Chromium;
 * they are NOT held by any assertion. A DOM-level guard that walked the real
 * rendered ancestry would cover them, and would cover only the handful of
 * dialogs some test already mounts -- worth adding, not yet written, and
 * claiming otherwise here would be worse than the gap.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const REPO = resolve(import.meta.dirname, "../../..");
const MODULES = resolve(REPO, "frontend/src/modules");

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = resolve(dir, name);
    if (statSync(path).isDirectory()) return name === "__tests__" ? [] : sources(path);
    return name.endsWith(".tsx") && !name.includes(".test.") ? [path] : [];
  });
}

const rel = (path: string) => path.slice(REPO.length + 1);

/** A field-shaped opening tag: `Field`'s wrapper, or the combobox that replaced one. */
const FIELD_TAG = /<(Field|EntityPicker)[\s/>]/;

const indent = (line: string | undefined) =>
  line === undefined ? 0 : line.length - line.trimStart().length;

/**
 * Does this line's JSX ancestry include a `.form-grid`?
 *
 * Walks upward taking the first line at each strictly-smaller indentation --
 * i.e. the parent, then its parent -- so a sibling `</div>` closing an
 * EARLIER grid is never mistaken for an enclosing one.
 */
function insideFormGrid(lines: readonly string[], at: number): boolean {
  let depth = indent(lines[at]);
  for (let i = at - 1; i >= 0 && depth > 0; i -= 1) {
    const line = lines[i] ?? "";
    if (line.trim() === "" || indent(line) >= depth) continue;
    if (line.includes("form-grid")) return true;
    depth = indent(line);
  }
  return false;
}

interface DialogBlock {
  file: string;
  line: number;
  body: readonly string[];
  offset: number;
}

function dialogs(path: string): DialogBlock[] {
  const lines = readFileSync(path, "utf8").split("\n");
  const found: DialogBlock[] = [];
  for (let i = 0; i < lines.length; i += 1) {
    // `\b`, not `[\s>]`: a multi-line opening tag puts `<Dialog` at the END
    // of its line, and a character class demanding one more character silently
    // skipped every dialog written that way -- a third of them.
    if (!/<Dialog\b/.test(lines[i] ?? "")) continue;
    const end = lines.findIndex((line, j) => j > i && line.includes("</Dialog>"));
    if (end < 0) continue;
    found.push({ file: path, line: i + 1, body: lines, offset: i });
    i = end;
  }
  return found;
}

const BLOCKS = sources(MODULES).flatMap(dialogs);

describe("dialog fields and the grid that separates them", () => {
  it("finds the dialogs, so the assertion below is not vacuous", () => {
    // If the scan ever stops matching -- a rename, a wrapper component -- it
    // would report zero dialogs and pass everything. That is the failure mode
    // of every guard of this shape, so the count is asserted first.
    expect(BLOCKS.length).toBeGreaterThan(40);
  });

  it.each(
    BLOCKS.map((block) => {
      const end = block.body.findIndex(
        (line, j) => j > block.offset && line.includes("</Dialog>"),
      );
      const fields: number[] = [];
      for (let i = block.offset; i <= end; i += 1) {
        if (FIELD_TAG.test(block.body[i] ?? "")) fields.push(i);
      }
      return [`${rel(block.file)}:${block.line}`, block.body, fields] as const;
    }).filter(([, , fields]) => fields.length > 1),
  )("%s seats every field in a `.form-grid`", (_where, lines, fields) => {
    const loose = fields
      .filter((i) => !insideFormGrid(lines, i))
      .map((i) => `line ${i + 1}: ${(lines[i] ?? "").trim()}`);
    expect(
      loose,
      "these fields have no `.form-grid` ancestor, so each one sits flush " +
        "against the next field's caption in a browser",
    ).toEqual([]);
  });
});
