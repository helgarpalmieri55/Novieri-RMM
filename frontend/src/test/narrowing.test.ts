/**
 * The narrowing double (Task 5 review, fix rounds 1 and 2): a read that should
 * have been narrowed and was not must fail a test. The first version answered
 * `forClient` with the same double, and could not; the second needed every
 * test to assert `unnarrowed`, and a test that forgot passed with a leak.
 */
import { describe, expect, it, vi } from "vitest";

import { recordingNarrowing } from "./narrowing";

function wrapped() {
  return {
    me: vi.fn(async () => "me"),
    getRecord: vi.fn(async (id: string) => `record ${id}`),
    listThings: vi.fn(async (filter: { state: string }) => `things ${filter.state}`),
    forClient: vi.fn(),
  };
}

describe("recordingNarrowing", () => {
  it("rejects, and records, a read the console's API does not allow", async () => {
    const api = wrapped();
    const narrowing = recordingNarrowing(api);
    await expect(narrowing.api.listThings({ state: "open" })).rejects.toThrow("un-narrowed");
    expect(narrowing.unnarrowed).toEqual(["listThings"]);
    expect(api.listThings).not.toHaveBeenCalled();
    // The leak was this test's subject and is asserted above: emptied, so the
    // helper's own check at the end of the test does not fail it.
    narrowing.unnarrowed.length = 0;
  });

  // EXPECTED TO FAIL, and that failure is the point: the body leaks a read and
  // asserts nothing, and the helper's own end-of-test check must fail it.
  // Without that check the body passes and `it.fails` reports "Expect test to fail".
  it.fails("fails a test that leaks an un-narrowed read and never asserts it", async () => {
    const narrowing = recordingNarrowing(wrapped());
    await narrowing.api.listThings({ state: "open" }).catch(() => undefined);
  });

  it("answers /me and what the test allows on the console's API, through the wrapped API", async () => {
    const api = wrapped();
    const narrowing = recordingNarrowing(api, { console: ["getRecord"] });
    expect(await narrowing.api.me()).toBe("me");
    expect(await narrowing.api.getRecord("c1")).toBe("record c1");
    expect(narrowing.unnarrowed).toEqual([]);
  });

  it("gives each client its own double, whose spies call the wrapped API", async () => {
    const api = wrapped();
    const narrowing = recordingNarrowing(api);
    const andes = narrowing.api.forClient("andes") as unknown as ReturnType<typeof wrapped>;
    const other = narrowing.api.forClient("other") as unknown as ReturnType<typeof wrapped>;
    expect(andes).not.toBe(other);
    expect(await andes.listThings({ state: "active" })).toBe("things active");
    expect(narrowing.client("andes").listThings).toHaveBeenCalledWith({ state: "active" });
    expect(narrowing.client("other").listThings).not.toHaveBeenCalled();
    expect(narrowing.narrowedTo).toEqual(["andes", "other"]);
    // A narrowed API narrowed again is the other client's, as `ApiClient.forClient` is.
    expect(andes.forClient("other")).toBe(other);
  });
});
