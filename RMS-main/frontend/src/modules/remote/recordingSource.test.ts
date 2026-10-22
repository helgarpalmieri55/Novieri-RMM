import { describe, expect, it, vi } from "vitest";

import { recordingSource } from "./recordingSource";

/**
 * The adapter that made playback possible at all.
 *
 * `guacamole-common-js` 1.5.0 cannot read a recording from a Blob (its
 * `recordingBlob` is never assigned, so the constructor dereferences
 * `undefined.size`), and the console's answer was to catch the `TypeError`
 * and print it. These tests hold the replacement to the contract
 * `SessionRecording` expects of a tunnel: instructions first, then CLOSED,
 * and an error reported BEFORE the close so the library does not treat a
 * failed read as a complete recording.
 */
describe("recordingSource", () => {
  /** One Guacamole instruction: `LEN.OPCODE,LEN.ARG;`. */
  const instruction = (opcode: string, ...args: string[]) =>
    [opcode, ...args].map((part) => `${part.length}.${part}`).join(",") + ";";

  function collect(source: ReturnType<typeof recordingSource>) {
    const instructions: [string, string[]][] = [];
    const states: number[] = [];
    const errors: string[] = [];
    source.oninstruction = (opcode, args) => instructions.push([opcode, args]);
    source.onstatechange = (state) => states.push(state);
    source.onerror = (status) => errors.push(status.message ?? "");
    return { instructions, states, errors };
  }

  it("replays the instructions in the file, then closes", async () => {
    const blob = new Blob([instruction("size", "0", "1024", "768") + instruction("sync", "0")]);
    const source = recordingSource(blob);
    const seen = collect(source);

    source.connect();
    await vi.waitFor(() => expect(seen.states).toContain(2));

    expect(seen.instructions).toEqual([
      ["size", ["0", "1024", "768"]],
      ["sync", ["0"]],
    ]);
    // OPEN before CLOSED, and CLOSED last: the library reads CLOSED as "the
    // whole recording has now been handed over".
    expect(seen.states).toEqual([1, 2]);
    expect(seen.errors).toEqual([]);
  });

  // An empty archive file is a real state, not a broken one: a session that
  // ended before anything was drawn records no instructions. It must load
  // cleanly and simply hold nothing, because the page's answer to "no frames"
  // is an empty state and its answer to "unreadable" is an alarm.
  it("loads an empty recording without reporting an error", async () => {
    const source = recordingSource(new Blob([]));
    const seen = collect(source);

    source.connect();
    await vi.waitFor(() => expect(seen.states).toContain(2));

    expect(seen.instructions).toEqual([]);
    expect(seen.errors).toEqual([]);
  });

  it("reports a file that is not a recording, and reports it before closing", async () => {
    // An illegal terminator: the length prefix is well formed, the character
    // that should close the element is not. A file of ordinary prose is NOT
    // this case — the parser simply buffers it waiting for a terminator that
    // never comes, which is a recording that never completes, not one that
    // fails.
    const source = recordingSource(new Blob(["4.size?1.0;"]));
    collect(source);
    const order: string[] = [];
    source.onerror = () => order.push("error");
    source.onstatechange = (state) => order.push(state === 2 ? "closed" : "open");

    source.connect();
    await vi.waitFor(() => expect(order).toContain("closed"));

    // `SessionRecording` only skips `onload` when it saw the error first: the
    // other order would announce a complete recording that never parsed.
    expect(order).toEqual(["open", "error", "closed"]);
  });

  it("drops a read that finished after the player was torn down", async () => {
    const source = recordingSource(new Blob([instruction("sync", "0")]));
    const seen = collect(source);

    source.connect();
    source.disconnect();
    await Promise.resolve();
    await Promise.resolve();

    expect(seen.instructions).toEqual([]);
    // CLOSED exactly once, from the disconnect: a second one would have the
    // library treat a torn-down player as a freshly loaded recording.
    expect(seen.states.filter((state) => state === 2)).toHaveLength(1);
  });
});

/**
 * THE FIXTURE IS A REAL RECORDING, and this is what makes that claim
 * checkable. `/remote/sess-1/recording` in fixture mode is the only place the
 * player is exercised end to end, and an archive file that does not parse
 * would leave that route drawing an empty state that looks exactly like the
 * defect this cycle fixed.
 *
 * BREAKS AGAINST: a fixture log with a malformed instruction, or one with no
 * `sync` in it (no `sync` means no frame, and no frame means no duration).
 */
describe("the fixture recording", () => {
  it("parses, and holds four frames over four seconds", async () => {
    const { RECORDING_LOG } = await import("./fixtures");
    const source = recordingSource(new Blob([RECORDING_LOG]));
    const opcodes: string[] = [];
    const syncs: string[] = [];
    const errors: string[] = [];
    let closed = false;
    source.oninstruction = (opcode, args) => {
      opcodes.push(opcode);
      if (opcode === "sync") syncs.push(args[0] as string);
    };
    source.onerror = (status) => errors.push(status.message ?? "");
    source.onstatechange = (state) => {
      if (state === 2) closed = true;
    };

    source.connect();
    await vi.waitFor(() => expect(closed).toBe(true));

    expect(errors).toEqual([]);
    expect(opcodes[0]).toBe("size");
    expect(syncs).toEqual(["0", "1200", "2400", "3600"]);
  });
});
