// `NUMERIC_FIELDS` USED TO BE TWO BYTE-IDENTICAL COPIES, one in
// `../settings/HealthTab.tsx` and one in `HealthSettingsPanel.tsx` --
// the exact failure mode `docs/design/settings-language.md` names, and the
// one already fixed for the notification event-type labels BY DELETION
// (`../notification/labels.ts`). This file is that fix for Health: one
// definition, imported by both surfaces. The regression this guards against
// is silent -- nothing breaks if a future edit "just updates one of them",
// it only drifts -- so this pins both the CONTENT and the fact that both
// surfaces import it rather than carrying their own copy.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { NUMERIC_FIELDS } from "../fields";

describe("the Health field table", () => {
  it("has all eight fields, each with a label, help and kind", () => {
    expect(NUMERIC_FIELDS).toHaveLength(8);
    for (const field of NUMERIC_FIELDS) {
      expect(field.label.length).toBeGreaterThan(0);
      expect(field.help.length).toBeGreaterThan(0);
      expect(["duration", "count"]).toContain(field.kind);
    }
  });

  it("marks the two non-duration fields, and only those, as `count`", () => {
    // `flap_threshold` (a count of alerts) and `alert_retention_days` (a
    // count of days, not a duration -- see the file's own header comment on
    // why running it through the seconds-based control would corrupt it).
    const counts = NUMERIC_FIELDS.filter((field) => field.kind === "count").map((field) => field.key);
    expect(counts.sort()).toEqual(["alert_retention_days", "flap_threshold"]);
  });

  it("names the unit for `alert_retention_days`, the one count field the old label's parenthetical used to carry", () => {
    // Defect: the language pass rewrote `Alert retention (days)` to `Delete
    // closed alerts after` and nothing replaced what the parenthetical said
    // -- neither the accessible name nor the description named "days"
    // anywhere. `unit` is what puts it back, and both the label going
    // unchanged and the help actually saying "days" are pinned here so a
    // future edit cannot silently drop either half.
    const retention = NUMERIC_FIELDS.find((field) => field.key === "alert_retention_days");
    expect(retention?.label).toBe("Delete closed alerts after");
    expect(retention?.unit).toBe("days");
    expect(retention?.help).toMatch(/\bdays\b/);
  });

  it("gives `flap_threshold` no `unit` -- its own help already names what it counts", () => {
    const threshold = NUMERIC_FIELDS.find((field) => field.key === "flap_threshold");
    expect(threshold?.unit).toBeUndefined();
    expect(threshold?.help).toMatch(/alerts/);
  });

  it("is imported by both surfaces, not redefined by either", () => {
    const healthTab = readFileSync(
      resolve(import.meta.dirname, "../../settings/HealthTab.tsx"),
      "utf8",
    );
    const settingsPanel = readFileSync(resolve(import.meta.dirname, "../HealthSettingsPanel.tsx"), "utf8");

    expect(healthTab).toMatch(/import\s*\{\s*NUMERIC_FIELDS\s*\}\s*from\s*["']\.\.\/health\/fields["']/);
    expect(settingsPanel).toMatch(/import\s*\{\s*NUMERIC_FIELDS\s*\}\s*from\s*["']\.\/fields["']/);

    // Neither file declares its own `NUMERIC_FIELDS` -- `const NUMERIC_FIELDS`
    // (as opposed to importing the name) is exactly the duplication this file
    // exists to remove.
    expect(healthTab).not.toMatch(/const\s+NUMERIC_FIELDS/);
    expect(settingsPanel).not.toMatch(/const\s+NUMERIC_FIELDS/);
  });
});
