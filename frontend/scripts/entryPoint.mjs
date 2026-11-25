/**
 * WHETHER THIS MODULE IS THE SCRIPT NODE WAS ASKED TO RUN.
 *
 * `harmony.mjs` and `a11y-browser.mjs` run their `main()` only when executed,
 * so a vitest guard can import their pure helpers without starting a dev
 * server and a browser.
 *
 * THROUGH `realpathSync`, BOTH SIDES (P3 adoption pass; batch 3 review,
 * Minor 1). Node gives a module its REAL path in `import.meta.url`, while
 * `process.argv[1]` is the path as typed. Compared as they were, a gate
 * invoked through a symlink (a linked checkout, a `.bin`-style link) found
 * the two different, never ran `main()`, and exited 0 having measured
 * nothing: a gate that fails open. A path that does not exist is compared as
 * written, so it can only be false.
 */
import { realpathSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

function real(path) {
  try {
    return realpathSync(path);
  } catch {
    return resolve(path);
  }
}

/** `argv1` is `process.argv[1]`; `moduleUrl` the caller's `import.meta.url`. */
export function isEntryPoint(argv1, moduleUrl) {
  if (!argv1) return false;
  return real(resolve(argv1)) === real(fileURLToPath(moduleUrl));
}
