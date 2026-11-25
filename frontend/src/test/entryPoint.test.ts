/**
 * THE BROWSER GATES RUN WHEN THEY ARE RUN, BY ANY PATH TO THEM (P3 adoption
 * pass; batch 3 review, Minor 1).
 *
 * `harmony.mjs` and `a11y-browser.mjs` run `main()` only when executed, so a
 * test can import their helpers without starting a server. The guard compared
 * `resolve(process.argv[1])` with the module's own path, and Node gives a
 * module its REAL path: invoked through a symlink (a linked checkout, a
 * `node_modules/.bin`-style link) the two differed, `main()` never ran, and the
 * gate exited 0 having measured nothing. `scripts/entryPoint.mjs` compares
 * both sides through `realpathSync`, and both scripts guard with it.
 */
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { afterAll, describe, expect, it } from "vitest";

import { isEntryPoint } from "../../scripts/entryPoint.mjs";

const SCRIPTS = resolve(import.meta.dirname, "../../scripts");
const HARMONY = join(SCRIPTS, "harmony.mjs");
const A11Y = join(SCRIPTS, "a11y-browser.mjs");
const links = mkdtempSync(join(tmpdir(), "entry-point-"));
afterAll(() => rmSync(links, { recursive: true, force: true }));

function linkTo(target: string, name: string): string {
  const link = join(links, name);
  symlinkSync(target, link);
  return link;
}

// POSIX symlinks: Windows needs a privilege to make one.
describe.skipIf(process.platform === "win32")("isEntryPoint", () => {
  it("is the module itself, invoked by its own path or a relative one", () => {
    expect(isEntryPoint(HARMONY, pathToFileURL(HARMONY).href)).toBe(true);
    expect(isEntryPoint(join(SCRIPTS, "..", "scripts", "harmony.mjs"), pathToFileURL(HARMONY).href)).toBe(true);
  });

  it("is the module invoked through a symlink to it", () => {
    const link = linkTo(HARMONY, "harmony-linked.mjs");
    expect(isEntryPoint(link, pathToFileURL(HARMONY).href)).toBe(true);
  });

  it("is not another script, nor nothing (imported by a test, or `node -e`)", () => {
    expect(isEntryPoint(A11Y, pathToFileURL(HARMONY).href)).toBe(false);
    expect(isEntryPoint(undefined, pathToFileURL(HARMONY).href)).toBe(false);
    expect(isEntryPoint(join(links, "no-such-file.mjs"), pathToFileURL(HARMONY).href)).toBe(false);
  });
});

describe.skipIf(process.platform === "win32")("the gates through a symlink", () => {
  it("runs harmony's main() when harmony is invoked through a symlink", () => {
    // An unknown argument is the first thing main() refuses, before any
    // server or browser starts: exit 1 and its sentence prove main() ran.
    // With the old guard this exited 0 and printed nothing.
    const link = linkTo(HARMONY, "harmony-run.mjs");
    const run = spawnSync(process.execPath, [link, "--no-such-flag"], { encoding: "utf8", timeout: 60_000 });
    expect(run.stderr).toContain("harmony: unknown argument --no-such-flag");
    expect(run.status).toBe(1);
  });

  it("guards both scripts with it, and neither with the bare comparison", () => {
    for (const script of [HARMONY, A11Y]) {
      const source = readFileSync(script, "utf8");
      expect(source, script).toMatch(/import \{ isEntryPoint \} from "\.\/entryPoint\.mjs";/);
      expect(source, script).toMatch(/if \(isEntryPoint\(process\.argv\[1\], import\.meta\.url\)\) \{/);
      expect(source, script).not.toMatch(/resolve\(process\.argv\[1\]\) === fileURLToPath\(import\.meta\.url\)/);
    }
  });
});
