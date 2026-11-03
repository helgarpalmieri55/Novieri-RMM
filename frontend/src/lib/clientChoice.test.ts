/**
 * The client chosen in the top bar, as a value outside React (phase 3 design
 * §1.1). Kept in `localStorage["itms.client"]` as the id or `all`; read back
 * only when it is one: the server answers a malformed `X-Client-Id` with 400
 * on every route, `/me` included, so a stored value it cannot read would lock
 * the console out of learning that it should drop it.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  CLIENT_STORAGE_KEY,
  adoptStoredClient,
  readStoredClient,
  selectedClient,
  setSelectedClient,
  subscribeClient,
} from "./clientChoice";

const ACME = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";

afterEach(() => vi.restoreAllMocks());

describe("the stored choice", () => {
  it("reads a client id back, and anything else as every client", () => {
    window.localStorage.setItem(CLIENT_STORAGE_KEY, ACME);
    expect(readStoredClient()).toBe(ACME);
    window.localStorage.setItem(CLIENT_STORAGE_KEY, "all");
    expect(readStoredClient()).toBeNull();
    // Not a client id: sent, it would be a 400 on every request.
    window.localStorage.setItem(CLIENT_STORAGE_KEY, "acme");
    expect(readStoredClient()).toBeNull();
    window.localStorage.removeItem(CLIENT_STORAGE_KEY);
    expect(readStoredClient()).toBeNull();
  });

  it("reads as every client when the browser refuses storage", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("storage is blocked");
    });
    expect(readStoredClient()).toBeNull();
  });

  it("is written as the id or `all`, and told to its listeners once per change", () => {
    const heard = vi.fn();
    const stop = subscribeClient(heard);
    setSelectedClient(ACME);
    expect(selectedClient()).toBe(ACME);
    expect(window.localStorage.getItem(CLIENT_STORAGE_KEY)).toBe(ACME);
    setSelectedClient(ACME);
    expect(heard).toHaveBeenCalledTimes(1);
    setSelectedClient(null);
    expect(selectedClient()).toBeNull();
    expect(window.localStorage.getItem(CLIENT_STORAGE_KEY)).toBe("all");
    expect(heard).toHaveBeenCalledTimes(2);
    stop();
  });

  it("adopts what another tab stored", () => {
    window.localStorage.setItem(CLIENT_STORAGE_KEY, ACME);
    adoptStoredClient();
    expect(selectedClient()).toBe(ACME);
  });
});
