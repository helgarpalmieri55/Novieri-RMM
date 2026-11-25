/**
 * THE DETECTOR'S SHARED IGNORES (`.impeccable/config.json`), each one a
 * reviewed false positive with its reason. An ignore is a decision someone
 * made about one finding; this pins the ones this project's waves asked for,
 * so a rewrite of the store that dropped one is a red test, not a silently
 * louder detector.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

interface IgnoreValue {
  rule: string;
  value: string;
  files?: string[];
  reason?: string;
}

const CONFIG = JSON.parse(readFileSync(resolve(import.meta.dirname, "../../../.impeccable/config.json"), "utf8")) as {
  detector: { ignoreValues: IgnoreValue[] };
};
const values = CONFIG.detector.ignoreValues;

describe("the detector's shared ignores", () => {
  it("still holds the ignores that predate this project", () => {
    // NON-VACUITY: the store was read, and a rewrite kept what was there.
    expect(values.map((entry) => `${entry.rule}=${entry.value}`)).toEqual(
      expect.arrayContaining(["side-tab=*", "overused-font=inter"]),
    );
  });

  /** Every `nested-cards` ignore, not the first one `find` would return (batch 2 review, H5). */
  const nested = values.filter((item) => item.rule === "nested-cards");

  it("scopes every nested-cards ignore to named files, each with its reason", () => {
    // A notice inside a card is a reviewed false positive page by page; an
    // unscoped ignore would silence the rule on every card in the console.
    expect(nested.length).toBeGreaterThan(0);
    for (const entry of nested) {
      expect(entry.files?.length, JSON.stringify(entry)).toBeGreaterThan(0);
      expect(entry.reason, JSON.stringify(entry)).toBeTruthy();
    }
  });

  it("ignores nested-cards on the consent page's notice in an entry of its own, and nowhere else (kit requests batch 2, K6)", () => {
    // O8: the overlay's `nested-cards` on ConsentPage's `.notice.gold` is a
    // false positive: a notice is a message in the card, not a card in a card.
    const consent = nested.filter((item) => item.files?.includes("frontend/src/modules/auth/ConsentPage.tsx"));
    expect(consent).toHaveLength(1);
    expect(consent[0]?.value).toBe("*");
    expect(consent[0]?.files).toEqual(["frontend/src/modules/auth/ConsentPage.tsx"]);
    expect(consent[0]?.reason).toMatch(/a notice is a message, not a card/);
  });

  it("ignores nested-cards on Settings' sessions notice and the assistant's action preview, each alone (kit requests batch 3, B3-5)", () => {
    // O7: the remember-device risk notice inside its form section, and the
    // facts strip inside the "What will run" panel, are not cards in cards.
    const expected: [file: string, reason: RegExp][] = [
      ["frontend/src/modules/settings/SessionsTab.tsx", /a notice is a message, not a card/],
      ["frontend/src/modules/ai-automation/ActionPreview.tsx", /the design's summary row inside its panel, not a card/],
    ];
    for (const [file, reason] of expected) {
      const entries = nested.filter((item) => item.files?.includes(file));
      expect(entries, file).toHaveLength(1);
      expect(entries[0]?.value).toBe("*");
      expect(entries[0]?.files).toEqual([file]);
      expect(entries[0]?.reason).toMatch(reason);
    }
  });
});
