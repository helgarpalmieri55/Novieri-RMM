/**
 * `/backup/coverage` over the fixture data, the way the harmony gate walks it.
 *
 * THE FIXTURE HAD DRIFTED FROM THE REPORT IT STANDS FOR. `runReport(
 * "backup-coverage")` in fixture mode answered rows of `hostname`, `verdict`
 * and `last_verified_restore_point` -- none of them but the hostname a field
 * of the server's `BackupCoverageRow` -- so the page keyed every row
 * `String(undefined)`, React logged "Encountered two children with the same
 * key" ten times on every walk (harmony, both languages, on main as on this
 * branch), and every row read "No backup policy / Never / Off / Never".
 *
 * Held here to the server's own model, read from its source, so the next
 * column the report gains cannot leave the fixture behind unnoticed.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { fixtureClient } from "../../../fixtures/client";
import { CoveragePage } from "../CoveragePage";

const api = vi.hoisted(() => ({ current: undefined as unknown }));
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => api.current }));

const MODEL = resolve(
  import.meta.dirname,
  "../../../../../backend/app/modules/reporting/definitions/backup_coverage.py",
);

/** `BackupCoverageRow`'s field names, in the order the server declares them. */
function serverFields(): string[] {
  const source = readFileSync(MODEL, "utf8");
  const body = /class BackupCoverageRow\(BaseModel\):([\s\S]*?)\n(?:class |def |\S)/.exec(source)?.[1];
  if (body === undefined) throw new Error("no BackupCoverageRow in backup_coverage.py");
  return [...body.matchAll(/^ {4}(\w+): [^\n]*= Field\(/gm)].map((match) => match[1]!);
}

describe("the backup-coverage report's fixture", () => {
  it("answers rows of the server's BackupCoverageRow, one per device", async () => {
    const fields = serverFields();
    // NON-VACUITY: the parse found the model, and the fields the page keys and filters on.
    expect(fields).toEqual(expect.arrayContaining(["device_id", "hostname", "coverage", "offsite"]));

    const page = await fixtureClient().runReport("backup-coverage", { limit: "500" });
    expect(page.rows.length).toBeGreaterThan(1);
    for (const row of page.rows) expect(Object.keys(row)).toEqual(fields);
    const ids = page.rows.map((row) => row["device_id"]);
    expect(new Set(ids).size).toBe(ids.length);
    // The totals count the same verdicts the rows carry.
    for (const verdict of ["covered", "stale", "failing", "unprotected"]) {
      expect(page.totals[verdict]).toBe(page.rows.filter((row) => row["coverage"] === verdict).length);
    }
  });

  it("draws /backup/coverage with every verdict and no duplicate row key", async () => {
    // A duplicate key is a `console.error`, which `src/test/setup.ts` fails the test on.
    api.current = fixtureClient();
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={["/backup/coverage"]}>
          <CoveragePage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.length).toBeGreaterThan(1);
    const text = rows.map((row) => row.textContent ?? "").join("\n");
    for (const verdict of ["Protected", "Out of date", "Failing", "Not protected"]) expect(text).toContain(verdict);
  });
});
