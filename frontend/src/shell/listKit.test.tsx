import { useQuery } from "@tanstack/react-query";
import { act, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { MeClient } from "../lib/api";
import { renderPage } from "../test/renderPage";
import {
  BulkBar,
  ClientFilter,
  FilterChips,
  ListEmpty,
  ShowingCount,
  useApiFor,
  useNarrowing,
  useNewParam,
  useUrlFilters,
} from "./listKit";

const ANDES = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57";
const BOGOTA = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";
/** A well-formed client id that is not one of this person's (a stale or edited link). */
const FOREIGN = "9d1f0c2a-4b3e-4c5d-8e6f-7a8b9c0d1e2f";

const scope = vi.hoisted(() => ({ selected: null as string | null, clients: [] as unknown[], ready: true }));
vi.mock("../lib/clientScope", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/clientScope")>()),
  useClientScope: () => ({ selected: scope.selected, clients: scope.clients, select: vi.fn(), ready: scope.ready }),
}));
const api = vi.hoisted(() => ({ forClient: vi.fn((id: string) => ({ narrowedTo: id })) }));
vi.mock("../lib/ApiContext", () => ({ useApi: () => api }));

function client(id: string, name: string): MeClient {
  return { id, name, kind: "customer", enabled_modules: [], permissions: [] } as unknown as MeClient;
}

beforeEach(() => {
  scope.selected = null;
  scope.ready = true;
  scope.clients = [client(ANDES, "Andes Logistics"), client(BOGOTA, "Bogotá Retail")];
  api.forClient.mockClear();
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

describe("ClientFilter (design §1.3: a Client chip while every client is shown)", () => {
  it("offers every client of the person, drawn as a chip", () => {
    renderPage("/x", <ClientFilter value="" onChange={vi.fn()} />);
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

  it("drops a client the person does not have from the URL, instead of drawing All clients over an empty list", () => {
    const onChange = vi.fn();
    renderPage("/x", <ClientFilter value={FOREIGN} onChange={onChange} />);
    expect(onChange).toHaveBeenCalledWith("");
  });

  it("is not drawn when the top bar has chosen a client, because that choice already decides", () => {
    scope.selected = ANDES;
    renderPage("/x", <ClientFilter value="" onChange={vi.fn()} />);
    expect(screen.queryByRole("combobox", { name: "Client" })).toBeNull();
  });
});

function Probe({ id }: { id: string }) {
  const narrowed = useApiFor(id) as unknown as { narrowedTo?: string };
  return <p>{narrowed.narrowedTo ?? "console"}</p>;
}

describe("useApiFor (decision D6)", () => {
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
  const narrowing = useNarrowing(id);
  return <p>{narrowing.ready ? (narrowing.client ?? "console") : "waiting"}</p>;
}

/** A list behind the chip, keyed on the narrowed client the way Receta L keys one. */
function ChipList() {
  const filters = useUrlFilters(["client"] as const);
  const narrowing = useNarrowing(filters.values.client);
  const narrowed = useApiFor(filters.values.client) as unknown as { narrowedTo?: string };
  const list = useQuery({
    queryKey: ["probe", narrowing.client],
    queryFn: async () => narrowed.narrowedTo ?? "console",
    enabled: narrowing.ready,
  });
  return (
    <>
      <ClientFilter value={filters.values.client} onChange={(value) => filters.set("client", value)} />
      <p>read as {list.data ?? "nothing yet"}</p>
    </>
  );
}

describe("useNarrowing (decision D17)", () => {
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

function Creating() {
  const created = useNewParam("ticket");
  return (
    <>
      <button type="button" onClick={created.start}>New ticket</button>
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
