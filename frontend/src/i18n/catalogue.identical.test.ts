import { describe, expect, it } from "vitest";
import { en } from "./en";
import { es } from "./es";
import { IDENTICAL_OK, IDENTICAL_OK_SHARED, SHARED_NAMESPACES, WAVE_ENTRIES, WAVE_NAMESPACES } from "./identical-ok";
import { identicalLeaves, leaves, SAME_IN_BOTH } from "./test-utils";

/** expectNoEnglish skips a leaf that is identical in both catalogues, so an
 *  English value pasted into es/* would pass every Spanish page test. This is
 *  the audit that makes that skip safe. */
function unexempted(english: unknown, spanish: unknown, ok: Record<string, string>): string[] {
  const values = leaves(english);
  return identicalLeaves(english, spanish).filter((path) => {
    const value = values.get(path)!;
    return !SAME_IN_BOTH.includes(value) && !(path in ok);
  });
}

describe("identical English and Spanish catalogue values", () => {
  it("are all exempted, by SAME_IN_BOTH or by IDENTICAL_OK with a reason", () => {
    const offenders = unexempted(en, es, IDENTICAL_OK);
    expect(offenders, `identical in en and es, not exempted:\n${offenders.join("\n")}`).toEqual([]);
  });

  it("have no stale IDENTICAL_OK entry, and every entry has a reason", () => {
    const identical = new Set(identicalLeaves(en, es));
    const stale = Object.keys(IDENTICAL_OK).filter((path) => !identical.has(path));
    expect(stale, `IDENTICAL_OK entries no longer identical: ${stale.join(", ")}`).toEqual([]);
    for (const reason of Object.values(IDENTICAL_OK)) expect(reason.trim().length).toBeGreaterThan(10);
  });

  it("keeps each wave's entries in that wave's own file, and only the shared ones in identical-ok.ts (redesign plan, shared files)", () => {
    const namespace = (path: string) => path.split(".")[0] as string;
    for (const [wave, entries] of Object.entries(WAVE_ENTRIES)) {
      const foreign = Object.keys(entries).filter((path) => !(WAVE_NAMESPACES[wave] ?? []).includes(namespace(path)));
      expect(foreign, `identical-ok.${wave}.ts lists another wave's keys: ${foreign.join(", ")}`).toEqual([]);
    }
    const unshared = Object.keys(IDENTICAL_OK_SHARED).filter((path) => !SHARED_NAMESPACES.includes(namespace(path)));
    expect(unshared, `identical-ok.ts lists a wave's keys: ${unshared.join(", ")}`).toEqual([]);
    // Every catalogue has an owner, so a new identical key always has a file to go to.
    const owned = new Set([...SHARED_NAMESPACES, ...Object.values(WAVE_NAMESPACES).flat()]);
    expect(Object.keys(en).filter((name) => !owned.has(name))).toEqual([]);
  });

  it("the audit itself finds a pasted English value, and a plural form", () => {
    const english = { a: { save: "Save", ok: "Email", n: { one: "{count} item", other: "{count} items" } } };
    const spanish = { a: { save: "Save", ok: "Email", n: { one: "{count} elemento", other: "{count} items" } } };
    expect(unexempted(english, spanish, {})).toEqual(["a.save", "a.n#other"]);
    expect(unexempted(english, spanish, { "a.save": "a product name", "a.n#other": "a loanword" })).toEqual([]);
  });
});
