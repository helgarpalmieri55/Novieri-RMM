/**
 * Discovery candidates (asset-inventory spec §3).
 *
 * THE STATE THIS PAGE MUST NOT GET WRONG is the empty one. "Nothing was found"
 * and "nothing could look" render identically as a blank table and mean
 * opposite things: the first says the segment is clean, the second says the
 * platform has no idea what is on it. IPAM records `state='no_agent'` on a
 * sweep it could not run precisely so a screen can tell them apart, and the
 * candidates response carries the count — this file is what makes the page use
 * it (falsifier 8).
 *
 * The other rule is the same one the fleet list follows: never a guessed
 * manufacturer. The vendor column is what somebody triaging fifty rows uses to
 * decide what a thing IS.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient, AssetCandidate, AssetCandidatePage, Me } from "../../lib/api";
import { ApiError } from "../../lib/api";
import { CandidatesPage } from "./CandidatesPage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function candidate(overrides: Partial<AssetCandidate> = {}): AssetCandidate {
  return {
    address: "172.22.104.55",
    mac: "00:50:56:aa:bb:cc",
    vendor: "VMware",
    oui_prefix: "00:50:56",
    hostname: "",
    first_seen_at: "2026-08-10T09:00:00Z",
    last_seen_at: "2026-08-18T09:00:00Z",
    sightings: 12,
    confidence: "medium",
    subnet_id: null,
    client_id: "o1",
    ...overrides,
  };
}

function page(overrides: Partial<AssetCandidatePage> = {}): AssetCandidatePage {
  return {
    items: [candidate()],
    total: 1,
    truncated: false,
    unsweepable_subnets: 0,
    ...overrides,
  };
}

const promote = vi.fn();
const listIpamSubnets = vi.fn();
const dispatchIpamSweep = vi.fn();

// A sweepable subnet as GET /ipam/subnets returns it: IPv4, `sweep_enabled`.
function subnet(overrides: Record<string, unknown> = {}) {
  return {
    id: "sub-1",
    vrf_id: "vrf-1",
    prefix: "10.0.4.0/24",
    kind: "network",
    name: "Ops LAN",
    description: "",
    site_id: null,
    vlan_id: null,
    gateway: null,
    dns_servers: [],
    parent_id: null,
    sweep_enabled: true,
    sweep_device_id: "dev-1",
    sweep_interval_hours: 24,
    last_swept_at: null,
    ...overrides,
  };
}

function renderPage(listAssetCandidates: () => Promise<unknown>, me: Partial<Me> = {}) {
  mockUseApi.mockReturnValue({
    listAssetCandidates,
    promoteAssetCandidates: promote,
    // The suggestions panel this page renders shares the client.
    listAssetSuggestions: vi.fn().mockResolvedValue({ items: [] }),
    // The Scan-a-subnet dialog this page mounts reads the subnet list and
    // dispatches the sweep through the same client.
    listIpamSubnets,
    dispatchIpamSweep,
    // The "Recorded by a sweep" tile counts the fleet list's own filter.
    listDevices: vi.fn().mockResolvedValue({
      items: [],
      total: 0,
      state_counts: { all: 0, online: 0, offline: 0, assets: 0, certs: 0 },
      fleet: { total: 0, managed: 0, online: 0, cert_attention: 0 },
    }),
    // `org_id` is the internal client's id, which a promotion names (phase 2).
    me: vi.fn().mockResolvedValue({ org_id: "o1", permissions: [], ...me } as Me),
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui: ReactElement = (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <CandidatesPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
  // The client too, so a test can make the page read its list again.
  return { ...render(ui), queryClient };
}

beforeEach(() => {
  mockUseApi.mockReset();
  promote.mockReset().mockResolvedValue({ results: [] });
  listIpamSubnets.mockReset().mockResolvedValue([subnet()]);
  dispatchIpamSweep.mockReset().mockResolvedValue({ id: "run-1", state: "queued" });
});

describe("CandidatesPage", () => {
  it("says it is loading rather than showing an empty network", async () => {
    renderPage(() => new Promise<never>(() => {}));
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty network", async () => {
    renderPage(() => Promise.reject(new Error("boom")));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it("says nothing unknown was seen when that is genuinely the case", async () => {
    renderPage(() => Promise.resolve(page({ items: [], total: 0 })));
    expect(await screen.findByText(/nothing unaccounted/i)).toBeInTheDocument();
    expect(screen.queryByText(/no agent/i)).not.toBeInTheDocument();
  });

  it("says when nothing could have looked, instead of leaving the blank list to speak", async () => {
    // Falsifier 8. THE failure this page exists to avoid.
    renderPage(() =>
      Promise.resolve(page({ items: [], total: 0, unsweepable_subnets: 3 })),
    );
    expect(await screen.findByText(/3 subnets/i)).toBeInTheDocument();
    expect(screen.getByText(/no agent that can sweep/i)).toBeInTheDocument();
  });

  it("shows the vendor when known", async () => {
    renderPage(() => Promise.resolve(page()));
    expect(await screen.findByText("VMware")).toBeInTheDocument();
  });

  it("shows the raw prefix rather than guessing a manufacturer", async () => {
    renderPage(() =>
      Promise.resolve(
        page({
          items: [candidate({ vendor: null, oui_prefix: "A4:BB:6D" })],
        }),
      ),
    );
    expect(await screen.findByText("A4:BB:6D")).toBeInTheDocument();
    // Not an empty cell and not an invented name.
    expect(screen.queryByText(/unknown vendor/i)).not.toBeInTheDocument();
  });

  it("says what the evidence actually is", async () => {
    // `low` means ICMP: something answered, and that is all it means. An
    // operator deciding whether to promote a row needs to know that is all the
    // platform has.
    renderPage(() =>
      Promise.resolve(page({ items: [candidate({ confidence: "low", mac: null })] })),
    );
    const row = (await screen.findByText("172.22.104.55")).closest("tr");
    expect(within(row as HTMLElement).getByText(/ping only/i)).toBeInTheDocument();
  });

  it("promotes only the rows the operator selected", async () => {
    renderPage(() =>
      Promise.resolve(
        page({
          items: [candidate(), candidate({ address: "172.22.104.56" })],
          total: 2,
        }),
      ),
    );
    await screen.findByText("172.22.104.55");
    await userEvent.click(screen.getByRole("checkbox", { name: /172\.22\.104\.56/ }));
    await userEvent.click(screen.getByRole("button", { name: /promote/i }));

    expect(promote).toHaveBeenCalledTimes(1);
    expect(promote.mock.calls[0]?.[0]).toMatchObject({
      addresses: ["172.22.104.56"],
    });
  });

  it("names the row's own client, not the internal one", async () => {
    // Operator decision 2026-10-28, "use the row's client": a technician
    // narrowed to X sees X's candidates, and `/me` is the organisation's (o1).
    renderPage(() => Promise.resolve(page({ items: [candidate({ client_id: "c-x" })] })));
    await screen.findByText("172.22.104.55");
    await userEvent.click(screen.getByRole("checkbox", { name: /172\.22\.104\.55/ }));
    await userEvent.click(screen.getByRole("button", { name: /promote/i }));

    await waitFor(() => expect(promote).toHaveBeenCalledTimes(1));
    expect(promote.mock.calls[0]?.[0]).toEqual({
      client_id: "c-x",
      addresses: ["172.22.104.55"],
    });
  });

  it("sends one request per client the selected rows are of", async () => {
    renderPage(() =>
      Promise.resolve(
        page({
          items: [
            candidate({ address: "10.0.0.1", client_id: "c-x" }),
            candidate({ address: "10.0.0.2", client_id: "c-y" }),
            candidate({ address: "10.0.0.3", client_id: "c-x" }),
          ],
          total: 3,
        }),
      ),
    );
    await screen.findByText("10.0.0.1");
    for (const address of ["10.0.0.1", "10.0.0.2", "10.0.0.3"]) {
      await userEvent.click(screen.getByRole("checkbox", { name: address }));
    }
    await userEvent.click(screen.getByRole("button", { name: /promote/i }));

    await waitFor(() => expect(promote).toHaveBeenCalledTimes(2));
    const bodies = promote.mock.calls.map((call) => call[0] as { client_id: string });
    expect(bodies.sort((a, b) => a.client_id.localeCompare(b.client_id))).toEqual([
      { client_id: "c-x", addresses: ["10.0.0.1", "10.0.0.3"] },
      { client_id: "c-y", addresses: ["10.0.0.2"] },
    ]);
  });

  it("reports every client's outcome together", async () => {
    promote.mockImplementation((body: { client_id: string; addresses: string[] }) =>
      Promise.resolve({
        results: body.addresses.map((address) =>
          body.client_id === "c-x"
            ? { address, created: true, device_id: "d-1", reason: "created" }
            : { address, created: false, device_id: null, reason: "already an asset" },
        ),
      }),
    );
    renderPage(() =>
      Promise.resolve(
        page({
          items: [
            candidate({ address: "10.0.0.1", client_id: "c-x" }),
            candidate({ address: "10.0.0.2", client_id: "c-y" }),
          ],
          total: 2,
        }),
      ),
    );
    await screen.findByText("10.0.0.1");
    await userEvent.click(screen.getByRole("checkbox", { name: "10.0.0.1" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "10.0.0.2" }));
    await userEvent.click(screen.getByRole("button", { name: /promote/i }));

    const outcome = await screen.findByRole("alert");
    expect(outcome).toHaveTextContent("1 of 2 promoted. Not promoted: 10.0.0.2 (already an asset).");
  });

  it("still reports one client's results when another client's request fails", async () => {
    promote.mockImplementation((body: { client_id: string; addresses: string[] }) =>
      body.client_id === "c-x"
        ? Promise.resolve({
            results: [{ address: "10.0.0.1", created: true, device_id: "d-1", reason: "created" }],
          })
        : Promise.reject(new ApiError(404, JSON.stringify({ detail: "no such client" }))),
    );
    renderPage(() =>
      Promise.resolve(
        page({
          items: [
            candidate({ address: "10.0.0.1", client_id: "c-x" }),
            candidate({ address: "10.0.0.2", client_id: "c-y" }),
          ],
          total: 2,
        }),
      ),
    );
    await screen.findByText("10.0.0.1");
    await userEvent.click(screen.getByRole("checkbox", { name: "10.0.0.1" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "10.0.0.2" }));
    await userEvent.click(screen.getByRole("button", { name: /promote/i }));

    expect(await screen.findByText("1 candidate promoted to assets.")).toBeInTheDocument();
    expect(await screen.findByText("no such client")).toBeInTheDocument();
  });

  it("keeps the rows of a client whose request failed selected, and only those", async () => {
    // Final review E: a partial failure cleared the whole selection, so
    // retrying the client that failed meant finding and ticking its rows
    // again. The client that succeeded is done; the one that failed is not.
    promote.mockImplementation((body: { client_id: string; addresses: string[] }) =>
      body.client_id === "c-x"
        ? Promise.resolve({
            results: [{ address: "10.0.0.1", created: true, device_id: "d-1", reason: "created" }],
          })
        : Promise.reject(new ApiError(503, JSON.stringify({ detail: "try again" }))),
    );
    renderPage(() =>
      Promise.resolve(
        page({
          items: [
            candidate({ address: "10.0.0.1", client_id: "c-x" }),
            candidate({ address: "10.0.0.2", client_id: "c-y" }),
            candidate({ address: "10.0.0.3", client_id: "c-y" }),
          ],
          total: 3,
        }),
      ),
    );
    await screen.findByText("10.0.0.1");
    for (const address of ["10.0.0.1", "10.0.0.2", "10.0.0.3"]) {
      await userEvent.click(screen.getByRole("checkbox", { name: address }));
    }
    await userEvent.click(screen.getByRole("button", { name: /promote/i }));

    expect(await screen.findByText("try again")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "10.0.0.1" })).not.toBeChecked());
    expect(screen.getByRole("checkbox", { name: "10.0.0.2" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "10.0.0.3" })).toBeChecked();
  });

  it("still sends a selected row a re-read dropped, and the server answers it", async () => {
    // Task 11 re-review, M-R2: the selection is a snapshot of each ticked row.
    promote.mockResolvedValue({
      results: [
        {
          address: "10.0.0.1",
          created: false,
          device_id: null,
          reason: "not a candidate: no current observation for this address",
        },
      ],
    });
    const list = vi
      .fn()
      .mockResolvedValueOnce(page({ items: [candidate({ address: "10.0.0.1", client_id: "c-x" })] }))
      .mockResolvedValue(page({ items: [candidate({ address: "10.0.0.2", client_id: "c-x" })] }));
    const { queryClient } = renderPage(list);
    await userEvent.click(await screen.findByRole("checkbox", { name: "10.0.0.1" }));
    await queryClient.invalidateQueries({ queryKey: ["asset-candidates"] });
    await screen.findByText("10.0.0.2");
    expect(screen.queryByText("10.0.0.1")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: /promote 1/i }));
    await waitFor(() => expect(promote).toHaveBeenCalledTimes(1));
    expect(promote.mock.calls[0]?.[0]).toEqual({ client_id: "c-x", addresses: ["10.0.0.1"] });
    expect(await screen.findByText(/Not promoted: 10\.0\.0\.1 \(not a candidate/)).toBeInTheDocument();
  });

  it("names every failure when every client's request fails", async () => {
    promote.mockImplementation((body: { client_id: string }) =>
      Promise.reject(
        new ApiError(409, JSON.stringify({ detail: `client ${body.client_id} is archived` })),
      ),
    );
    renderPage(() =>
      Promise.resolve(
        page({
          items: [
            candidate({ address: "10.0.0.1", client_id: "c-x" }),
            candidate({ address: "10.0.0.2", client_id: "c-y" }),
          ],
          total: 2,
        }),
      ),
    );
    await userEvent.click(await screen.findByRole("checkbox", { name: "10.0.0.1" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "10.0.0.2" }));
    await userEvent.click(screen.getByRole("button", { name: /promote/i }));

    expect(await screen.findByText("client c-x is archived")).toBeInTheDocument();
    expect(screen.getByText("client c-y is archived")).toBeInTheDocument();
    // Nothing went through, so the selection stays for a second try.
    expect(screen.getByRole("checkbox", { name: "10.0.0.1" })).toBeChecked();
  });

  it("lists the same address of two clients as two rows, selected apart", async () => {
    renderPage(() =>
      Promise.resolve(
        page({ items: [candidate(), candidate({ client_id: "c-y" })], total: 2 }),
      ),
    );
    expect(await screen.findAllByText("172.22.104.55")).toHaveLength(2);
    // Each row says whose it is -- the client's name for someone holding
    // `clients.read`, its id otherwise -- so its checkbox is its own.
    const own = screen.getByRole("checkbox", { name: "172.22.104.55 of o1" });
    const theirs = screen.getByRole("checkbox", { name: "172.22.104.55 of c-y" });
    await userEvent.click(theirs);
    expect(theirs).toBeChecked();
    expect(own).not.toBeChecked();
    await userEvent.click(screen.getByRole("button", { name: /promote 1/i }));
    await waitFor(() => expect(promote).toHaveBeenCalledTimes(1));
    expect(promote.mock.calls[0]?.[0]).toEqual({ client_id: "c-y", addresses: ["172.22.104.55"] });
  });

  it("will not promote nothing", async () => {
    // A promote button that fires with an empty selection turns a mis-click
    // into a request whose result the operator has no way to interpret.
    renderPage(() => Promise.resolve(page()));
    await screen.findByText("172.22.104.55");
    expect(screen.getByRole("button", { name: /promote/i })).toBeDisabled();
  });

  it("says how many candidates were not shown", async () => {
    // A /16 sweep observes tens of thousands of addresses. A truncated list
    // that does not say so reads as "that is all of them".
    renderPage(() => Promise.resolve(page({ total: 4210, truncated: true })));
    // The SENTENCE, not just the number -- the stat card also shows 4210, and
    // a card alone leaves an operator to infer that the table below it is
    // partial. It has to be said in words.
    expect(await screen.findByText(/showing 1 of 4210/i)).toBeInTheDocument();
  });
});

/**
 * The promote that came back 200 and promoted nothing.
 *
 * `POST /devices/candidates/promote` answers PER ADDRESS on purpose -- the
 * route's own docstring says "one stale entry in a selection must not silently
 * discard the rest" -- and this page used to throw that answer away: it
 * cleared the selection and refetched, so a response where every row came back
 * `created: false` was indistinguishable from one where every row became an
 * asset. That is the audit's swallowed-failure class arriving through a 2xx
 * instead of a 503.
 */
