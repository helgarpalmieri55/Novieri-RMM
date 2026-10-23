/**
 * Where the console looks for Keycloak.
 *
 * THIS FILE EXISTS BECAUSE THE CONSOLE COULD NOT LOG IN ON ANY DEPLOYMENT.
 * `authority` was `import.meta.env.VITE_OIDC_AUTHORITY ?? "http://localhost:8080/..."`,
 * and `VITE_OIDC_AUTHORITY` was set nowhere — not in `.env.example`, not in
 * `frontend.Dockerfile`, not as a compose build argument. So every image ever
 * built carried the localhost fallback, and every browser that opened the
 * console tried to fetch the discovery document from *its own machine* on port
 * 8080. The symptom is a login button that does nothing, with repeated 0-byte
 * `openid-configuration` fetches in the network tab and no server-side error
 * anywhere, because the request never reaches the server.
 *
 * A build-time variable was the wrong shape for this value. Keycloak is served
 * on the SAME ORIGIN as the console, under `/realms` (the `kc` router in
 * `deploy/docker-compose.yml`), so the authority is derivable from the page
 * itself and cannot be misconfigured. The override survives only for local
 * development, where the dev server and Keycloak are genuinely on different
 * ports — see `frontend/.env.development`.
 */

import { describe, expect, it } from "vitest";

import { resolveAuthority } from "./auth";

describe("resolveAuthority", () => {
  it("derives the authority from the page's own origin when nothing is configured", () => {
    expect(resolveAuthority(undefined, "https://itms.toeshee.com")).toBe(
      "https://itms.toeshee.com/realms/it-suite",
    );
  });

  it("works for any hostname, because the deployment's name is not knowable at build time", () => {
    // The image is built once and deployed under whatever name the operator
    // owns. Anything baked in at build time is wrong for every deployment but
    // the one it was built for.
    expect(resolveAuthority(undefined, "https://console.example.net")).toBe(
      "https://console.example.net/realms/it-suite",
    );
  });

  it("NEVER falls back to localhost — the defect this file was written for", () => {
    for (const configured of [undefined, ""]) {
      const resolved = resolveAuthority(configured, "https://itms.toeshee.com");
      expect(resolved).not.toContain("localhost");
      expect(resolved).not.toContain(":8080");
    }
  });

  it("treats an empty string as unset, since that is what an unsubstituted build arg produces", () => {
    expect(resolveAuthority("", "https://x.example")).toBe("https://x.example/realms/it-suite");
  });

  it("honours an explicit override, which is how local development reaches a separate Keycloak", () => {
    expect(resolveAuthority("http://localhost:8080/realms/it-suite", "http://localhost:5173")).toBe(
      "http://localhost:8080/realms/it-suite",
    );
  });
});
