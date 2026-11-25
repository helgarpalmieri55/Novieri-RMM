import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { useState, type ReactElement } from "react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { MeClient } from "../lib/api";
import { renderPage } from "../test/renderPage";
import {
  BulkBar,
  ClientFilter,
  FilterChips,
  ListEmpty,
  ShowingCount,
  useNarrowedApi,
  useNewParam,
  useUrlFilters,
} from "./listKit";
// The pieces kit requests batch 1 added, imported through `components.tsx`,
// the kit's one door, so a piece that is not re-exported there fails here.
import {
  ChipSelect,
  formatUrlList,
  MultiFilterChips,
  parseUrlList,
  useEditParam as useEditParamFromKit,
  withClient,
} from "./components";

const ANDES = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57";
const BOGOTA = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";
/** A well-formed client id that is not one of this person's (a stale or edited link). */
const FOREIGN = "9d1f0c2a-4b3e-4c5d-8e6f-7a8b9c0d1e2f";

const scope = vi.hoisted(() => ({ selected: null as string | null, clients: [] as unknown[], ready: true }));
vi.mock("../lib/clientScope", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/clientScope")>()),
  useClientScope: () => ({ selected: scope.selected, clients: scope.clients, select: vi.fn(), ready: scope.ready }),
}));
const api = vi.hoisted(() => ({ forClient: vi.fn((id: string) => ({ narrowedTo: id })), listThings: vi.fn(async () => "rows") }));
vi.mock("../lib/ApiContext", () => ({ useApi: () => api }));

function client(id: string, name: string): MeClient {
  return { id, name, kind: "customer", enabled_modules: [], permissions: [] } as unknown as MeClient;
}

beforeEach(() => {
  scope.selected = null;
  scope.ready = true;
  scope.clients = [client(ANDES, "Andes Logistics"), client(BOGOTA, "Bogotá Retail")];
  api.forClient.mockClear();
  api.listThings.mockClear();
});

function Fleet() {
  const filters = useUrlFilters(["state", "q"] as const);
  const online = filters.values.state === "online";
  return (
    <>
      <input aria-label="Search" value={filters.values.q} onChange={(event) => filters.set("q", event.target.value)} />
      <FilterChips
        label="State"
        options={[
          { value: "", label: "All", count: 12 },
          { value: "online", label: "Online", count: 9 },
          { value: "offline", label: "Offline" },
        ]}
        value={filters.values.state}
        onChange={(value) => filters.set("state", value)}
      />
      <ShowingCount shown={online ? 9 : 12} total={12} />
      {filters.values.state === "offline" ? (
        <ListEmpty filtered={filters.active} onClear={filters.clear} nothingYet="No computers yet." />
      ) : null}
    </>
  );
}

