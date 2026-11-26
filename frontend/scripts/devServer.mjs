/**
 * A DEV SERVER A SCRIPT STARTS, AND STOPS WHOLE (Task 3, fix round 1, item 10).
 *
 * `harmony.mjs` and `a11y-browser.mjs` start `npx vite` and used to stop it
 * with `child.kill("SIGTERM")`, which signals `npx` and nothing else. The
 * `vite` that `npx` had started kept running, reparented to init, still
 * serving and still holding the worktree's `node_modules/.vite`: one session
 * left four of them behind, and a dev server sharing that cache with a later
 * pass is how a page comes back `504 (Outdated Optimize Dep)` (the project's
 * Global Constraints, "Worktrees").
 *
 * So the server is spawned in a PROCESS GROUP OF ITS OWN (`detached`), and
 * stopping it signals the group: `npx`, `vite` and anything `vite` started.
 * `stop()` then CHECKS: it resolves only when no member of the group is left,
 * and rejects, naming the survivors, when one outlives SIGKILL. A script that
 * exits some other way (an exception, Ctrl-C) takes the group with it.
 *
 * Linux reads `/proc` to tell a living member from a zombie a container's init
 * has not reaped yet (a zombie still answers `kill(-pgid, 0)`); elsewhere the
 * group's existence is the answer.
 */
import { spawn } from "node:child_process";
import { existsSync, readdirSync, readFileSync } from "node:fs";

/** The members of process group `pgid` that are still running (zombies excluded). */
export function livingMembers(pgid) {
  if (existsSync("/proc/self/stat")) {
    const living = [];
    for (const name of readdirSync("/proc")) {
      if (!/^\d+$/.test(name)) continue;
      let stat;
      try {
        stat = readFileSync(`/proc/${name}/stat`, "utf8");
      } catch {
        continue; // gone between the listing and the read
      }
      // `pid (comm) state ppid pgrp ...`; `comm` may hold spaces and parentheses.
      const fields = stat.slice(stat.lastIndexOf(")") + 2).split(" ");
      const state = fields[0];
      const pgrp = Number(fields[2]);
      if (pgrp === pgid && state !== "Z" && state !== "X") living.push(Number(name));
    }
    return living;
  }
  try {
    process.kill(-pgid, 0);
    return [pgid];
  } catch (err) {
    return err && err.code === "EPERM" ? [pgid] : [];
  }
}

/** Throws, naming them, when any member of the group is still running. */
export function assertNoSurvivor(pgid, what = "dev server") {
  const left = livingMembers(pgid);
  if (left.length > 0) {
    throw new Error(`a ${what} process from this run survived its stop: pid ${left.join(", ")} (process group ${pgid})`);
  }
}

function signalGroup(pgid, signal) {
  try {
    process.kill(-pgid, signal);
  } catch (err) {
    if (!err || err.code !== "ESRCH") throw err;
  }
}

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Spawns `command` as the leader of a new process group. Returns the child,
 * the group's id and `stop()`: SIGTERM to the group, SIGKILL after
 * `graceMs`, then the survivor check.
 */
export function spawnGroup(command, args, options = {}, { what = "dev server", graceMs = 5000 } = {}) {
  const child = spawn(command, args, { ...options, detached: true });
  const pgid = child.pid;
  // A script that ends without calling `stop()` -- an exception, `process.exit`,
  // Ctrl-C -- still takes the group with it.
  const reap = () => signalGroup(pgid, "SIGKILL");
  const onSignal = (signal) => {
    reap();
    process.exit(signal === "SIGINT" ? 130 : 143);
  };
  process.on("exit", reap);
  process.on("SIGINT", onSignal);
  process.on("SIGTERM", onSignal);
  let stopping;
  const stop = () =>
    (stopping ??= (async () => {
      try {
        signalGroup(pgid, "SIGTERM");
        const deadline = Date.now() + graceMs;
        while (livingMembers(pgid).length > 0 && Date.now() < deadline) await pause(50);
        if (livingMembers(pgid).length > 0) {
          signalGroup(pgid, "SIGKILL");
          const killed = Date.now() + 2000;
          while (livingMembers(pgid).length > 0 && Date.now() < killed) await pause(50);
        }
        assertNoSurvivor(pgid, what);
      } finally {
        process.off("exit", reap);
        process.off("SIGINT", onSignal);
        process.off("SIGTERM", onSignal);
      }
    })());
  return { child, pgid, stop };
}
