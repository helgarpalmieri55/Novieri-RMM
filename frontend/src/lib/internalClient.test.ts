/**
 * The internal client the console works on until the client selector exists
 * (MSP clients, phase 1; the selector is phase 3).
 *
 * WHAT IS PINNED. The id convention -- the organisation's internal client and
 * its "Principal" site carry the organisation's own id, which `/me` already
 * returns as `org_id` -- and the refusal when that id is not known yet: a
 * create that cannot name its client or site must say so in the person's
 * language rather than send a body the server answers with a 422.
 */

import { afterEach, describe, expect, it } from "vitest";

import { setActiveLanguage } from "../i18n/core";
import type { Me } from "./api";
import { InternalScopeUnknown, internalClientId, internalSiteId, requireKnown } from "./internalClient";

const ME: Me = {
  user_id: "u",
  org_id: "00000000-0000-4000-8000-00000000000a",
  email: "op@example.com",
  display_name: "Operator",
  permissions: [],
};

afterEach(() => setActiveLanguage("en"));

describe("the internal client and its Principal site", () => {
  it("are both the organisation's id, read from /me", () => {
    expect(internalClientId(ME)).toBe(ME.org_id);
    expect(internalSiteId(ME)).toBe(ME.org_id);
  });

  it("are unknown, never a guess, before /me has answered", () => {
    expect(internalClientId(undefined)).toBeNull();
    expect(internalSiteId(undefined)).toBeNull();
  });
});

describe("requireKnown", () => {
  it("hands back a known id unchanged", () => {
    expect(requireKnown(ME.org_id)).toBe(ME.org_id);
  });

  it("refuses an unknown one with a sentence, in the person's language", () => {
    expect(() => requireKnown(null)).toThrow(InternalScopeUnknown);
    expect(() => requireKnown(null)).toThrow(/account has not loaded/i);
    setActiveLanguage("es");
    expect(() => requireKnown(null)).toThrow(/tu cuenta aún no se ha cargado/i);
  });
});