describe("what a promote actually did", () => {
  it("names the addresses that did NOT become assets, and why", async () => {
    promote.mockResolvedValue({
      results: [
        { address: "172.22.104.55", created: true, device_id: "d1", reason: "created" },
        {
          address: "172.22.104.56",
          created: false,
          device_id: "d2",
          reason: "already an asset",
        },
      ],
    });
    renderPage(() =>
      Promise.resolve(
        page({ items: [candidate(), candidate({ address: "172.22.104.56" })], total: 2 }),
      ),
    );
    await screen.findByText("172.22.104.55");
    await userEvent.click(screen.getByRole("checkbox", { name: /172\.22\.104\.55/ }));
    await userEvent.click(screen.getByRole("button", { name: /promote/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("1 of 2 promoted");
    expect(alert).toHaveTextContent("172.22.104.56 (already an asset)");
  });

  it("does not report a silent all-refused promote as success", async () => {
    // THE failure this describe exists for: a 200 in which nothing was
    // created, on a page that clears the selection either way.
    promote.mockResolvedValue({
      results: [
        {
          address: "172.22.104.55",
          created: false,
          device_id: null,
          reason: "not a candidate: no current observation for this address",
        },
      ],
    });
    renderPage(() => Promise.resolve(page()));
    await screen.findByText("172.22.104.55");
    await userEvent.click(screen.getByRole("checkbox", { name: /172\.22\.104\.55/ }));
    await userEvent.click(screen.getByRole("button", { name: /promote/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("0 of 1 promoted");
    expect(alert).toHaveTextContent(/no current observation/);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("says so plainly when every selected row did become an asset", async () => {
    // NON-VACUITY: a page that always alerted would pass both tests above and
    // teach the operator to ignore the message.
    promote.mockResolvedValue({
      results: [
        { address: "172.22.104.55", created: true, device_id: "d1", reason: "created" },
      ],
    });
    renderPage(() => Promise.resolve(page()));
    await screen.findByText("172.22.104.55");
    await userEvent.click(screen.getByRole("checkbox", { name: /172\.22\.104\.55/ }));
    await userEvent.click(screen.getByRole("button", { name: /promote/i }));

    expect(await screen.findByRole("status")).toHaveTextContent("1 candidate promoted");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("announces a bodyless 503 as a sentence rather than the word Error", async () => {
    // `String(new ApiError(503, ""))` is the bare string "Error" -- which is
    // what this page rendered, unannounced, for the exact failure the audit
    // filed as a BLOCKER.
    promote.mockRejectedValue(new ApiError(503, ""));
    renderPage(() => Promise.resolve(page()));
    await screen.findByText("172.22.104.55");
    await userEvent.click(screen.getByRole("checkbox", { name: /172\.22\.104\.55/ }));
    await userEvent.click(screen.getByRole("button", { name: /promote/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/unavailable right now/i);
    expect(alert).toHaveTextContent(/Nothing was changed/i);
    expect(alert.textContent).not.toBe("Error");
  });

  it("clears the last outcome when the operator starts a new selection", async () => {
    // An outcome describing ONE promote of ONE selection, left on screen while
    // the next selection is built, reads as a report on rows it never saw.
    promote.mockResolvedValue({
      results: [
        { address: "172.22.104.55", created: true, device_id: "d1", reason: "created" },
      ],
    });
    renderPage(() =>
      Promise.resolve(
        page({ items: [candidate(), candidate({ address: "172.22.104.56" })], total: 2 }),
      ),
    );
    await screen.findByText("172.22.104.55");
    await userEvent.click(screen.getByRole("checkbox", { name: /172\.22\.104\.55/ }));
    await userEvent.click(screen.getByRole("button", { name: /promote/i }));
    await screen.findByRole("status");

    await userEvent.click(screen.getByRole("checkbox", { name: /172\.22\.104\.56/ }));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});

describe("candidate timestamps", () => {
  it("renders last seen in the profile's zone, not the browser's", async () => {
    // 09:00Z in August is 05:00 in New York (EDT). Under the runner's TZ=UTC a
    // page reading the browser zone renders 09:00 -- the shape of the audit's
    // "displayed 5 hours earlier than it was entered" BLOCKER.
    renderPage(() => Promise.resolve(page()), { timezone: "America/New_York" });
    expect(await screen.findByText("18 Aug 2026, 05:00")).toBeInTheDocument();
    expect(screen.queryByText("18 Aug 2026, 09:00")).not.toBeInTheDocument();
  });
});

/**
 * NO DEAD TILES.
 *
 * The operator's instruction was "all tiles and all numbers or kpis need to be
 * clickable and direct the user to the specific menus, i dont want dead tiles
 * everywhere", and `StatCard` now enforces it in the type system. This is the
 * page-level half of that: the type can force a `to` to exist, but only a test
 * can say it points at the right thing, and only a test stops the next hurried
 * change from pointing it back at this page.
 */
describe("the tiles are links", () => {
  it("sends the unsweepable count to the subnets it is about", async () => {
    renderPage(() => Promise.resolve(page({ unsweepable_subnets: 3 })));
    await screen.findByText(/3 subnets/i);
    const tiles = [...document.querySelectorAll(".stat-card")].map((card) => [
      card.querySelector(".stat-label")?.textContent,
      card.tagName,
      card.getAttribute("href"),
    ]);
    // The rows a sweep wrote on its own evidence live on the fleet list under
    // its provenance filter. A subnet is counted in the second tile because
    // its last sweep run came back `no_agent`; the subnets and their sweep
    // history live in IPAM, not on this page.
    expect(tiles).toEqual([
      ["Recorded by a sweep", "A", "/devices?provenance=sweep"],
      ["Subnets nobody can sweep", "A", "/ipam"],
    ]);
  });

  it("draws the pair at the console's tile width, not stretched across the page", async () => {
    // `columns` is the TRACK count, not the child count. At 2 these two
    // ordinary counters were half the content width each, twice every other
    // tile in the console, and the shorter figure left a band of empty tile
    // beneath it. Four tracks is what the design's own Discovered screen
    // draws; `components.css` still folds it to three, two and one.
    renderPage(() => Promise.resolve(page()));
    await screen.findByText("Recorded by a sweep");
    expect(document.querySelector(".stats-grid")?.getAttribute("data-columns")).toBe("4");
    expect(document.querySelectorAll(".stat-card")).toHaveLength(2);
  });

  it("keeps the two counts whose tiles were deleted for having nowhere to go", async () => {
    // "Candidates" counted the table directly beneath it and "Selected"
    // counted a checkbox set held in this component's state. Neither had a
    // list to open, so neither is a tile any more -- but both numbers are
    // still on screen, which is what makes the deletion a move rather than a
    // loss.
    const user = userEvent.setup();
    renderPage(() => Promise.resolve(page()));
    expect(await screen.findByText(/1 candidate/i)).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: /172\.22\.104\.55/ }));
    expect(screen.getByRole("button", { name: /promote 1/i })).toBeInTheDocument();
  });

  it("gives the unsweepable tile the sub-line its neighbour has", async () => {
    // Only the zero case said anything under the figure, so a real count drew
    // an empty band beside a tile that carries a line. The sentence says what
    // puts a subnet in the count, and it is not doubled with `zeroLabel`.
    renderPage(() => Promise.resolve(page({ unsweepable_subnets: 3 })));
    // Wait for the count itself: before it lands the tile is still drawing the
    // zero, and asserting mid-flight would assert on the loading state.
    await screen.findByText(/3 subnets/i);
    const card = (await screen.findByText("Subnets nobody can sweep")).closest(
      ".stat-card",
    ) as HTMLElement;
    expect(
      within(card).getByText("Nothing there could run a sweep"),
    ).toBeInTheDocument();
    expect(
      within(card).queryByText("No sweep has reported a missing agent"),
    ).not.toBeInTheDocument();
  });

  it("says which zero it is when nothing is unsweepable", async () => {
    // "every subnet is covered" and "no sweep has ever run" are the same 0.
    renderPage(() => Promise.resolve(page({ unsweepable_subnets: 0 })));
    const card = (await screen.findByText("Subnets nobody can sweep")).closest(
      ".stat-card",
    ) as HTMLElement;
    expect(within(card).getByText("No sweep has reported a missing agent")).toBeInTheDocument();
    // And only that line: the foot below is about a count that exists.
    expect(
      within(card).queryByText("Nothing there could run a sweep"),
    ).not.toBeInTheDocument();
    // A measured zero is never loud, whatever the tile asked for.
    expect(card.className).not.toContain("urgent");
  });
});

/**
 * WHAT THIS PAGE IS FOR AFTER 2026-08-26.
 *
 * A sweep now records every host it finds, so an empty candidate table is the
 * normal state and this screen's remaining job is to say what nothing else
 * says: which subnets nobody could sweep, what is still unaccounted for, and
 * where the rows the sweep DID write can be reviewed.
 */
describe("once a sweep records what it finds", () => {
  it("does not tell the operator that nothing becomes an asset until they promote", async () => {
    // The page said exactly that until the operator ruled otherwise. Left in
    // place it would be the console contradicting the platform, on the screen
    // an operator goes to when they distrust the inventory.
    renderPage(() => Promise.resolve(page({ items: [], total: 0 })));
    await screen.findByText(/nothing unaccounted/i);
    expect(document.body.textContent).not.toMatch(/until you promote/i);
  });

  it("points at the rows the sweep wrote rather than copying them here", async () => {
    // Two lists of one thing disagree the first time either changes; the
    // fleet list already owns the fleet, and `provenance` is the filter.
    renderPage(() => Promise.resolve(page()));
    const link = await screen.findByRole("link", { name: /review them there/i });
    expect(link).toHaveAttribute("href", "/devices?provenance=sweep");
  });

  it("still offers promote, because that is how a person vouches for a row", async () => {
    // Not a remnant behind a flag: promoting is the only path that records
    // that a human stood behind a row, and the only one that takes the asset
    // type, lifecycle, owner and location a sweep cannot know.
    renderPage(() => Promise.resolve(page()));
    await screen.findByText("172.22.104.55");
    await userEvent.click(screen.getByLabelText("172.22.104.55"));
    expect(screen.getByRole("button", { name: /promote 1/i })).toBeEnabled();
  });

  it("scans a subnet from the discovered page, reusing the sweep dispatch", async () => {
    const user = userEvent.setup();
    renderPage(() => Promise.resolve(page({ items: [], total: 0 })));
    await screen.findByText(/nothing unaccounted/i);

    await user.click(screen.getByRole("button", { name: /scan a subnet/i }));
    const dialog = await screen.findByRole("dialog", { name: /scan a subnet/i });

    // The picker is fed by the subnet list, not a hand-typed id.
    await user.selectOptions(
      within(dialog).getByRole("combobox"),
      "sub-1",
    );
    await user.click(within(dialog).getByRole("button", { name: /scan now/i }));

    // It dispatched the SAME sweep the IPAM panel uses, by subnet id -- not a
    // second signing path.
    expect(dispatchIpamSweep).toHaveBeenCalledWith("sub-1");
    // And it confirms "asked", pointing at where the results land, rather than
    // claiming anything was found.
    expect(await within(dialog).findByText(/queued for the/i)).toBeInTheDocument();
  });

  it("surfaces a sweep refusal as a message, not a bare failure", async () => {
    const user = userEvent.setup();
    dispatchIpamSweep.mockRejectedValueOnce(
      new ApiError(403, "not authorized"),
    );
    renderPage(() => Promise.resolve(page({ items: [], total: 0 })));
    await screen.findByText(/nothing unaccounted/i);

    await user.click(screen.getByRole("button", { name: /scan a subnet/i }));
    const dialog = await screen.findByRole("dialog", { name: /scan a subnet/i });
    await user.selectOptions(within(dialog).getByRole("combobox"), "sub-1");
    await user.click(within(dialog).getByRole("button", { name: /scan now/i }));

    expect(await within(dialog).findByRole("alert")).toBeInTheDocument();
  });

});
