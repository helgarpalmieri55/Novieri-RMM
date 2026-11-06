/**
 * NOTHING IS LEFT FOR PHASE 3 (MSP clients phase 3, design §3.6).
 *
 * Phases 1 and 2 marked every place that filed a new row under the
 * organisation's internal client "until the selector exists" with a
 * `PHASE 3` line and a call to `internalClientId(me)` or `internalSiteId(me)`.
 * Phase 3 built the selector and moved every one of them to
 * `lib/clientSelection.ts`. A marker or a call that comes back is a form
 * filing rows under the internal client without anyone choosing it -- the
 * thing the operator's Q1 ruled out -- so the whole source tree is read, tests
 * and comments included, and any of the three fails it.
 *
 * This file names all three in its own prose, so it is skipped by path; the
 * markers themselves are built from pieces, so the self-test below proves the
 * matcher rather than a copy of this file's text.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = join(import.meta.dirname, "..");
const SELF = join("lib", "noPhase3.test.ts");
const MARKERS = [["PHASE", "3"].join(" "), ["internal", "ClientId"].join(""), ["internal", "SiteId"].join("")];

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return walk(path);
    return /\.(ts|tsx|mts|js|mjs|css|json)$/.test(name) ? [path] : [];
  });
}

function offenders(files: readonly { path: string; text: string }[]): string[] {
  return files.flatMap(({ path, text }) =>
    MARKERS.filter((marker) => text.includes(marker)).map((marker) => `${path}: ${marker}`),
  );
}

describe("the console keeps nothing marked for phase 3", () => {
  const files = walk(SRC);

  it("reads the whole source tree", () => {
    expect(files.length).toBeGreaterThan(500);
    expect(files.map((file) => relative(SRC, file))).toContain(join("lib", "api.ts"));
  });

  it("has no PHASE 3 marker and no internal-client helper left", () => {
    const tree = files
      .map((file) => ({ path: relative(SRC, file), text: readFileSync(file, "utf8") }))
      .filter((file) => file.path !== SELF);
    expect(offenders(tree)).toEqual([]);
  });

  it("finds each of the three when one is there", () => {
    expect(
      offenders([
        { path: "a.tsx", text: `// ${MARKERS[0]}: the selector` },
        { path: "b.ts", text: `const c = ${MARKERS[1]}(me);` },
        { path: "c.ts", text: `siteId={${MARKERS[2]}(me.data)}` },
        { path: "d.ts", text: "nothing here" },
      ]),
    ).toEqual([`a.tsx: ${MARKERS[0]}`, `b.ts: ${MARKERS[1]}`, `c.ts: ${MARKERS[2]}`]);
  });
});
