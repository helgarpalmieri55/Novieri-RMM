/**
 * THE A11Y PASS'S `armed` STATE ARMS, OR SAYS IT DID NOT (batch 2 review, H4).
 *
 * `armed` exists to grade an ENABLED destructive confirm (kit requests batch
 * 2, K9): the `dialog` state only ever saw it disabled, which is how red ink
 * on the accent fill went unmeasured. If the bench's subject or the typing
 * changed, the confirm would stay disabled and `armed` would silently grade
 * the disabled button again. So it checks, and fails loudly.
 */
import { describe, expect, it } from "vitest";

import { STATES, type StatePage } from "../../scripts/a11y-browser.mjs";

/** A page whose confirm stays disabled, or arms, after the subject is typed. */
function page(disabledAfterTyping: boolean): StatePage & { typed: string[] } {
  const typed: string[] = [];
  return {
    typed,
    getByRole: () => ({ first: () => ({ click: async () => {} }) }),
    locator: () => ({
      fill: async (value: string) => {
        typed.push(value);
      },
      nth: () => ({ isDisabled: async () => disabledAfterTyping }),
    }),
    keyboard: { press: async () => {} },
    waitForTimeout: async () => {},
  };
}

describe("the a11y pass's armed state (batch 2 review, H4)", () => {
  it("types the delete confirmation's subject", async () => {
    const armedPage = page(false);
    await STATES.armed?.(armedPage);
    expect(armedPage.typed).toEqual(["ws-alpha.corp.local"]);
  });

  it("fails loudly when the confirm is still disabled, rather than grade the disabled button", async () => {
    await expect(STATES.armed?.(page(true))).rejects.toThrow(/armed: the destructive confirm is still disabled/);
  });
});
