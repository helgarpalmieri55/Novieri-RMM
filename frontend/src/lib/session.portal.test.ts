/** The portal's sign-in page among the public ones (own-auth phase 2, Task 10). */
import { describe, expect, it } from "vitest";

import { PORTAL_SIGN_IN_PATH, isPublicPath, safeNext, signInPath } from "./session";

describe("the portal's sign-in page", () => {
  it("is public, and the portal itself is not", () => {
    expect(PORTAL_SIGN_IN_PATH).toBe("/portal/sign-in");
    expect(isPublicPath("/portal/sign-in")).toBe(true);
    expect(isPublicPath("/portal")).toBe(false);
    expect(isPublicPath("/portal/tickets/t1")).toBe(false);
  });

  it("is where a portal page sends a person to sign in, carrying the page", () => {
    expect(signInPath("/portal")).toBe("/portal/sign-in?next=%2Fportal");
    expect(signInPath("/portal/tickets/t1")).toBe("/portal/sign-in?next=%2Fportal%2Ftickets%2Ft1");
    expect(signInPath("/devices")).toBe("/sign-in?next=%2Fdevices");
    expect(signInPath()).toBe("/sign-in");
  });

  it("is never a page to come back to", () => {
    expect(safeNext("/portal/sign-in")).toBe("/");
  });
});
