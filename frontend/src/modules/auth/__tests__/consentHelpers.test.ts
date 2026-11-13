/**
 * `consent.ts`, the consent page's two helpers (plan 2026-11-06, Task 4; fix
 * round 1). `isFramed` is the frame-busting fallback for a browser that
 * ignores `frame-ancestors`, so the case that matters most is the one a test
 * cannot reach by rendering: a cross-origin parent, whose `window.top` throws.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { authorizationQuery, isFramed } from "../consent";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("isFramed", () => {
  it("is false for a document on its own", () => {
    expect(isFramed()).toBe(false);
  });

  it("is true inside another document", () => {
    vi.spyOn(window, "top", "get").mockReturnValue({} as Window);
    expect(isFramed()).toBe(true);
  });

  it("is true when the parent cannot even be read: a cross-origin frame", () => {
    vi.spyOn(window, "top", "get").mockImplementation(() => {
      throw new DOMException("Blocked a frame from accessing a cross-origin frame.", "SecurityError");
    });
    expect(isFramed()).toBe(true);
  });
});

describe("authorizationQuery", () => {
  it("is the query as the browser has it, without the ?", () => {
    expect(authorizationQuery("?client_id=itms_x&redirect_uri=https%3A%2F%2Fa.example")).toBe(
      "client_id=itms_x&redirect_uri=https%3A%2F%2Fa.example",
    );
    expect(authorizationQuery("")).toBe("");
  });
});
