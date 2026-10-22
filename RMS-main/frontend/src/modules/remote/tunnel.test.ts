import { describe, expect, it } from "vitest";

import { canOpenTunnel, screenProtocols, screenUrl } from "./tunnel";

describe("screenUrl", () => {
  it("addresses the session's screen stream on the console's own origin", () => {
    expect(screenUrl("sess-1", { protocol: "http:", host: "127.0.0.1:5173" })).toBe(
      "ws://127.0.0.1:5173/api/v1/remote/sessions/sess-1/screen",
    );
  });

  it("upgrades to wss when the console itself was served over TLS", () => {
    expect(screenUrl("sess-1", { protocol: "https:", host: "console.example" })).toBe(
      "wss://console.example/api/v1/remote/sessions/sess-1/screen",
    );
  });

  it("escapes the session id", () => {
    expect(screenUrl("a/../b", { protocol: "http:", host: "h" })).toBe(
      "ws://h/api/v1/remote/sessions/a%2F..%2Fb/screen",
    );
  });

  it("never carries a query string, so no credential can reach an access log", () => {
    expect(screenUrl("s", { protocol: "http:", host: "h" })).not.toContain("?");
  });
});

describe("screenProtocols", () => {
  it("carries the token as the second subprotocol, after bearer", () => {
    expect(screenProtocols("tok-1")).toEqual(["bearer", "tok-1"]);
  });
});

describe("canOpenTunnel", () => {
  it("refuses a missing or blank credential", () => {
    expect(canOpenTunnel(null)).toBe(false);
    expect(canOpenTunnel(undefined)).toBe(false);
    expect(canOpenTunnel("")).toBe(false);
    expect(canOpenTunnel("   ")).toBe(false);
  });

  it("accepts a real one", () => {
    expect(canOpenTunnel("tok-1")).toBe(true);
  });
});
