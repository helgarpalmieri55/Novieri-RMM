/** Types for `devServer.mjs`, so `src/test/devServer.test.ts` imports it under `strict`. */
import type { ChildProcess, SpawnOptions } from "node:child_process";

export interface GroupServer {
  child: ChildProcess;
  /** The process group's id: the spawned process is its leader. */
  pgid: number;
  /** SIGTERM to the whole group, SIGKILL after the grace period; rejects naming any survivor. */
  stop(): Promise<void>;
}

export declare function livingMembers(pgid: number): number[];
export declare function assertNoSurvivor(pgid: number, what?: string): void;
export declare function spawnGroup(
  command: string,
  args: readonly string[],
  options?: SpawnOptions,
  settings?: { what?: string; graceMs?: number },
): GroupServer;
