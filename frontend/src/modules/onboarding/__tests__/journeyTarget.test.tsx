/**
 * A page opened from the add-a-computer journey (MSP clients phase 3, design
 * §4.4): under «Todos mis clientes» it works on the journey's client, never
 * over a client chosen above, and its site starts at the journey's.
 */
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { MeClient } from "../../../lib/api";
import { useAdoptJourneyClient, useJourneySiteState } from "../journeyTarget";

const clientScope = vi.hoisted(() => ({
  selected: null as string | null,
  clients: [] as MeClient[],
  select: vi.fn(),
  ready: true,
}));
vi.mock("../../../lib/clientScope", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../lib/clientScope")>()),
  useClientScope: () => ({
    selected: clientScope.selected,
    select: clientScope.select,
    clients: clientScope.clients,
    ready: clientScope.ready,
  }),
}));

const ANDES: MeClient = { id: "c-andes", name: "Andes", kind: "customer", enabled_modules: [], permissions: [] };

function Probe() {
  useAdoptJourneyClient();
  const [site] = useJourneySiteState();
  return <p>{site ?? "none"}</p>;
}

// THE PAGE IS THE SECOND HISTORY ENTRY, so each render arrives on an entry
// with its own `location.key`: the first entry's key is always "default", and
// the hook remembers the entries it has decided on (`journeyTarget.ts`).
function renderAt(path: string, probeKey = "first") {
  const ui = (key: string) => (
    <MemoryRouter initialEntries={["/", path]} initialIndex={1}>
      <Probe key={key} />
    </MemoryRouter>
  );
  const view = render(ui(probeKey));
  return { ...view, remount: (key: string) => view.rerender(ui(key)) };
}

beforeEach(() => {
  clientScope.selected = null;
  clientScope.clients = [ANDES];
  clientScope.select = vi.fn();
  clientScope.ready = true;
});

describe("a page opened from the journey", () => {
  it("makes an every-client console work on the journey's client", () => {
    renderAt("/enrollment?client=c-andes&site=s-north");
    expect(clientScope.select).toHaveBeenCalledWith("c-andes");
    expect(clientScope.select).toHaveBeenCalledTimes(1);
  });

  it("never overrides a client chosen above, and ignores that client's site", () => {
    clientScope.selected = "c-other";
    renderAt("/enrollment?client=c-andes&site=s-north");
    expect(clientScope.select).not.toHaveBeenCalled();
    expect(screen.getByText("none")).toBeInTheDocument();
  });

  it("ignores a client that is not one of the person's", () => {
    clientScope.clients = [];
    renderAt("/enrollment?client=c-andes&site=s-north");
    expect(clientScope.select).not.toHaveBeenCalled();
  });

  it("starts the site at the journey's once the console works on its client", () => {
    clientScope.selected = "c-andes";
    renderAt("/enrollment?client=c-andes&site=s-north");
    expect(screen.getByText("s-north")).toBeInTheDocument();
  });

  it("adopts once per arrival: every client chosen afterwards is the person's choice", () => {
    const view = renderAt("/enrollment?client=c-andes&site=s-north");
    expect(clientScope.select).toHaveBeenCalledTimes(1);
    // The console now works on the journey's client: the page starts again.
    clientScope.selected = "c-andes";
    view.remount("on-andes");
    expect(screen.getByText("s-north")).toBeInTheDocument();
    // The person picks «Todos mis clientes» above (or in another tab): the
    // page starts again on the same URL, and is not switched back.
    clientScope.selected = null;
    view.remount("every-client");
    expect(clientScope.select).toHaveBeenCalledTimes(1);
    expect(screen.getByText("none")).toBeInTheDocument();
  });

  it("waits for `/me` to list the person's clients before deciding", () => {
    // Before `/me` answers every person has no clients: that is not "not one
    // of the person's", and deciding on it would never adopt.
    clientScope.ready = false;
    clientScope.clients = [];
    const view = renderAt("/enrollment?client=c-andes&site=s-north");
    expect(clientScope.select).not.toHaveBeenCalled();
    clientScope.ready = true;
    clientScope.clients = [ANDES];
    view.remount("first");
    expect(clientScope.select).toHaveBeenCalledWith("c-andes");
    expect(clientScope.select).toHaveBeenCalledTimes(1);
  });
});
