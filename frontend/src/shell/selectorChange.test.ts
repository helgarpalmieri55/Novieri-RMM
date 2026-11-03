/**
 * The mark a change made through the client selector leaves for the selector
 * the change remounts (WCAG 2.4.3, 3.2.2). It is taken once, and only for the
 * choice it was made for: a change that arrives some other way -- another
 * tab, the fallback when /me no longer lists the choice -- finds no mark that
 * matches, and moves nobody's focus.
 */

import { describe, expect, it } from "vitest";

import { setSelectedClient } from "../lib/clientChoice";
import { markSelectorChange, selectorChangePending, takeSelectorChange } from "./selectorChange";

const ACME = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";
const ZETA = "5c9e1d2f-3a4b-4c5d-8e6f-7a8b9c0d1e02";

describe("the selector's change mark", () => {
  it("is pending for the choice it was made for, and taken once", () => {
    markSelectorChange(ACME);
    setSelectedClient(ACME);
    expect(selectorChangePending()).toBe(true);
    expect(takeSelectorChange()).toBe(true);
    expect(selectorChangePending()).toBe(false);
    expect(takeSelectorChange()).toBe(false);
  });

  it("names every client as well as one", () => {
    setSelectedClient(ACME);
    markSelectorChange(null);
    setSelectedClient(null);
    expect(takeSelectorChange()).toBe(true);
  });

  it("is not pending once the choice has moved on without the selector", () => {
    markSelectorChange(ACME);
    setSelectedClient(ACME);
    // Another tab chose ZETA before the selector took its mark.
    setSelectedClient(ZETA);
    expect(selectorChangePending()).toBe(false);
    expect(takeSelectorChange()).toBe(false);
  });
});
