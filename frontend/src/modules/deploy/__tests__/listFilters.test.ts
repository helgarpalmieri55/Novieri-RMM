/**
 * The two pieces Packages and Scripts filter their loaded rows with, kept
 * once for both (review fix round 1): the System chips' platforms, and the
 * search's folding.
 */
import { describe, expect, it } from "vitest";

import { fold, OS_FILTERS } from "../listFilters";

describe("the deploy lists' filters", () => {
  it("offers the three platforms in the chips' order", () => {
    expect(OS_FILTERS).toEqual(["windows", "linux", "macos"]);
  });

  it("folds case, accents and outer spaces out of a search", () => {
    expect(fold("  Actualización de NOTEPAD++ ")).toBe("actualizacion de notepad++");
    expect(fold("Ñandú")).toBe("nandu");
  });
});
