/**
 * THE HARMONY GATE SEES A CLIENT CHOSEN IN THE TOP BAR (phase 3 final review,
 * Important 5).
 *
 * Until now it could not: the fixture clients' ids were not UUIDs, so the
 * console's `readStoredClient` threw any of them away, and the gate only ever
 * seeded the language. So no state with a client chosen -- the mobile top
 * bar's client mark (operator decision 4), the desktop selector beside the
 * labelled "Add a computer" between 641 and 1024 px -- had ever been rendered
 * in a real browser.
 *
 * What is pinned here is what the browser run cannot say about itself: that
 * it walks those states, that the client it chooses is one the console keeps
 * and `/me` lists, and what it calls a broken top bar.
 */
import { describe, expect, it } from "vitest";

import { VIEWPORTS, chromeProblems, markDrawn, viewportLabel } from "../../scripts/harmony.mjs";
import type { Viewport } from "../../scripts/harmony.mjs";
import { CHOSEN_CLIENT, PARAMS, ROUTES } from "../../scripts/routes.mjs";
import { fixtureClient } from "../fixtures/client";
import { isClientId } from "../lib/clientChoice";

describe("the harmony gate's viewports", () => {
  it("keeps the two the spec names, with every client", () => {
    expect(VIEWPORTS).toContainEqual({ width: 1440, height: 900, coarse: false, client: false });
    expect(VIEWPORTS).toContainEqual({ width: 390, height: 844, coarse: true, client: false });
  });

  it("walks a client chosen on the phone and at the first width the bar holds the selector", () => {
    expect(VIEWPORTS).toContainEqual({ width: 390, height: 844, coarse: true, client: true });
    expect(VIEWPORTS).toContainEqual({ width: 641, height: 900, coarse: false, client: true });
  });

  it("files each viewport's screenshots under a name of its own", () => {
    const labels = VIEWPORTS.map(viewportLabel);
    expect(new Set(labels).size).toBe(labels.length);
    expect(labels).toContain("390x844");
    expect(labels).toContain("390x844-client");
  });
});

describe("the client the gate chooses", () => {
  it("is a UUID, the only thing the console reads back from itms.client", () => {
    expect(isClientId(CHOSEN_CLIENT)).toBe(true);
  });

  it("is a customer the fixture /me lists, so the console keeps it rather than falling back", async () => {
    const me = await fixtureClient().me();
    expect((me.clients ?? []).find((client) => client.id === CHOSEN_CLIENT)).toMatchObject({
      kind: "customer",
      name: "Andes Logistics",
    });
  });

  it("is the client whose page the gate opens", () => {
    expect(PARAMS.clientId).toBe(CHOSEN_CLIENT);
    expect(ROUTES).toContain(`/clients/${CHOSEN_CLIENT}`);
  });
});

describe("what the gate calls a broken top bar", () => {
  const fine = { toggle: { width: 66, height: 44 }, profileRight: 380, innerWidth: 390 };

  it("passes a drawer toggle of 44 to 70 px and an account menu 10 px clear of the edge", () => {
    expect(chromeProblems(fine)).toEqual([]);
    expect(chromeProblems({ ...fine, toggle: { width: 44, height: 44 } })).toEqual([]);
    expect(chromeProblems({ ...fine, toggle: { width: 70, height: 50 } })).toEqual([]);
  });

  it("refuses a toggle the client mark widened past 70 px", () => {
    expect(chromeProblems({ ...fine, toggle: { width: 72, height: 44 } })).toEqual([
      ".nav-toggle is 72px wide (44-70)",
    ]);
  });

  it("refuses a toggle squeezed below a 44 px target, either way", () => {
    expect(chromeProblems({ ...fine, toggle: { width: 40, height: 44 } })).toEqual([
      ".nav-toggle is 40px wide (44-70)",
    ]);
    expect(chromeProblems({ ...fine, toggle: { width: 44, height: 36 } })).toEqual([
      ".nav-toggle is 36px tall (at least 44)",
    ]);
  });

  it("refuses an account menu pushed to the edge of the screen", () => {
    expect(chromeProblems({ ...fine, profileRight: 385 })).toEqual([
      ".profile-btn ends at 385px, past innerWidth - 10 (380)",
    ]);
  });

  it("says nothing about a toggle the layout does not draw, or a page with no top bar", () => {
    expect(chromeProblems({ toggle: null, profileRight: 1416, innerWidth: 1440 })).toEqual([]);
    expect(chromeProblems({ toggle: null, profileRight: null, innerWidth: 1440 })).toEqual([]);
  });
});

// Phase 3 last fix round, item 7. The 44-70 px band alone passed a phone bar
// whose client mark had WRAPPED AWAY: the toggle then shrinks back to the
// plain 44 px icon, and the one place the bar shows the client is gone. With
// a client chosen on a handset the mark is drawn (`AppShell`'s `marked`), so
// the toggle holds it: 68 px today (`shell.css`), never under 60.
describe("the client mark on the phone, with a client chosen", () => {
  const at = (width: number, client: boolean): Viewport => {
    const found = VIEWPORTS.find((vp) => vp.width === width && vp.client === client);
    if (!found) throw new Error(`no ${width} viewport with client ${String(client)}`);
    return found;
  };
  const phone = { toggle: { width: 68, height: 44 }, profileRight: 380, innerWidth: 390, topbar: true };

  it("is expected at 390x844-client only", () => {
    expect(VIEWPORTS.filter(markDrawn).map(viewportLabel)).toEqual(["390x844-client"]);
  });

  it("passes the toggle holding it, 60 to 70 px", () => {
    expect(chromeProblems(phone, at(390, true))).toEqual([]);
    expect(chromeProblems({ ...phone, toggle: { width: 60, height: 44 } }, at(390, true))).toEqual([]);
  });

  it("refuses a toggle the mark wrapped out of: the plain 44 px icon", () => {
    expect(chromeProblems({ ...phone, toggle: { width: 44, height: 44 } }, at(390, true))).toEqual([
      ".nav-toggle is 44px wide (60-70: the client mark is drawn)",
    ]);
    expect(chromeProblems({ ...phone, toggle: { width: 59, height: 44 } }, at(390, true))).toEqual([
      ".nav-toggle is 59px wide (60-70: the client mark is drawn)",
    ]);
  });

  it("refuses a phone bar with a client chosen that draws no toggle at all", () => {
    expect(chromeProblems({ ...phone, toggle: null }, at(390, true))).toEqual([
      ".nav-toggle is not drawn (the client mark needs it)",
    ]);
    // A page with no top bar has no mark to draw.
    expect(chromeProblems({ ...phone, toggle: null, topbar: false }, at(390, true))).toEqual([]);
  });

  it("asks nothing more of the plain icon where no mark is drawn", () => {
    const plain = { ...phone, toggle: { width: 44, height: 44 } };
    expect(chromeProblems(plain, at(390, false))).toEqual([]);
    expect(chromeProblems({ ...plain, innerWidth: 641, profileRight: 630 }, at(641, true))).toEqual([]);
    expect(chromeProblems({ toggle: null, profileRight: 1416, innerWidth: 1440, topbar: true }, at(1440, false))).toEqual(
      [],
    );
  });
});
