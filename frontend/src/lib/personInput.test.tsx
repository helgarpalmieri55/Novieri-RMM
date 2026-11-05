/**
 * A REQUEST NO PERSON CAUSED DOES NOT KEEP THE SESSION ALIVE (final re-review,
 * P1; coordinator decision, 2026-11-05). The console polls -- the shell's
 * health and review counts every 60 s on every page -- and the server renewed
 * the session on every request, so the 30-minute inactivity limit never ran
 * out while a console tab was visible. NIST SP 800-63B-4 §5.2: "An inactivity
 * timeout terminates a session without activity from the subscriber for a
 * specific period."; OWASP Session Management Cheat Sheet: "Session timeout
 * management and expiration must be enforced server-side."
 *
 * So the console says which requests a person caused: every request carries
 * `X-ITMS-Background: 1` unless a person gave input in the last 60 s, and the
 * server does not renew the session for it. And a person working on a page
 * that asks nothing of its own -- a remote screen -- is kept signed in: while
 * they give input, a request without the header goes out at least every 60 s.
 */
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiClient, BACKGROUND_HEADER, requestHeaders } from "./api";
import { ACTIVE_FOR_MS, personActive, resetPersonInput, watchPersonInput } from "./personInput";

let stop: (() => void) | null = null;

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-11-05T09:00:00Z"));
  resetPersonInput();
});

afterEach(() => {
  stop?.();
  stop = null;
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/** Every request `fetch` was asked to make, with its headers. */
function stubFetch(): Array<{ url: string; headers: Headers }> {
  const sent: Array<{ url: string; headers: Headers }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      sent.push({ url: String(input), headers: new Headers(init?.headers) });
      return new Response(JSON.stringify({ user_id: "u1", permissions: [] }), { status: 200 });
    }),
  );
  return sent;
}

describe("the background header", () => {
  it("is on a poll nobody caused", async () => {
    const sent = stubFetch();
    const api = new ApiClient(() => {});
    await api.listDevices().catch(() => {});
    expect(sent).toHaveLength(1);
    expect(sent[0]!.headers.get(BACKGROUND_HEADER)).toBe("1");
  });

  it("is off on a request right after the person's input, and back on 60 s later", async () => {
    stop = watchPersonInput(window, () => {});
    const sent = stubFetch();
    const api = new ApiClient(() => {});

    fireEvent.keyDown(window, { key: "a" });
    await api.listDevices().catch(() => {});
    expect(sent.at(-1)!.headers.has(BACKGROUND_HEADER)).toBe(false);

    vi.advanceTimersByTime(ACTIVE_FOR_MS);
    await api.listDevices().catch(() => {});
    expect(sent.at(-1)!.headers.get(BACKGROUND_HEADER)).toBe("1");
  });

  it("counts a click, a touch, a wheel and a moved pointer as a person, and nothing else", () => {
    stop = watchPersonInput(window, () => {});
    for (const fire of [
      () => fireEvent.pointerDown(window),
      () => fireEvent.touchStart(window),
      () => fireEvent.wheel(window),
      () => fireEvent.pointerMove(window),
      () => fireEvent.keyDown(window, { key: "Tab" }),
    ]) {
      resetPersonInput();
      fire();
      expect(personActive()).toBe(true);
    }
    resetPersonInput();
    fireEvent.focus(window);
    fireEvent.scroll(window);
    expect(personActive()).toBe(false);
    expect(requestHeaders().get(BACKGROUND_HEADER)).toBe("1");
  });

  it("counts input over a component that stops it from bubbling -- the remote screen does", () => {
    stop = watchPersonInput(window, () => {});
    render(
      <div
        role="application"
        aria-label="Remote screen"
        tabIndex={0}
        onKeyDown={(event) => event.stopPropagation()}
        onPointerMove={(event) => event.stopPropagation()}
      />,
    );
    fireEvent.keyDown(screen.getByRole("application", { name: "Remote screen" }), { key: "x" });
    expect(personActive()).toBe(true);
  });
});

describe("the keep-alive", () => {
  it("sends a request without the header at least every 60 s while the person gives input, and stops without it", () => {
    const keepAlive = vi.fn();
    stop = watchPersonInput(window, keepAlive);

    // Two minutes of steady work, an input every 5 s, on a page that asks nothing.
    for (let second = 0; second < 120; second += 5) {
      fireEvent.pointerDown(window);
      vi.advanceTimersByTime(5_000);
    }
    expect(keepAlive).toHaveBeenCalledTimes(2);

    // Then nobody: no more keep-alive, however long.
    vi.advanceTimersByTime(30 * 60_000);
    expect(keepAlive).toHaveBeenCalledTimes(2);
  });

  it("is not needed while the page's own requests already carry no header", () => {
    const keepAlive = vi.fn();
    stop = watchPersonInput(window, keepAlive);
    fireEvent.keyDown(window, { key: "a" });
    expect(keepAlive).toHaveBeenCalledTimes(1);
    for (let second = 0; second < 120; second += 10) {
      vi.advanceTimersByTime(10_000);
      fireEvent.keyDown(window, { key: "a" });
      // The page asks something itself, and the person caused it.
      expect(requestHeaders().has(BACKGROUND_HEADER)).toBe(false);
    }
    expect(keepAlive).toHaveBeenCalledTimes(1);
  });

  it("stops listening when it is stopped", () => {
    const keepAlive = vi.fn();
    stop = watchPersonInput(window, keepAlive);
    stop();
    stop = null;
    fireEvent.keyDown(window, { key: "a" });
    expect(personActive()).toBe(false);
    expect(keepAlive).not.toHaveBeenCalled();
  });
});

describe("the console's own keep-alive", () => {
  it("is a plain GET /me through the provider's client, without the header", async () => {
    const sent = stubFetch();
    const { ApiProvider } = await import("./ApiContext");
    render(
      <ApiProvider>
        <p>page</p>
      </ApiProvider>,
    );
    await act(async () => {
      fireEvent.keyDown(window, { key: "a" });
    });
    const me = sent.filter((request) => request.url.endsWith("/api/v1/me"));
    expect(me).toHaveLength(1);
    expect(me[0]!.headers.has(BACKGROUND_HEADER)).toBe(false);
  });
});
