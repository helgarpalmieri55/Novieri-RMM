/**
 * `reportProse`: the server's report prose as a person reads it (P3 adoption
 * pass, coordinator territory D: `reportText.ts:108`).
 *
 * The definitions write ` -- ` where a sentence takes an aside, and this used
 * to promote it to an em dash, which DESIGN.md bars from UI text. It is UI
 * text, not file content: only the catalogue cards and the report page call
 * it, and the CSV a person downloads is the server's own bytes. The Spanish
 * the server sends already reads without one, a pair as brackets and a single
 * one as a colon (`backend/app/core/i18n/es.py`); the English now does the same.
 */
import { describe, expect, it } from "vitest";

import { reportProse } from "./reportText";

describe("reportProse", () => {
  it("turns an aside between two double hyphens into brackets", () => {
    expect(
      reportProse(
        "Tickets with no SLA row at all -- no policy matched, or the calendar is misconfigured -- stay in the ticket count.",
      ),
    ).toBe("Tickets with no SLA row at all (no policy matched, or the calendar is misconfigured) stay in the ticket count.");
  });

  it("turns a single double hyphen into a colon", () => {
    expect(reportProse("Devices with no agent are reported as stale -- so the device total is the fleet.")).toBe(
      "Devices with no agent are reported as stale: so the device total is the fleet.",
    );
  });

  it("pairs only within a sentence", () => {
    expect(reportProse("One -- two. Three -- four.")).toBe("One: two. Three: four.");
  });

  it("never draws an em dash, and still strips backticks", () => {
    const text = reportProse("A -- b -- c -- d at `meta.generated_at`.");
    expect(text).not.toContain("—");
    expect(text).not.toContain(" -- ");
    expect(text).toBe("A (b) c: d at meta.generated_at.");
  });

  it("leaves prose without either alone", () => {
    expect(reportProse("Computed live; it is a statement about the record.")).toBe(
      "Computed live; it is a statement about the record.",
    );
  });
});
