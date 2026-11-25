import { describe, expect, it } from "vitest";

import { es } from "./es";
import { leaves } from "./test-utils";

/**
 * THE MACHINE LIST IS «EQUIPOS» (decision D13). The rail and the page name it
 * so since the redesign, and a sentence that sends someone to «Dispositivos»
 * names a place that is not on the screen. Spanish does not capitalise a
 * common noun inside a sentence, so a capital «Dispositivos» after another word
 * is the old page name; at the start of a value it is the ordinary noun (a
 * column, a caption) and is left alone.
 */
const OLD_PAGE_NAME = /\S\s+Dispositivos\b/;

describe("Spanish page names", () => {
  it("calls the machine list Equipos in the rail", () => {
    expect(es.shell.nav.item.devices).toBe("Equipos");
  });

  it("never sends anyone to «Dispositivos» inside a sentence", () => {
    const stale = [...leaves(es)].filter(([, value]) => OLD_PAGE_NAME.test(value)).map(([path, value]) => `${path}: ${value}`);
    expect(stale, stale.join("\n")).toEqual([]);
  });

  it("the check finds the old name mid-sentence and leaves the noun at the start alone", () => {
    expect(OLD_PAGE_NAME.test("Apruébalo en Dispositivos para incluirlo.")).toBe(true);
    expect(OLD_PAGE_NAME.test("Dispositivos en línea")).toBe(false);
    expect(OLD_PAGE_NAME.test("Apruébalo en Equipos para incluirlo.")).toBe(false);
  });
});
