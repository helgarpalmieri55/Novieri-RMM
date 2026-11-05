/**
 * The dev fixtures answer as the server does (final review D2, Minor 4).
 * Fixture mode is what the harmony walk and a developer see; a fixture that
 * answers differently from the server shows a screen production never draws.
 *
 *   - cancelling an invitation that is not pending: the server's 409 with its
 *     sentence (`service._require_pending_invitation`), not a 404;
 *   - a password change: the server ends the person's OTHER sessions too
 *     (`account.change_password`), not only the remembered devices.
 */
import { describe, expect, it } from "vitest";

import { ApiError } from "../lib/api";
import profile from "../modules/profile/fixtures";
import settings from "../modules/settings/fixtures";

describe("the dev fixtures, against the server's answers", () => {
  it("refuse to cancel an invitation that is not pending with the server's 409", async () => {
    let refusal: unknown;
    try {
      await settings.cancelInvite!("usr-not-invited");
    } catch (error: unknown) {
      refusal = error;
    }
    expect(refusal).toBeInstanceOf(ApiError);
    expect((refusal as ApiError).status).toBe(409);
    expect(JSON.parse((refusal as ApiError).message)).toEqual({ detail: "this person has no pending invitation" });
  });

  it("end the other sessions on a password change, and keep the one that changed it", async () => {
    const before = await profile.getMySecurity!();
    expect(before.sessions.filter((session) => !session.current).length).toBeGreaterThan(0);

    await profile.changePassword!({ current: "an old phrase of mine", new: "correct horse battery staple" });

    const after = await profile.getMySecurity!();
    expect(after.sessions.map((session) => session.current)).toEqual([true]);
    expect(after.trusted_devices).toEqual([]);
  });
});
