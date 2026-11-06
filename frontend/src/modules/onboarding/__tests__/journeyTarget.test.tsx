/**
 * A page opened from the add-a-computer journey (MSP clients phase 3, design
 * §4.4): under «Todos mis clientes» it works on the journey's client, never
 * over a client chosen above, and its site starts at the journey's.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentType } from "react";
import { Link, MemoryRouter, Route, Routes, useSearchParams } from "react-router-dom";
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
function renderAt(path: string, probeKey = "first", Page: ComponentType = Probe) {
  const ui = (key: string) => (
    <MemoryRouter initialEntries={["/", path]} initialIndex={1}>
      <Page key={key} />
    </MemoryRouter>
  );
  const view = render(ui(probeKey));
  return { ...view, remount: (key: string) => view.rerender(ui(key)) };
}

/**
 * A journey page that writes its own query, as two of them do: Enrollment
 * pushes `?tab=`, the subnet tree pushes `?state=` and replaces `?register=`
 * away. Each write is a new history entry -- a new `location.key` -- on the
 * same page, with the journey's `client=` still in it.
 */
function WritingPage() {
  useAdoptJourneyClient();
  const [params, setParams] = useSearchParams();
  function write(name: string, value: string, replace: boolean) {
    const next = new URLSearchParams(params);
    next.set(name, value);
    setParams(next, { replace });
  }
  return (
    <>
      <button type="button" onClick={() => write("tab", "tokens", false)}>
        push
      </button>
      <button type="button" onClick={() => write("state", "assignable", true)}>
        replace
      </button>
      <output>{params.toString()}</output>
    </>
  );
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

  it("adopts on arrival from a link, as from the journey's page", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/devices/onboard"]}>
        <Routes>
          <Route
            path="/devices/onboard"
            element={<Link to="/enrollment?client=c-andes&site=s-north">open</Link>}
          />
          <Route path="/enrollment" element={<WritingPage />} />
        </Routes>
      </MemoryRouter>,
    );
    await user.click(screen.getByRole("link", { name: "open" }));
    expect(clientScope.select).toHaveBeenCalledWith("c-andes");
    expect(clientScope.select).toHaveBeenCalledTimes(1);
  });
});

/**
 * THE PAGE'S OWN NAVIGATIONS ARE THE SAME ARRIVAL. A tab clicked, a view
 * chosen or a dialog closed writes the page's query, and so makes a new
 * history entry that keeps the journey's `client=`: it is not the journey
 * opening the page again, and the client the person chose meanwhile stays.
 */
describe("a journey page that writes its own query", () => {
  it("keeps «Todos mis clientes» chosen after an arrival on the journey's client", async () => {
    // The journey's link is followed with its client chosen above (the usual path).
    clientScope.selected = "c-andes";
    const user = userEvent.setup();
    const view = renderAt("/enrollment?client=c-andes&site=s-north", "on-andes", WritingPage);
    expect(clientScope.select).not.toHaveBeenCalled();
    // «Todos mis clientes» above: the page starts again on the same URL.
    clientScope.selected = null;
    view.remount("every-client");
    expect(clientScope.select).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "push" }));
    expect(screen.getByRole("status")).toHaveTextContent("tab=tokens");
    await user.click(screen.getByRole("button", { name: "replace" }));
    expect(screen.getByRole("status")).toHaveTextContent("state=assignable");
    expect(clientScope.select).not.toHaveBeenCalled();
  });

  it("keeps «Todos mis clientes» chosen after the journey's client was adopted", async () => {
    const user = userEvent.setup();
    const view = renderAt("/enrollment?client=c-andes&site=s-north", "first", WritingPage);
    expect(clientScope.select).toHaveBeenCalledTimes(1);
    clientScope.selected = "c-andes";
    view.remount("on-andes");
    clientScope.selected = null;
    view.remount("every-client");
    await user.click(screen.getByRole("button", { name: "push" }));
    await user.click(screen.getByRole("button", { name: "replace" }));
    expect(screen.getByRole("status")).toHaveTextContent("tab=tokens");
    expect(screen.getByRole("status")).toHaveTextContent("state=assignable");
    expect(clientScope.select).toHaveBeenCalledTimes(1);
  });

  it("keeps another client chosen after the arrival, through the page's own navigations", async () => {
    clientScope.clients = [ANDES, { ...ANDES, id: "c-other", name: "Other" }];
    const user = userEvent.setup();
    const view = renderAt("/enrollment?client=c-andes&site=s-north", "first", WritingPage);
    expect(clientScope.select).toHaveBeenCalledTimes(1);
    // The person then picks another of their clients above...
    clientScope.selected = "c-other";
    view.remount("on-other");
    await user.click(screen.getByRole("button", { name: "push" }));
    // ...and «Todos mis clientes» after that: each is the person's choice.
    clientScope.selected = null;
    view.remount("every-client");
    await user.click(screen.getByRole("button", { name: "replace" }));
    expect(clientScope.select).toHaveBeenCalledTimes(1);
  });
});
