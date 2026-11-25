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

  it("ignores nested-cards on the consent page's notice, and nowhere else (kit requests batch 2, K6)", () => {
    // O8: the overlay's `nested-cards` on ConsentPage's `.notice.gold` is a
    // false positive: a notice is a message in the card, not a card in a card.
    const entry = values.find((item) => item.rule === "nested-cards");
    expect(entry?.value).toBe("*");
    expect(entry?.files).toEqual(["frontend/src/modules/auth/ConsentPage.tsx"]);
    expect(entry?.reason).toMatch(/a notice is a message, not a card/);
  });
});
