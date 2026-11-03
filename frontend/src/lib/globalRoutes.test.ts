/**
 * Which requests never carry `X-Client-Id` (phase 3 design §1.2).
 *
 * WHAT IS PINNED. The comparison, not the list: the list is the server's,
 * and `backend/tests/test_console_global_routes.py` holds `globalRoutes.json`
 * equal to it. Here, that a request is matched by METHOD and PATH together, a
 * `{param}` standing for exactly one non-empty segment -- so `PATCH
 * /clients/{id}` (one client's own row) keeps the header while `POST
 * /clients/{id}/archive` (every client) drops it.
 */

import { describe, expect, it } from "vitest";

import { GLOBAL_ROUTES, isGlobalRoute } from "./globalRoutes";
import routes from "./globalRoutes.json";

const ID = "0192f3a4-5b6c-7d8e-9f01-23456789abcd";

describe("isGlobalRoute", () => {
  it("matches a template with its parameter filled in", () => {
    expect(isGlobalRoute("POST", `/clients/${ID}/archive`)).toBe(true);
    expect(isGlobalRoute("DELETE", `/users/${ID}/roles/${ID}`)).toBe(true);
    expect(isGlobalRoute("DELETE", "/agent/releases/versions/1.4.2")).toBe(true);
  });

  it("needs the method too", () => {
    expect(isGlobalRoute("POST", "/clients")).toBe(true);
    expect(isGlobalRoute("GET", "/clients")).toBe(false);
    expect(isGlobalRoute("PATCH", "/orgs/current")).toBe(true);
    expect(isGlobalRoute("GET", "/orgs/current")).toBe(false);
    expect(isGlobalRoute("GET", "/audit")).toBe(true);
    expect(isGlobalRoute("post", "/clients")).toBe(true);
  });

  it("keeps the header on a route that is one client's", () => {
    expect(isGlobalRoute("PATCH", `/clients/${ID}`)).toBe(false);
    expect(isGlobalRoute("POST", `/clients/${ID}/contacts`)).toBe(false);
    expect(isGlobalRoute("POST", "/health/assignments")).toBe(false);
    expect(isGlobalRoute("GET", "/devices")).toBe(false);
  });

  it("lets a parameter stand for one non-empty segment, never more or none", () => {
    expect(isGlobalRoute("POST", `/clients/${ID}/x/archive`)).toBe(false);
    expect(isGlobalRoute("POST", "/clients//archive")).toBe(false);
    expect(isGlobalRoute("POST", `/clients/${ID}/archive/`)).toBe(false);
    expect(isGlobalRoute("POST", "/clients/archive")).toBe(false);
  });

  it("compares the path without its query string", () => {
    expect(isGlobalRoute("GET", "/audit?limit=50&offset=100")).toBe(true);
    expect(isGlobalRoute("GET", "/notifications/deliveries?status=failed")).toBe(true);
  });

  it("takes paths relative to /api/v1, as ApiClient requests them", () => {
    expect(isGlobalRoute("GET", "/api/v1/audit")).toBe(false);
  });

  it("finds every route of the generated file", () => {
    expect(GLOBAL_ROUTES.length).toBe(routes.length);
    expect(GLOBAL_ROUTES.length).toBeGreaterThan(0);
    for (const route of GLOBAL_ROUTES) {
      const filled = route.path.replace(/\{[^/{}]+\}/g, ID);
      expect(isGlobalRoute(route.method, filled), `${route.method} ${route.path}`).toBe(true);
      expect(route.path.startsWith("/api/")).toBe(false);
    }
  });
});
