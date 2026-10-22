import { describe, expect, it } from "vitest";

import { initialsOf, personIndex } from "./people";

describe("initialsOf", () => {
  it("takes the first and last word, not the first two", () => {
    // `UsersTab` took the first two, so "Ada King Lovelace" was "AK" there and
    // "AL" on every other page showing the same person.
    expect(initialsOf("Ada King Lovelace")).toBe("AL");
    expect(initialsOf("Ada Lovelace")).toBe("AL");
  });

  it("reads an email address as a name, and drops the domain", () => {
    // The subscriptions copy split on whitespace only, so this was "AD"; the
    // other three split on `@` as an ordinary separator and got "AE" -- the A
    // of "ada" and the E of "example", so every operator at one company
    // shared a last initial.
    expect(initialsOf("ada.lovelace@toeshee.example")).toBe("AL");
    expect(initialsOf("ada-lovelace")).toBe("AL");
    expect(initialsOf("ada@toeshee.example")).toBe("AD");
  });

  it("answers for a single word and for nothing at all", () => {
    expect(initialsOf("Ada")).toBe("AD");
    expect(initialsOf("   ")).toBe("?");
    expect(initialsOf("")).toBe("?");
  });
});

describe("personIndex", () => {
  it("is stable for an id, which is the entire point", () => {
    expect(personIndex("usr-1")).toBe(personIndex("usr-1"));
  });

  it("separates ids that differ", () => {
    expect(personIndex("usr-1")).not.toBe(personIndex("usr-2"));
  });

  it("is never negative, so `Avatar`'s modulus lands on a real tone", () => {
    // The hash is `| 0`, which wraps into the negatives on a long id.
    for (const id of ["", "a", "usr-00000000-0000-0000-0000-0000000000ff", "éè"]) {
      expect(personIndex(id)).toBeGreaterThanOrEqual(0);
    }
  });

  it("does NOT reduce to a tone, because `Avatar` does that", () => {
    // A copy that took `% 6` inside its own loop produced a different colour
    // for the same person than one that reduced at the end. Reducing in two
    // places is the defect; this returns the whole hash.
    const many = new Set([1, 2, 3, 4, 5, 6, 7, 8].map((n) => personIndex(`usr-${n}`)));
    expect([...many].some((value) => value > 6)).toBe(true);
  });
});
