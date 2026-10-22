import { afterEach, describe, expect, it } from "vitest";

import { setActiveLanguage } from "../../i18n/core";
import { compactMoney } from "./money";

afterEach(() => setActiveLanguage("en"));

describe("compactMoney follows the language", () => {
  it("lower-cases the multiplier in English, as the design writes it", () => {
    expect(compactMoney("76010.40", "EUR")).toBe("€76k");
    expect(compactMoney("1200000", "EUR")).toBe("€1.2m");
  });

  it("leaves the multiplier alone in Spanish: a million is an upper-case M, never a lower-case m", () => {
    setActiveLanguage("es");
    const million = compactMoney("1200000");
    expect(million).toMatch(/1,2\s?M$/);
    expect(million).not.toMatch(/m$/);
    // With a currency the symbol comes last in Spanish, so there is nothing to lower-case either.
    expect(compactMoney("1200000", "EUR")).toMatch(/1,2\s?M\s?€$/);
    expect(compactMoney("76010.40", "EUR")).not.toMatch(/\bk\b/);
  });
});
