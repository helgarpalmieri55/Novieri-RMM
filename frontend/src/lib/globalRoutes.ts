/**
 * The routes of configuration shared by every client, which never carry
 * `X-Client-Id` (phase 3 design §1.2).
 *
 * WHY. The server lets only a scope of EVERY client change global
 * configuration (`require_permission_on_every_client`), and the header
 * narrows the scope to one client -- so a global route sent with it is a 403
 * for the very person allowed to make the change, whichever client the
 * selector shows.
 *
 * THE LIST IS NOT WRITTEN HERE. `globalRoutes.json` is generated from
 * `GLOBAL_WRITES` and `GLOBAL_READS` in `backend/tests/test_global_writes.py`
 * (`cd backend && .venv/bin/python -m tests.export_console_global_routes`),
 * and `backend/tests/test_console_global_routes.py` fails CI when they
 * disagree.
 *
 * Paths are relative to `/api/v1`, as `ApiClient` requests them. A `{param}`
 * stands for exactly one non-empty segment; a query string is ignored.
 */

import routes from "./globalRoutes.json";

export interface GlobalRoute {
  method: string;
  path: string;
}

export const GLOBAL_ROUTES: readonly GlobalRoute[] = routes;

interface Matcher {
  method: string;
  /** The template's segments; `null` is a `{param}`, which one non-empty segment fills. */
  segments: readonly (string | null)[];
}

const MATCHERS: readonly Matcher[] = GLOBAL_ROUTES.map((route) => ({
  method: route.method.toUpperCase(),
  segments: route.path.split("/").map((part) => (/^\{[^/{}]+\}$/.test(part) ? null : part)),
}));

export function isGlobalRoute(method: string, path: string): boolean {
  const wanted = method.toUpperCase();
  const parts = (path.split(/[?#]/, 1)[0] ?? "").split("/");
  return MATCHERS.some(
    (matcher) =>
      matcher.method === wanted &&
      matcher.segments.length === parts.length &&
      matcher.segments.every((segment, index) => {
        const part = parts[index] ?? "";
        return segment === null ? part !== "" : segment === part;
      }),
  );
}
