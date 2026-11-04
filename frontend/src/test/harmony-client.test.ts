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

import { VIEWPORTS, chromeProblems, viewportLabel } from "../../scripts/harmony.mjs";
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
