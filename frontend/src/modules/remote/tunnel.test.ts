import { describe, expect, it } from "vitest";

import * as tunnel from "./tunnel";
import { screenUrl } from "./tunnel";

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

/**
 * RE-POINTED (own-auth Task 10). `screenProtocols` put the bearer token in the
 * subprotocol list and `canOpenTunnel` refused to open without one. The
 * session cookie is the stream's credential now, sent by the browser with the
 * upgrade, so this module has no token to place and none to check:
 * `ScreenViewer.test.tsx` holds the viewer to opening with no subprotocol and
 * to opening nothing once the session has ended.
 */
describe("the stream's credential", () => {
  it("is nobody's to place: the module offers the address and nothing else", () => {
    expect(Object.keys(tunnel).sort()).toEqual(["screenUrl"]);
  });
});
