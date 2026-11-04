/**
 * The console's `ApiClient`, as `ApiProvider` builds it outside fixture mode.
 *
 * `X-CLIENT-ID` IS VALIDATED WHERE IT IS SENT. The server answers a malformed
 * `X-Client-Id` with 400 on every route, `/me` included
 * (`backend/app/api/deps.py`), so a choice it cannot read would leave the
 * console unable even to learn that it should drop it. A real choice is
 * always an id from `/me`; what this boundary refuses is a value that was
 * never a client's -- a fixture-style id (`cli-logistics`, as the fixtures'
 * customer was until harmony had to choose it) outside fixture mode, a bug.
 * Fixture mode never builds an `ApiClient` (it answers from a proxy), so its
 * ids are untouched by this.
 */

import { render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "./api";
import { ApiProvider, useApi } from "./ApiContext";
import { setSelectedClient } from "./clientChoice";

vi.mock("./auth", () => ({ accessToken: () => "t", signIn: vi.fn() }));

const ACME = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";

function stubFetch(): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response("[]", { status: 200 })));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function sent(fetchMock: ReturnType<typeof vi.fn>, call = 0): Headers {
  return new Headers((fetchMock.mock.calls[call] as [string, RequestInit])[1].headers);
}

/** The client `ApiProvider` hands every page. */
function providedApi(): ApiClient {
  let api: ApiClient | null = null;
  function Probe() {
    api = useApi();
    return null;
  }
  render(
    <ApiProvider>
      <Probe />
    </ApiProvider>,
  );
  if (api === null) throw new Error("ApiProvider rendered nothing");
  return api;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ApiProvider's X-Client-Id", () => {
  it("names the chosen client when it is a client id", async () => {
    const fetchMock = stubFetch();
    const api = providedApi();
    setSelectedClient(ACME);
    await api.listDevices();
    expect(sent(fetchMock).get("X-Client-Id")).toBe(ACME);
  });

  it("is not sent for a choice that is not a client id", async () => {
    const fetchMock = stubFetch();
    const api = providedApi();
    for (const notAnId of ["cli-logistics", `${ACME}\n`, `{${ACME}}`, ` ${ACME}`, "all"]) {
      setSelectedClient(notAnId);
      await api.listDevices();
    }
    expect(fetchMock).toHaveBeenCalledTimes(5);
    for (let call = 0; call < 5; call += 1) expect(sent(fetchMock, call).has("X-Client-Id")).toBe(false);
  });
});
