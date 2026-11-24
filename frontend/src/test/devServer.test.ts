/**
 * THE DEV SERVER A BROWSER PASS STARTS IS STOPPED WHOLE (Task 3, fix round 1,
 * item 10). `harmony.mjs` and `a11y-browser.mjs` spawned `npx vite` and, to
 * stop it, signalled `npx` alone: the `vite` it had started kept running,
 * reparented to init, still holding the worktree's `node_modules/.vite`. One
 * session left four of them behind, which is the shared-cache trap the
 * project's constraints describe (`504 (Outdated Optimize Dep)`).
 *
 * `scripts/devServer.mjs` spawns the server in a process group of its own and
 * stops the group; a stop that leaves a member alive is an error the script
 * reports, never a silent success. A shell that starts two `sleep`s stands in
 * for `npx` starting `vite`: the same shape, a parent whose children outlive
 * a signal sent only to it.
 */
import { describe, expect, it } from "vitest";

import { assertNoSurvivor, livingMembers, spawnGroup } from "../../scripts/devServer.mjs";

/** A parent with two children, as `npx` has `vite`. */
const PARENT_WITH_CHILDREN = ["-c", "sleep 30 & sleep 30 & wait"];

async function until(check: () => boolean, ms = 5000): Promise<void> {
  const deadline = Date.now() + ms;
  while (!check()) {
    if (Date.now() > deadline) throw new Error("timed out");
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
}

// POSIX process groups: Windows has none to signal (review fix round 2, item 4).
describe.skipIf(process.platform === "win32")("scripts/devServer.mjs", () => {
  it("stops every process the server started, not only the one it spawned", async () => {
    const server = spawnGroup("sh", PARENT_WITH_CHILDREN, { stdio: "ignore" });
    await until(() => livingMembers(server.pgid).length >= 3);
    await server.stop();
    expect(livingMembers(server.pgid)).toEqual([]);
  });

  it("is not vacuous: a signal to the spawned process alone leaves its children running", async () => {
    const server = spawnGroup("sh", PARENT_WITH_CHILDREN, { stdio: "ignore" });
    await until(() => livingMembers(server.pgid).length >= 3);
    const exited = new Promise((resolve) => server.child.once("exit", resolve));
    server.child.kill("SIGTERM");
    await exited;
    try {
      expect(livingMembers(server.pgid).length).toBeGreaterThanOrEqual(2);
    } finally {
      await server.stop();
    }
    expect(livingMembers(server.pgid)).toEqual([]);
  });

  it("names a survivor instead of reporting a clean stop", async () => {
    const server = spawnGroup("sh", PARENT_WITH_CHILDREN, { stdio: "ignore" });
    await until(() => livingMembers(server.pgid).length >= 3);
    try {
      expect(() => assertNoSurvivor(server.pgid, "vite")).toThrow(/vite.*survived/);
    } finally {
      await server.stop();
    }
    expect(() => assertNoSurvivor(server.pgid, "vite")).not.toThrow();
  });
});
