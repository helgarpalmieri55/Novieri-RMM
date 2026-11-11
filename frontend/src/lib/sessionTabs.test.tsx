/**
 * THE OTHER TABS HEAR AN END (final re-review, M1). A second window left
 * visible beside the one that signed out kept the tenant's page drawn until
 * its own next request -- for ever, on a page that asks nothing. OWASP's
 * "active actions to invalidate the session on both sides": a tab that learns
 * its session ended tells the others, over a `BroadcastChannel`, or a
 * `storage` event where a browser has none.
 *
 *   - a sign-out ends them;
 *   - any other end makes them ask `/me` (`./sessionCheck.ts`): the cookie
 *     is shared, so it answers for them too, and decides.
 *
 * "The other tab" is a second `BroadcastChannel` on the same name, which is
 * what another document in this browser holds.
 */
import { render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiProvider } from "./ApiContext";
import { isSessionEnded, sessionEnded, sessionPerson, sessionSignedOut } from "./sessionEnd";
import { SESSION_CHANNEL, SESSION_STORAGE_KEY, resetSessionTabs } from "./sessionTabs";

let otherTab: BroadcastChannel | null = null;

beforeEach(() => {
  resetSessionTabs();
  otherTab = new BroadcastChannel(SESSION_CHANNEL);
});

afterEach(() => {
  otherTab?.close();
  otherTab = null;
  resetSessionTabs();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function heardByOtherTab(): unknown[] {
  const heard: unknown[] = [];
  otherTab!.onmessage = (event: MessageEvent) => heard.push(event.data);
  return heard;
}

function renderTab() {
  return render(
    <ApiProvider>
      <p>page</p>
    </ApiProvider>,
  );
}

function stubMe(answer: () => Response): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) =>
    String(input).endsWith("/api/v1/me") ? answer() : new Response("{}", { status: 404 }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("telling the other tabs", () => {
  it("says a sign-out as a sign-out", async () => {
    const heard = heardByOtherTab();
    sessionSignedOut();
    await waitFor(() => expect(heard).toEqual(["signed_out"]));
  });

  it("says any other end as an end, once", async () => {
    const heard = heardByOtherTab();
    sessionEnded();
    sessionEnded();
    await waitFor(() => expect(heard).toEqual(["ended"]));
    sessionPerson("u-1");
    expect(heard).toEqual(["ended"]);
  });

  it("does not hear itself", async () => {
    renderTab();
    sessionPerson("u1");
    const fetchMock = stubMe(() => new Response(JSON.stringify({ user_id: "u1" }), { status: 200 }));
    sessionSignedOut();
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("hearing another tab", () => {
  it("ends this tab's session when the other one signed out", async () => {
    renderTab();
    otherTab!.postMessage("signed_out");
    await waitFor(() => expect(isSessionEnded()).toBe(true));
  });

  it("asks /me when another tab's session ended, and stays while it answers for the same person", async () => {
    renderTab();
    sessionPerson("u1");
    const fetchMock = stubMe(() => new Response(JSON.stringify({ user_id: "u1" }), { status: 200 }));
    otherTab!.postMessage("ended");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(isSessionEnded()).toBe(false);
  });

  it("ends when that /me answers 401", async () => {
    renderTab();
    sessionPerson("u1");
    stubMe(() => new Response("", { status: 401 }));
    otherTab!.postMessage("ended");
    await waitFor(() => expect(isSessionEnded()).toBe(true));
  });

  it("ignores anything else on the channel", async () => {
    renderTab();
    const fetchMock = stubMe(() => new Response("", { status: 401 }));
    otherTab!.postMessage({ kind: "signed_out" });
    otherTab!.postMessage("hello");
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(isSessionEnded()).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("a browser with no BroadcastChannel", () => {
  beforeEach(() => {
    otherTab?.close();
    otherTab = null;
    resetSessionTabs();
    vi.stubGlobal("BroadcastChannel", undefined);
  });

  it("tells the other tabs through a storage event", () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    sessionSignedOut();
    expect(setItem).toHaveBeenCalledWith(SESSION_STORAGE_KEY, expect.stringMatching(/^signed_out /));
    expect(window.localStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
  });

  it("hears another tab's sign-out through one", async () => {
    renderTab();
    window.dispatchEvent(new StorageEvent("storage", { key: SESSION_STORAGE_KEY, newValue: "signed_out 1730800000000" }));
    await waitFor(() => expect(isSessionEnded()).toBe(true));
  });
});
