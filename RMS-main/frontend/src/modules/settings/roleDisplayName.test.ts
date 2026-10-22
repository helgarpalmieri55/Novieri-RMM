/**
 * The seeded roles read in the active language; every other role name reads as
 * it always did.
 *
 * `roleDisplayName` is called from components AND from helpers, so it asks the
 * catalogue (`enums.SeededRole.<name as stored>`) and falls back to the
 * capitalised, de-hyphenated derivation for a role an operator made. The
 * English entries are that derivation, so the English page did not change.
 */

import { afterEach, describe, expect, it } from "vitest";

import { setActiveLanguage } from "../../i18n/core";
import { en } from "../../i18n/en";
import { es } from "../../i18n/es";
import { roleChipName, roleDisplayName } from "./types";

afterEach(() => setActiveLanguage("en"));

/** The global roles migrations 0003 and 0028 insert, by the name they are stored under. */
const SEEDED = ["super-admin", "org-admin", "technician", "auditor", "read-only", "requester"];

describe("roleDisplayName", () => {
  it("reads the seeded roles as it always did in English", () => {
    expect(SEEDED.map(roleDisplayName)).toEqual([
      "Super admin",
      "Org admin",
      "Technician",
      "Auditor",
      "Read only",
      "Requester",
    ]);
  });

  it("has an English and a Spanish entry for each seeded role, under the stored name", () => {
    const english = en.enums.SeededRole as Record<string, string>;
    const spanish = es.enums.SeededRole as Record<string, string>;
    expect(Object.keys(english).sort()).toEqual([...SEEDED].sort());
    expect(Object.keys(spanish).sort()).toEqual([...SEEDED].sort());
  });

  it("words the seeded roles in Spanish", () => {
    setActiveLanguage("es");
    expect(SEEDED.map(roleDisplayName)).toEqual([
      "Superadministrador",
      "Administrador de la organización",
      "Técnico",
      "Auditor",
      "Solo lectura",
      "Solicitante",
    ]);
  });

  it("leaves a role an operator made as it was, in either language", () => {
    expect(roleDisplayName("warehouse-lead")).toBe("Warehouse lead");
    setActiveLanguage("es");
    expect(roleDisplayName("warehouse-lead")).toBe("Warehouse lead");
    expect(roleDisplayName("  ")).toBe("  ");
  });
});

describe("roleChipName (the shell chip, the person page's sign-in roles)", () => {
  it("keeps the role exactly as stored in English, whatever it is", () => {
    expect(SEEDED.map(roleChipName)).toEqual(SEEDED);
    expect(roleChipName("warehouse-lead")).toBe("warehouse-lead");
    expect(roleChipName("")).toBe("");
  });

  it("words a seeded role in Spanish and leaves any other role as its author named it", () => {
    setActiveLanguage("es");
    expect(SEEDED.map(roleChipName)).toEqual([
      "Superadministrador",
      "Administrador de la organización",
      "Técnico",
      "Auditor",
      "Solo lectura",
      "Solicitante",
    ]);
    expect(roleChipName("warehouse-lead")).toBe("warehouse-lead");
    expect(roleChipName("constructor")).toBe("constructor");
  });
});
