/**
 * A contact's 401 is confirmed with `/portal/me` (own-auth phase 2, Task 10,
 * decision 4): the console's `/me` answers them 403 `portal_only`, which
 * decides nothing, so the portal's own `/me` is asked with the same two rules.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { confirmSessionEnded } from "./sessionCheck";
import { isSessionEnded, sessionEndReason, sessionPerson } from "./sessionEnd";

const PORTAL_ONLY = JSON.stringify({ detail: "this account can only use the portal", code: "portal_only" });

function answers(byPath: Record<string, Response>): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input).replace("/api/v1", "");
    const found = byPath[path];
    if (found === undefined) throw new Error(`unexpected request to ${path}`);
    return found.clone();
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a contact's 401", () => {
  it("ends the session when the portal's /me says 401 too", async () => {
    answers({ "/me": new Response(PORTAL_ONLY, { status: 403 }), "/portal/me": new Response("{}", { status: 401 }) });
    await confirmSessionEnded();
    expect(sessionEndReason()).toBe("ended");
  });

  it("ends it as replaced when the portal's /me names somebody else", async () => {
    sessionPerson("contact-1");
    answers({
      "/me": new Response(PORTAL_ONLY, { status: 403 }),
      "/portal/me": new Response(JSON.stringify({ user_id: "contact-2" }), { status: 200 }),
    });
    await confirmSessionEnded();
    expect(sessionEndReason()).toBe("replaced");
  });

  it("does not end it when the portal's /me names the same person", async () => {
    sessionPerson("contact-1");
    answers({
      "/me": new Response(PORTAL_ONLY, { status: 403 }),
      "/portal/me": new Response(JSON.stringify({ user_id: "contact-1" }), { status: 200 }),
    });
    await confirmSessionEnded();
    expect(isSessionEnded()).toBe(false);
  });

  it("does not ask the portal for any other 403", async () => {
    const fetchMock = answers({ "/me": new Response(JSON.stringify({ detail: "no" }), { status: 403 }) });
    await confirmSessionEnded();
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(isSessionEnded()).toBe(false);
  });
});