describe("useUrlFilters and FilterChips (design §2: filters live in the URL)", () => {
  it("reads the filter from the URL, so a filtered view is a link", () => {
    renderPage("/devices?state=online", <Fleet />);
    expect(screen.getByRole("button", { name: /Online/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /All/ })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByText("Showing 9 of 12")).toBeInTheDocument();
  });

  it("writes a chip into the URL, goes back to the first page, and drops the default", async () => {
    const { router } = renderPage("/devices?offset=50", <Fleet />);
    await userEvent.click(screen.getByRole("button", { name: /Online/ }));
    expect(router.state.location.search).toBe("?state=online");
    await userEvent.click(screen.getByRole("button", { name: /All/ }));
    expect(router.state.location.search).toBe("");
  });

  it("draws a count only where the caller has one, never a made-up zero", () => {
    renderPage("/devices", <Fleet />);
    expect(screen.getByRole("button", { name: /All\s*12/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Offline" })).toBeInTheDocument();
  });

  it("lets Back undo a chip, while typing a search adds no history entry of its own", async () => {
    const { router } = renderPage("/devices", <Fleet />);
    await userEvent.click(screen.getByRole("button", { name: /Online/ }));
    await userEvent.type(screen.getByRole("textbox", { name: "Search" }), "srv");
    expect(router.state.location.search).toBe("?state=online&q=srv");
    await act(() => router.navigate(-1));
    expect(router.state.location.search).toBe("");
  });
});

describe("ShowingCount", () => {
  it("never claims a total the server did not give", () => {
    renderPage("/x", <ShowingCount shown={50} total={null} />);
    expect(screen.getByText("Showing 50")).toBeInTheDocument();
  });
});

describe("ListEmpty (design §2: the empty state says the real cause)", () => {
  it("says nothing matches the filters, and clearing them goes back to the whole list", async () => {
    const { router } = renderPage("/devices?state=offline&q=srv", <Fleet />);
    expect(screen.getByText("Nothing matches these filters.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(router.state.location.search).toBe("");
  });

  it("says nothing exists yet, and offers the page's own action", () => {
    renderPage("/x", <ListEmpty filtered={false} onClear={vi.fn()} nothingYet="No computers yet." action={<a href="/devices/onboard">Add a computer</a>} />);
    expect(screen.getByText("No computers yet.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Add a computer" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clear filters" })).toBeNull();
  });
});

describe("BulkBar (design §2: only where bulk actions exist, and only with a selection)", () => {
  it("is not drawn until something is selected", () => {
    renderPage("/x", <BulkBar count={0} onClear={vi.fn()}><button type="button">Approve</button></BulkBar>);
    expect(screen.queryByRole("region", { name: "Bulk actions" })).toBeNull();
  });

  it("counts the selection and clears it", async () => {
    const clear = vi.fn();
    renderPage("/x", <BulkBar count={3} onClear={clear}><button type="button">Approve</button></BulkBar>);
    expect(screen.getByRole("region", { name: "Bulk actions" })).toHaveTextContent("3 selected");
    await userEvent.click(screen.getByRole("button", { name: "Clear selection" }));
    expect(clear).toHaveBeenCalledTimes(1);
  });
});

/**
 * The chip takes the page's `filters` and writes its own `client` key (review
 * fix round 2, item 2): there is no `onChange` a page could wire so that a
 * stale client is dropped with a history entry, which was the Back trap.
 */
function ChipPage() {
  const filters = useUrlFilters(["client", "state"] as const);
  return <ClientFilter filters={filters} />;
}

describe("ClientFilter (design §1.3: a Client chip while every client is shown)", () => {
  it("offers every client of the person, drawn as a chip", () => {
    renderPage("/x", <ChipPage />);
    const select = screen.getByRole("combobox", { name: "Client" });
    expect([...(select as HTMLSelectElement).options].map((option) => option.textContent)).toEqual([
      "All clients",
      "Andes Logistics",
      "Bogotá Retail",
    ]);
    // Design §1.3 says a "Client" chip: a select in the chip's shape, pinned
    // so a refactor cannot drop the shape (spec pre-flight M-11).
    expect(select.closest(".chip")).toHaveClass("chip-select");
  });

  it("drops a client the person does not have from the URL, instead of drawing All clients over an empty list", async () => {
    const { router } = renderPage(`/x?client=${FOREIGN}&state=open`, <ChipPage />);
    await waitFor(() => expect(router.state.location.search).toBe("?state=open"));
    // In place, so the stale link leaves no entry to come Back to.
    expect(router.state.historyAction).toBe("REPLACE");
  });

  it("writes a person's choice as a new entry, so Back undoes it", async () => {
    const { router } = renderPage("/x", <ChipPage />);
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Client" }), ANDES);
    expect(router.state.location.search).toBe(`?client=${ANDES}`);
    expect(router.state.historyAction).toBe("PUSH");
    await act(() => router.navigate(-1));
    expect(router.state.location.search).toBe("");
  });

  it("is not drawn when the top bar has chosen a client, because that choice already decides", () => {
    scope.selected = ANDES;
    renderPage("/x", <ChipPage />);
    expect(screen.queryByRole("combobox", { name: "Client" })).toBeNull();
  });
});

/**
 * ONE HOOK FOR A LIST BEHIND THE CHIP (review fix round 1, item 4): the API, the
 * client it narrows to (the query key) and whether that is decided, from one
 * call, so a list cannot use one and forget the others.
 */
function Probe({ id }: { id: string }) {
  const narrowed = useNarrowedApi(id).api as unknown as { narrowedTo?: string };
  return <p>{narrowed.narrowedTo ?? "console"}</p>;
}

describe("useNarrowedApi: the API (decision D6)", () => {
  it("narrows to the chip's client with X-Client-Id", () => {
    renderPage("/x", <Probe id={ANDES} />);
    expect(screen.getByText(ANDES)).toBeInTheDocument();
  });

  it("leaves the console's own client alone when the chip is empty, or the top bar has chosen", () => {
    renderPage("/x", <Probe id="" />);
    expect(screen.getByText("console")).toBeInTheDocument();
    scope.selected = BOGOTA;
    renderPage("/y", <Probe id={ANDES} />);
    expect(screen.getAllByText("console")).toHaveLength(2);
  });

  it("never sends a value that is not a client id", () => {
    renderPage("/x", <Probe id="not-a-uuid" />);
    expect(screen.getByText("console")).toBeInTheDocument();
    expect(api.forClient).not.toHaveBeenCalled();
  });

  it("never sends a client the person does not have (decision D17)", () => {
    renderPage("/x", <Probe id={FOREIGN} />);
    expect(screen.getByText("console")).toBeInTheDocument();
    expect(api.forClient).not.toHaveBeenCalled();
  });
});

function Decided({ id }: { id: string }) {
  const narrowing = useNarrowedApi(id);
  return <p>{narrowing.ready ? (narrowing.client ?? "console") : "waiting"}</p>;
}

/** A list behind the chip, keyed on the narrowed client the way Receta L keys one. */
function ChipList() {
  const filters = useUrlFilters(["client"] as const);
  const { api: narrowed, client, ready } = useNarrowedApi(filters.values.client);
  const list = useQuery({
    queryKey: ["probe", client],
    queryFn: async () => (narrowed as unknown as { narrowedTo?: string }).narrowedTo ?? "console",
    enabled: ready,
  });
  return (
    <>
      <ClientFilter filters={filters} />
      <p>read as {list.data ?? "nothing yet"}</p>
    </>
  );
}

/** A list that forgot `enabled: ready`: what the hook's API does before `/me` answers. */
function Forgetful({ id }: { id: string }) {
  const { api: narrowed } = useNarrowedApi(id);
  const list = useQuery({
    queryKey: ["forgetful", id],
    queryFn: () => (narrowed as unknown as { listThings(): Promise<string> }).listThings(),
  });
  return <p>{list.isError ? "refused" : (list.data ?? "pending")}</p>;
}

describe("useNarrowedApi: when it is decided (decision D17)", () => {
  it("decides nothing before /me has listed the person's clients, so a list reads nothing", () => {
    scope.ready = false;
    renderPage("/x", <Decided id={ANDES} />);
    expect(screen.getByText("waiting")).toBeInTheDocument();
  });

  it("is decided at once for an empty chip, or when the top bar has chosen", () => {
    scope.ready = false;
    renderPage("/x", <Decided id="" />);
    scope.selected = BOGOTA;
    renderPage("/y", <Decided id={ANDES} />);
    expect(screen.getAllByText("console")).toHaveLength(2);
  });

  it("refuses every read before /me answers, so even a list without enabled: ready never reads through the console's API", async () => {
    scope.ready = false;
    renderPage("/x", <Forgetful id={ANDES} />);
    expect(await screen.findByText("refused")).toBeInTheDocument();
    expect(api.listThings).not.toHaveBeenCalled();
    expect(api.forClient).not.toHaveBeenCalled();
  });

  it("reads again under the new client when the chip changes, so no row of the last one stays", async () => {
    renderPage("/x", <ChipList />);
    expect(await screen.findByText("read as console")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Client" }), ANDES);
    expect(await screen.findByText(`read as ${ANDES}`)).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Client" }), BOGOTA);
    expect(await screen.findByText(`read as ${BOGOTA}`)).toBeInTheDocument();
    expect(api.forClient).toHaveBeenCalledWith(ANDES);
    expect(api.forClient).toHaveBeenCalledWith(BOGOTA);
  });
});

/** Mounts `element` under a data router with an earlier entry to go Back to. */
function withHistory(entries: string[], pattern: string, element: ReactElement) {
  const router = createMemoryRouter(
    [
      { path: "/start", element: <p>start</p> },
      { path: pattern, element },
    ],
    { initialEntries: entries, initialIndex: entries.length - 1 },
  );
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

/** A page that re-renders on demand, while the chip holds a client. */
function Rerendering() {
  const filters = useUrlFilters(["client"] as const);
  const [renders, setRenders] = useState(0);
  return (
    <>
      <ClientFilter filters={filters} />
      <button type="button" onClick={() => setRenders(renders + 1)}>
        Again
      </button>
    </>
  );
}

describe("ClientFilter and a stale link (review fix rounds 1 and 2)", () => {
  it("drops a foreign client in place, so Back leaves the page instead of coming back to drop it again", async () => {
    const router = withHistory(["/start", `/devices?client=${FOREIGN}`], "/devices", <ChipPage />);
    await waitFor(() => expect(router.state.location.search).toBe(""));
    expect(router.state.location.pathname).toBe("/devices");
    await act(() => router.navigate(-1));
    expect(router.state.location.pathname).toBe("/start");
  });

  it("re-renders of the page write nothing while the chip holds one of the person's clients", async () => {
    const router = withHistory(["/start", `/devices?client=${ANDES}`], "/devices", <Rerendering />);
    await userEvent.click(screen.getByRole("button", { name: "Again" }));
    await userEvent.click(screen.getByRole("button", { name: "Again" }));
    expect(router.state.location.search).toBe(`?client=${ANDES}`);
    await act(() => router.navigate(-1));
    expect(router.state.location.pathname).toBe("/start");
  });
});

describe("history hygiene (review fix round 1, item 6)", () => {
  it("writes nothing when a chip already says so, so Back is not spent on a duplicate", async () => {
    const router = withHistory(["/start", "/devices?state=online"], "/devices", <Fleet />);
    await userEvent.click(screen.getByRole("button", { name: /Online/ }));
    expect(router.state.location.search).toBe("?state=online");
    await act(() => router.navigate(-1));
    expect(router.state.location.pathname).toBe("/start");
  });

  it("closing a dialog opened here goes back, so the next Back leaves the list", async () => {
    const router = withHistory(["/start", "/service-desk"], "/service-desk", <Creating />);
    await userEvent.click(screen.getByRole("button", { name: "New ticket" }));
    expect(router.state.location.search).toBe("?new=ticket");
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(router.state.location.pathname + router.state.location.search).toBe("/service-desk");
    await act(() => router.navigate(-1));
    expect(router.state.location.pathname).toBe("/start");
  });

  it("closing a dialog that arrived in a link replaces, and Back still leaves the list", async () => {
    const router = withHistory(["/start", "/service-desk?new=ticket"], "/service-desk", <Creating />);
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(router.state.location.pathname + router.state.location.search).toBe("/service-desk");
    await act(() => router.navigate(-1));
    expect(router.state.location.pathname).toBe("/start");
  });

  it("closing after the query moved on replaces rather than going back to an older state", async () => {
    const router = withHistory(["/start", "/service-desk"], "/service-desk", <Creating />);
    await userEvent.click(screen.getByRole("button", { name: "New ticket" }));
    await userEvent.click(screen.getByRole("button", { name: "Open only" }));
    expect(router.state.location.search).toBe("?new=ticket&state=open");
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(router.state.location.search).toBe("?state=open");
  });
});


function Creating() {
  const created = useNewParam("ticket");
  const filters = useUrlFilters(["state"] as const);
  return (
    <>
      <button type="button" onClick={created.start}>New ticket</button>
      <button type="button" onClick={() => filters.set("state", "open")}>Open only</button>
      {created.open ? (
        <div role="dialog" aria-label="New ticket">
          <button type="button" onClick={created.close}>Close</button>
        </div>
      ) : null}
    </>
  );
}

describe("useNewParam (decision D8)", () => {
  it("opens a create dialog from ?new=, which is what the New menu links to", () => {
    renderPage("/service-desk?new=ticket", <Creating />);
    expect(screen.getByRole("dialog", { name: "New ticket" })).toBeInTheDocument();
  });

  it("puts the dialog in the URL when opened here, and takes it out when closed", async () => {
    const { router } = renderPage("/service-desk?state=open", <Creating />);
    await userEvent.click(screen.getByRole("button", { name: "New ticket" }));
    expect(router.state.location.search).toBe("?state=open&new=ticket");
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(router.state.location.search).toBe("?state=open");
    await act(() => router.navigate("/service-desk?new=other"));
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

/**
 * `?edit=<id>` (kit requests batch 1, item 6; O4's K2): the editor's twin of
 * `useNewParam`, imported through `components.tsx`, the kit's one door.
 */
function Editing({ kind = "policy" }: { kind?: string }) {
  const edit = useEditParamFromKit(kind);
  const filters = useUrlFilters(["state"] as const);
  return (
    <>
      <button type="button" onClick={() => edit.open("pol-1")}>
        Edit Pilot
      </button>
      <button type="button" onClick={() => filters.set("state", "open")}>
        Open only
      </button>
      {edit.id !== "" ? (
        <div role="dialog" aria-label={`Edit ${edit.id}`}>
          <button type="button" onClick={edit.close}>
            Close
          </button>
        </div>
      ) : (
        <p>no editor</p>
      )}
    </>
  );
}

/** Two editors on one page, each with its own kind: only the one that opened goes Back. */
function TwoEditors() {
  const policy = useEditParamFromKit("policy");
  const schedule = useEditParamFromKit("schedule");
  return (
    <>
      <button type="button" onClick={() => policy.open("pol-1")}>
        Edit policy
      </button>
      <button type="button" onClick={schedule.close}>
        Close schedule
      </button>
    </>
  );
}

describe("useEditParam (kit requests batch 1, item 6)", () => {
  it("opens an editor from ?edit=<id>, so an edit is a link", () => {
    renderPage("/backup/policies?edit=pol-1", <Editing />);
    expect(screen.getByRole("dialog", { name: "Edit pol-1" })).toBeInTheDocument();
  });

  it("names no record while the URL names none", () => {
    renderPage("/backup/policies", <Editing />);
    expect(screen.getByText("no editor")).toBeInTheDocument();
  });

  it("puts the id in the URL when opened here, as a new entry, and takes it out when closed, keeping the rest", async () => {
    const { router } = renderPage("/backup/policies?state=open", <Editing />);
    await userEvent.click(screen.getByRole("button", { name: "Edit Pilot" }));
    expect(router.state.location.search).toBe("?state=open&edit=pol-1");
    expect(router.state.historyAction).toBe("PUSH");
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(router.state.location.search).toBe("?state=open");
  });

  it("closing an editor opened here goes back, so the next Back leaves the list", async () => {
    const router = withHistory(["/start", "/backup/policies"], "/backup/policies", <Editing />);
    await userEvent.click(screen.getByRole("button", { name: "Edit Pilot" }));
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(router.state.location.pathname + router.state.location.search).toBe("/backup/policies");
    await act(() => router.navigate(-1));
    expect(router.state.location.pathname).toBe("/start");
  });

  it("closing an editor that arrived in a link replaces, and Back still leaves the list", async () => {
    const router = withHistory(["/start", "/backup/policies?edit=pol-1"], "/backup/policies", <Editing />);
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(router.state.location.pathname + router.state.location.search).toBe("/backup/policies");
    expect(router.state.historyAction).toBe("REPLACE");
    await act(() => router.navigate(-1));
    expect(router.state.location.pathname).toBe("/start");
  });

  it("closing after the query moved on replaces rather than going back to an older state", async () => {
    const router = withHistory(["/start", "/backup/policies"], "/backup/policies", <Editing />);
    await userEvent.click(screen.getByRole("button", { name: "Edit Pilot" }));
    await userEvent.click(screen.getByRole("button", { name: "Open only" }));
    expect(router.state.location.search).toBe("?edit=pol-1&state=open");
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(router.state.location.search).toBe("?state=open");
  });

  it("writes nothing when the editor asked for is already open, so Back is not spent on a duplicate", async () => {
    const router = withHistory(["/start", "/backup/policies"], "/backup/policies", <Editing />);
    await userEvent.click(screen.getByRole("button", { name: "Edit Pilot" }));
    await userEvent.click(screen.getByRole("button", { name: "Edit Pilot" }));
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    await act(() => router.navigate(-1));
    expect(router.state.location.pathname).toBe("/start");
  });

  it("goes Back only from an entry its own kind opened: another editor's close replaces", async () => {
    const router = withHistory(["/start", "/backup/policies"], "/backup/policies", <TwoEditors />);
    await userEvent.click(screen.getByRole("button", { name: "Edit policy" }));
    expect(router.state.location.search).toBe("?edit=pol-1");
    await userEvent.click(screen.getByRole("button", { name: "Close schedule" }));
    expect(router.state.location.search).toBe("");
    expect(router.state.historyAction).toBe("REPLACE");
  });

  it("is not useNewParam's mark: a ?new= dialog's entry is not one an editor goes Back from", async () => {
    function Both() {
      const created = useNewParam("policy");
      const edit = useEditParamFromKit("policy");
      return (
        <>
          <button type="button" onClick={created.start}>New policy</button>
          <button type="button" onClick={() => edit.open("pol-1")}>Edit Pilot</button>
          <button type="button" onClick={edit.close}>Close editor</button>
        </>
      );
    }
    const router = withHistory(["/start", "/backup/policies"], "/backup/policies", <Both />);
    await userEvent.click(screen.getByRole("button", { name: "New policy" }));
    expect(router.state.location.search).toBe("?new=policy");
    // No ?edit= to close: nothing is written and nobody goes Back.
    await userEvent.click(screen.getByRole("button", { name: "Close editor" }));
    expect(router.state.location.search).toBe("?new=policy");
    expect(router.state.historyAction).toBe("PUSH");
  });
});

/**
 * FilterChips' MULTI-SELECT MODE (kit requests batch 1, item 4; O3's K1): one
 * key, several values, in the URL as a comma list.
 */
const SEVERITY_OPTIONS = [
  { value: "", label: "All severities", count: 40 },
  { value: "critical", label: "Critical", count: 3 },
  { value: "important", label: "Important", count: 12 },
  { value: "low", label: "Low" },
] as const;

function Severities() {
  const filters = useUrlFilters(["severity"] as const);
  return (
    <MultiFilterChips
      label="Severity"
      options={SEVERITY_OPTIONS}
      values={parseUrlList(filters.values.severity)}
      onChange={(values) => filters.set("severity", formatUrlList(values))}
    />
  );
}

const pressed = (name: RegExp) => screen.getByRole("button", { name }).getAttribute("aria-pressed");

/** The kit bench's source, where `a11y-browser.mjs` measures every visual piece of the kit. */
const PREVIEW = readFileSync(resolve(import.meta.dirname, "../preview/PreviewPage.tsx"), "utf8");
/** The JSX element `<Name ...>` in the bench, from its opening to the end of its props. */
function benchElement(name: string): string {
  const at = PREVIEW.search(new RegExp(`<${name}\\s`));
  return at === -1 ? "" : PREVIEW.slice(at, PREVIEW.indexOf(">", PREVIEW.indexOf("onChange", at)));
}

describe("MultiFilterChips (kit requests batch 1, item 4)", () => {
  it("reads several values from one key, so a filtered view is still a link", () => {
    renderPage("/patch?severity=critical,important", <Severities />);
    expect(screen.getByRole("group", { name: "Severity" })).toBeInTheDocument();
    expect(pressed(/Critical/)).toBe("true");
    expect(pressed(/Important/)).toBe("true");
    expect(pressed(/Low/)).toBe("false");
    expect(pressed(/All severities/)).toBe("false");
  });

  it("is every value by default: no key in the URL, and the all chip pressed", () => {
    renderPage("/patch", <Severities />);
    expect(pressed(/All severities/)).toBe("true");
    expect(pressed(/Critical/)).toBe("false");
  });

  it("adds a value per chip, written in the options' order whatever order they were pressed in", async () => {
    const { router } = renderPage("/patch", <Severities />);
    await userEvent.click(screen.getByRole("button", { name: /Important/ }));
    expect(new URLSearchParams(router.state.location.search).get("severity")).toBe("important");
    await userEvent.click(screen.getByRole("button", { name: /Critical/ }));
    // One filter, one link: Critical then Important is the same URL as Important then Critical.
    expect(new URLSearchParams(router.state.location.search).get("severity")).toBe("critical,important");
    expect(router.state.historyAction).toBe("PUSH");
  });

  it("takes a pressed value out, and the last one out is every value again, which writes nothing", async () => {
    const { router } = renderPage("/patch?severity=critical,low", <Severities />);
    await userEvent.click(screen.getByRole("button", { name: /Low/ }));
    expect(new URLSearchParams(router.state.location.search).get("severity")).toBe("critical");
    await userEvent.click(screen.getByRole("button", { name: /Critical/ }));
    expect(router.state.location.search).toBe("");
  });

  it("clears every value from the all chip", async () => {
    const { router } = renderPage("/patch?severity=critical,low&offset=50", <Severities />);
    await userEvent.click(screen.getByRole("button", { name: /All severities/ }));
    expect(router.state.location.search).toBe("");
  });

  it("keeps a value the URL holds that it does not offer, so a click never drops what it cannot show", async () => {
    const { router } = renderPage("/patch?severity=moderate", <Severities />);
    await userEvent.click(screen.getByRole("button", { name: /Critical/ }));
    expect(new URLSearchParams(router.state.location.search).get("severity")).toBe("critical,moderate");
  });

  it("draws FilterChips' own chip: a count only where the caller has one, the mark on a chosen one", () => {
    renderPage("/patch?severity=critical", <Severities />);
    expect(screen.getByRole("button", { name: /Important\s*12/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Low" })).toBeInTheDocument();
    const critical = screen.getByRole("button", { name: /Critical/ });
    expect(critical).toHaveClass("chip", "on");
    // The second carrier: the chosen chip says so by shape as well as colour.
    expect(critical.querySelector("svg")).not.toBeNull();
    expect(screen.getByRole("button", { name: /Important/ }).querySelector("svg")).toBeNull();
  });

  it("is on /__preview with a chosen value, so the browser's a11y pass measures it", () => {
    const element = benchElement("MultiFilterChips");
    expect(element).not.toBe("");
    expect(element).toMatch(/values=\{/);
  });

  it("lets Back undo a chip", async () => {
    const { router } = renderPage("/patch", <Severities />);
    await userEvent.click(screen.getByRole("button", { name: /Critical/ }));
    await userEvent.click(screen.getByRole("button", { name: /Low/ }));
    await act(() => router.navigate(-1));
    expect(new URLSearchParams(router.state.location.search).get("severity")).toBe("critical");
  });
});

describe("the comma list a multi-select filter keeps in the URL", () => {
  it("reads an absent or empty key as every value", () => {
    expect(parseUrlList("")).toEqual([]);
  });

  it("splits on commas, dropping empty pieces and repeats", () => {
    expect(parseUrlList("critical,important")).toEqual(["critical", "important"]);
    expect(parseUrlList("critical,,important,critical,")).toEqual(["critical", "important"]);
  });

  it("joins with commas, and every value is the empty string, which useUrlFilters writes as no key", () => {
    expect(formatUrlList(["critical", "important"])).toBe("critical,important");
    expect(formatUrlList([])).toBe("");
    expect(formatUrlList(["low", "low", ""])).toBe("low");
  });
});

/**
 * THE KIT'S SELECT CHIP (kit requests batch 1, item 5): `ClientFilter`'s
 * `.chip-select`, for any URL key, so no page hand-draws one.
 */
const RESOLUTIONS = [
  { value: "", label: "Any resolution" },
  { value: "fixed", label: "Fixed" },
  { value: "wontfix", label: "Won't fix" },
] as const;

function Resolutions({ ariaLabel }: { ariaLabel?: string }) {
  const filters = useUrlFilters(["resolution"] as const);
  return (
    <ChipSelect
      label="Resolution"
      {...(ariaLabel === undefined ? {} : { ariaLabel })}
      options={RESOLUTIONS}
      value={filters.values.resolution}
      onChange={(value) => filters.set("resolution", value)}
    />
  );
}

/** An order is a choice that narrows nothing, so it is never drawn as a filter in force. */
function Order() {
  const [order, setOrder] = useState("first_seen");
  return (
    <ChipSelect
      label="Order"
      ariaLabel="Order the groups by"
      selected={false}
      options={[
        { value: "last_seen", label: "Last seen" },
        { value: "first_seen", label: "First seen" },
      ]}
      value={order}
      onChange={setOrder}
    />
  );
}

describe("ChipSelect (kit requests batch 1, item 5)", () => {
  it("draws a chip that opens a choice, its caption shown and the select named by it", () => {
    renderPage("/health", <Resolutions />);
    const select = screen.getByRole("combobox", { name: "Resolution" });
    expect([...(select as HTMLSelectElement).options].map((option) => option.textContent)).toEqual([
      "Any resolution",
      "Fixed",
      "Won't fix",
    ]);
    const chip = select.closest(".chip");
    expect(chip).toHaveClass("chip-select");
    // The caption is drawn for the eye; the select carries the name, so it is not read twice.
    expect(chip?.querySelector('[aria-hidden="true"]')).toHaveTextContent("Resolution");
  });

  it("takes a fuller name for the select where the caption alone says less", () => {
    renderPage("/health", <Resolutions ariaLabel="Filter by resolution" />);
    expect(screen.getByRole("combobox", { name: "Filter by resolution" })).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Resolution" })).toBeNull();
  });

  it("is drawn selected while it holds a value, and plain by default", () => {
    renderPage("/health?resolution=fixed", <Resolutions />);
    const select = screen.getByRole("combobox", { name: "Resolution" });
    expect(select).toHaveValue("fixed");
    expect(select.closest(".chip")).toHaveClass("on");
  });

  it("is plain while it holds every value", () => {
    renderPage("/health", <Resolutions />);
    expect(screen.getByRole("combobox", { name: "Resolution" }).closest(".chip")).not.toHaveClass("on");
  });

  it("writes a choice as a new entry, so Back undoes it, and every value writes no key", async () => {
    const { router } = renderPage("/health?offset=50", <Resolutions />);
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Resolution" }), "fixed");
    expect(router.state.location.search).toBe("?resolution=fixed");
    expect(router.state.historyAction).toBe("PUSH");
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Resolution" }), "");
    expect(router.state.location.search).toBe("");
    await act(() => router.navigate(-1));
    expect(router.state.location.search).toBe("?resolution=fixed");
  });

  it("never draws an order as a filter in force", async () => {
    renderPage("/error-monitor", <Order />);
    const select = screen.getByRole("combobox", { name: "Order the groups by" });
    expect(select).toHaveValue("first_seen");
    expect(select.closest(".chip")).not.toHaveClass("on");
    await userEvent.selectOptions(select, "last_seen");
    expect(select).toHaveValue("last_seen");
    expect(select.closest(".chip")).not.toHaveClass("on");
  });

  it("is the Client chip's own markup: ClientFilter is drawn by it", () => {
    renderPage("/x", <ChipPage />);
    const client = screen.getByRole("combobox", { name: "Client" }).closest(".chip") as HTMLElement;
    const { container } = render(
      <ChipSelect
        label="Client"
        options={[{ value: "", label: "All clients" }, { value: ANDES, label: "Andes Logistics" }, { value: BOGOTA, label: "Bogotá Retail" }]}
        value=""
        onChange={vi.fn()}
      />,
    );
    expect(container.firstElementChild?.outerHTML).toBe(client.outerHTML);
  });

  it("is on /__preview, holding a value, so the browser's a11y pass measures it", () => {
    const element = benchElement("ChipSelect");
    expect(element).not.toBe("");
    expect(element).toMatch(/value=\{/);
  });
});

/**
 * A LINK UNDER THE CLIENT CHIP KEEPS IT (kit requests batch 2, K1): Health's
 * `withClient`, copied by O3 (`underChip`) and O4 (`withChip`), as one kit piece.
 */
describe("withClient (kit requests batch 2, K1)", () => {
  it("leaves the link alone when no client is read", () => {
    expect(withClient("/health?state=open", null)).toBe("/health?state=open");
    expect(withClient("/devices", null)).toBe("/devices");
  });

  it("adds the client to a link with no query", () => {
    expect(withClient("/devices", ANDES)).toBe(`/devices?client=${ANDES}`);
  });

  it("adds it after a link's own query", () => {
    expect(withClient("/health?state=cleared&resolution=resolved", ANDES)).toBe(
      `/health?state=cleared&resolution=resolved&client=${ANDES}`,
    );
  });

  it("encodes what it adds", () => {
    expect(withClient("/devices", "a b&c")).toBe("/devices?client=a%20b%26c");
  });

  it("reads an empty chip as no client, so it never writes an empty client=", () => {
    expect(withClient("/devices?state=offline", "")).toBe("/devices?state=offline");
  });

  it("opens the list on the same client: the Client chip there reads it", () => {
    renderPage(withClient("/devices?state=offline", ANDES), <ChipPage />);
    expect(screen.getByRole("combobox", { name: "Client" })).toHaveValue(ANDES);
  });
});
