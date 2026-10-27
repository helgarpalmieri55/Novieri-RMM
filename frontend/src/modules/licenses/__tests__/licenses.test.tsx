import { readFileSync } from "node:fs";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../../lib/api";
import type { ApiClient, ClientOut, Me } from "../../../lib/api";
import { complianceVariant, licenseTypeVariant } from "../licenseState";
import {
  PRODUCT_PICKER_EMPTY_HINT,
  VENDOR_PICKER_EMPTY_HINT,
  productPickerOptions,
  vendorPickerOptions,
} from "../pickerOptions";
import { ComplianceDashboard } from "../ComplianceDashboard";
import { LicenseDetail } from "../LicenseDetail";
import { LicensesPage } from "../LicensesPage";
import { ProductsPage } from "../ProductsPage";
import { RenewalCalendar } from "../RenewalCalendar";
import { ReviewQueue } from "../ReviewQueue";
import { VendorsPage } from "../VendorsPage";
import type {
  AllocationOut,
  ComplianceRow,
  LicenseOut,
  ProductOut,
  RenewalRow,
  ReviewQueueRow,
  VendorOut,
} from "../types";

// The auth-backed API client is mocked so this is a pure component test: no
// real fetch — mirrors modules/bcp/__tests__/bcp.test.tsx.
const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function renderWithProviders(ui: ReactElement, initialEntries: string[] = ["/"]) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={initialEntries}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

// THE STATIC ROUTES App.tsx ACTUALLY SERVES, read out of the route table
// rather than copied into this file. A copied list keeps passing after
// somebody renames a route -- which is the moment a tile goes dead.
//
// DYNAMIC ROUTES ARE EXCLUDED, AND THAT EXCLUSION IS WHAT GIVES THIS TEETH.
// `/licenses/:licenseId` matches "/licenses/breaches", so a typo'd tile would
// still "resolve" -- onto a detail page for an id that does not exist -- and a
// looser assertion here passed against exactly that, which is why it is
// written this way. `*` is excluded for the same reason twice over: it matches
// every string and it redirects to /devices. Every tile in this cluster points
// at a list or a report, so an exact static route is both the truth and
// falsifiable; a tile that one day needs a detail route will fail here, and
// its author can say so in the assertion rather than inheriting a check that
// never objected to anything.
function staticRoutePaths(): string[] {
  // `import.meta.url` under vitest is the module's dev-server URL, not a
  // file: one, so the path is taken from its pathname -- which is this file's
  // absolute path on disk -- and walked back to the src root.
  const here = new URL(import.meta.url).pathname;
  const source = readFileSync(here.replace(/\/src\/.*$/, "/src/App.tsx"), "utf8");
  return [...source.matchAll(/<Route\s[^>]*?path="([^"]+)"/g)]
    .flatMap((match) => (match[1] ? [match[1]] : []))
    .filter((path) => !path.includes("*") && !path.includes(":"));
}

function isServedRoute(href: string | null, routes: string[]): boolean {
  return href !== null && routes.includes(href);
}

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  permissions: ["licenses.read", "licenses.manage", "licenses.keys.read"],
  // THE ZONE IS PINNED IN THE FIXTURE ON PURPOSE (cycle 5). Asserting a
  // rendered time is only meaningful if the expected string does not depend on
  // the machine the suite runs on -- and asserting against the *profile* zone
  // rather than the browser's is the whole point of the change: a technician
  // who sets a timezone must see it, from any machine they sign in from.
  timezone: "Asia/Tokyo",
};

/** A client as `GET /clients` returns it, for the per-client report columns. */
function clientOut(id: string, name: string): ClientOut {
  return {
    id, name, kind: id === "o1" ? "internal" : "customer", status: "active", email_domains: [],
    phone: "", address: "", website: "", notes: "", enabled_modules: [], device_count: 0,
    site_count: 0,
  };
}

/** The operator, able to read clients too: report rows then name theirs. */
const ME_READS_CLIENTS: Me = { ...ME, permissions: [...ME.permissions, "clients.read"] };

// A bodyless 503 is what a proxy returns, and `ApiError` is built as
// `new ApiError(status, await response.text())` -- so `message` is "". That
// empty string is why `{error ? <span>{error.message}</span> : null}` rendered
// NOTHING and `String(error)` rendered "ApiError:" with nothing after it. It
// is the exact shape of Auditor A's BLOCKER, so it is the shape every error
// test below uses.
function bodylessUnavailable(): ApiError {
  return new ApiError(503, "");
}

/**
 * Choose an option from an `EntityPicker`, the way a person does: focus the
 * combobox, then click the row.
 *
 * Deliberately NOT `user.type(...)` followed by a submit -- the picker's box
 * FILTERS, it does not accept. Typing a product name and pressing Save must
 * leave the field unchosen, which is its own test below.
 */
async function pick(
  user: ReturnType<typeof userEvent.setup>,
  pickerLabel: string,
  optionName: RegExp,
): Promise<void> {
  await user.click(screen.getByRole("combobox", { name: pickerLabel }));
  // Scoped to THIS picker's listbox: a `<select>` elsewhere on the page also
  // reports role `option`, and a helper that matched the first one anywhere
  // would pass while clicking the wrong control.
  const list = await screen.findByRole("listbox", { name: pickerLabel });
  await user.click(await within(list).findByRole("option", { name: optionName }));
}

/**
 * Press the confirm button in an open `ConfirmDialog`.
 *
 * Found inside a `role="dialog"` rather than anywhere on the page, so a test
 * that passes because the confirmation was never rendered is impossible: the
 * whole point of cycle 8 is that the click no longer goes straight through.
 */
async function confirmWith(
  user: ReturnType<typeof userEvent.setup>,
  confirmLabel: string,
): Promise<void> {
  const dialogs = await screen.findAllByRole("dialog");
  const button = dialogs
    .map((dialog) => within(dialog).queryByRole("button", { name: confirmLabel }))
    .find((found): found is HTMLElement => found != null);
  if (button == null) throw new Error(`No open dialog offers "${confirmLabel}"`);
  await user.click(button);
}

const VENDORS: VendorOut[] = [
  { id: "v1", name: "Acme Software", website: "https://acme.example", contacts: [], created_at: "2026-01-01T00:00:00.000Z" },
];

const PRODUCTS: ProductOut[] = [
  {
    id: "p1",
    name: "Acme Office Suite",
    publisher: "Acme Software",
    match_rules: [{ type: "exact", pattern: "Acme Office Suite", publisher: "Acme Software" }],
    created_at: "2026-01-01T00:00:00.000Z",
  },
];

const LICENSES: LicenseOut[] = [
  {
    id: "l1",
    client_id: "o1",
    product_id: "p1",
    vendor_id: "v1",
    type: "perpetual",
    entitlements: 50,
    has_key: true,
    cost: "1200.00",
    currency: "USD",
    po_ref: "PO-1",
    purchased_at: "2025-01-01",
    maintenance_expires_at: null,
    renews_at: null,
    agreement_ref: null,
    notes: "",
    created_at: "2026-01-01T00:00:00.000Z",
  },
];

describe("VendorsPage", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listVendors: vi.fn().mockResolvedValue(VENDORS),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("lists vendors from the mocked api", async () => {
    renderWithProviders(<VendorsPage />);
    expect(await screen.findByText("Acme Software")).toBeInTheDocument();
  });

  it("hides the New vendor control when the caller lacks licenses.manage", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME,
      permissions: ME.permissions.filter((p) => p !== "licenses.manage"),
    });
    renderWithProviders(<VendorsPage />);

    await screen.findByText("Acme Software");
    expect(screen.queryByRole("button", { name: "New vendor" })).not.toBeInTheDocument();
  });

  it("shows the New vendor control when the caller has licenses.manage", async () => {
    renderWithProviders(<VendorsPage />);

    await screen.findByText("Acme Software");
    expect(screen.getByRole("button", { name: "New vendor" })).toBeInTheDocument();
  });

  // Cycle 5. `created_at` is a `datetime` -- a real instant -- so it renders
  // in the zone the operator's PROFILE names, in the product's one format.
  // `new Date(x).toLocaleString()` used the browser's zone and the browser's
  // format, which is how one record read two ways on two machines.
  it("renders Created in the profile timezone, not the browser's", async () => {
    api.listVendors = vi
      .fn()
      .mockResolvedValue([{ ...VENDORS[0], created_at: "2026-01-01T23:00:00.000Z" }]);
    renderWithProviders(<VendorsPage />);

    // 23:00Z is 08:00 the NEXT DAY in Asia/Tokyo. A browser-zone rendering
    // cannot produce this string unless the runner happens to be in Tokyo.
    expect(await screen.findByText("02 Jan 2026, 08:00")).toBeInTheDocument();
  });

  // Cycle 4. `String(create.error)` rendered "ApiError:" and nothing after it
  // for a bodyless 503, in no live region at all.
  it("announces a bodyless 503 from Save as a sentence, not as \"ApiError:\"", async () => {
    const user = userEvent.setup();
    api.createVendor = vi.fn().mockRejectedValue(bodylessUnavailable());
    renderWithProviders(<VendorsPage />);

    await screen.findByText("Acme Software");
    await user.click(screen.getByRole("button", { name: "New vendor" }));
    await user.type(screen.getByLabelText("Name"), "Globex");
    await user.click(screen.getByRole("button", { name: "Save" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/service is unavailable right now/i);
    expect(alert).toHaveTextContent(/Nothing was changed/i);
    expect(alert.textContent).not.toContain("ApiError");
  });

  // The client-side half of the same finding: an empty Name used to land on a
  // disabled control -- nothing moved and no field was named.
  it("names the missing field on an empty submit instead of disabling Save", async () => {
    const user = userEvent.setup();
    api.createVendor = vi.fn();
    renderWithProviders(<VendorsPage />);

    await screen.findByText("Acme Software");
    await user.click(screen.getByRole("button", { name: "New vendor" }));

    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeEnabled();
    await user.click(save);

    expect(await screen.findByRole("alert")).toHaveTextContent("Name is required");
    expect(api.createVendor).not.toHaveBeenCalled();
  });
});

describe("ProductsPage", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listProducts: vi.fn().mockResolvedValue(PRODUCTS),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("lists products from the mocked api", async () => {
    renderWithProviders(<ProductsPage />);
    expect(await screen.findByText("Acme Office Suite", { selector: ".cell-title" })).toBeInTheDocument();
  });

  // The compliance report is one row per (client, product) since MSP clients
  // phase 2, and "Detected" is what inventory found across them: the sum,
  // not whichever client's row came last.
  it("detects a product's installs across every client's position", async () => {
    const position: ComplianceRow = {
      client_id: "o1",
      product_id: "p1",
      name: "Acme Office Suite",
      entitled: 5,
      installed: 3,
      delta: 2,
      breach: false,
    };
    api.licenseCompliance = vi.fn().mockResolvedValue([
      position,
      { ...position, client_id: "c2", installed: 4, delta: 1 },
    ]);
    renderWithProviders(<ProductsPage />);

    const row = (await screen.findByText("Acme Office Suite", { selector: ".cell-title" })).closest(
      "tr",
    ) as HTMLElement;
    expect(await within(row).findByText("7 installs")).toBeInTheDocument();
  });

  it("hides the New product control when the caller lacks licenses.manage", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME,
      permissions: ME.permissions.filter((p) => p !== "licenses.manage"),
    });
    renderWithProviders(<ProductsPage />);

    await screen.findByText("Acme Office Suite", { selector: ".cell-title" });
    expect(screen.queryByRole("button", { name: "New product" })).not.toBeInTheDocument();
  });

  it("shows the New product control when the caller has licenses.manage", async () => {
    renderWithProviders(<ProductsPage />);

    await screen.findByText("Acme Office Suite", { selector: ".cell-title" });
    expect(screen.getByRole("button", { name: "New product" })).toBeInTheDocument();
  });

  // Cycle 4, the other half of the BLOCKER: this backend registers no custom
  // 422 handler, so a validation failure arrives as raw Pydantic JSON.
  // `String(mutation.error)` printed that payload into the dialog.
  it("translates a 422 body into a sentence instead of printing the payload", async () => {
    const user = userEvent.setup();
    api.createProduct = vi.fn().mockRejectedValue(
      new ApiError(
        422,
        JSON.stringify({
          detail: [
            { type: "string_too_short", loc: ["body", "name"], msg: "String should have at least 2 characters", input: "x" },
          ],
        }),
      ),
    );
    renderWithProviders(<ProductsPage />);

    await screen.findByText("Acme Office Suite", { selector: ".cell-title" });
    await user.click(screen.getByRole("button", { name: "New product" }));
    await user.type(screen.getByLabelText("Name"), "x");
    await user.click(screen.getByRole("button", { name: "Save" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Name: String should have at least 2 characters");
    // The machine-readable parts stay off the screen -- `input` especially,
    // which echoes what the operator submitted.
    expect(alert.textContent).not.toContain("string_too_short");
    expect(alert.textContent).not.toContain("loc");
  });

  it("names the missing field on an empty submit instead of disabling Save", async () => {
    const user = userEvent.setup();
    api.createProduct = vi.fn();
    renderWithProviders(<ProductsPage />);

    await screen.findByText("Acme Office Suite", { selector: ".cell-title" });
    await user.click(screen.getByRole("button", { name: "New product" }));

    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeEnabled();
    await user.click(save);

    expect(await screen.findByRole("alert")).toHaveTextContent("Name is required");
    expect(api.createProduct).not.toHaveBeenCalled();
  });
});

describe("LicensesPage", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listLicenses: vi.fn().mockResolvedValue(LICENSES),
      listProducts: vi.fn().mockResolvedValue(PRODUCTS),
      listVendors: vi.fn().mockResolvedValue(VENDORS),
      licenseCompliance: vi.fn().mockResolvedValue([]),
      licenseRenewals: vi.fn().mockResolvedValue([]),
      createLicense: vi.fn().mockResolvedValue(LICENSES[0]),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("lists licenses from the mocked api", async () => {
    renderWithProviders(<LicensesPage />);

    expect(await screen.findByText("Acme Office Suite")).toBeInTheDocument();
    // The type pill is humanised by the kit: sentence case, no snake_case.
    expect(screen.getByText("Perpetual")).toBeInTheDocument();
    expect(screen.getByText("50")).toBeInTheDocument();
  });

  it("says it is loading rather than showing an empty table", async () => {
    api.listLicenses = () => new Promise<never>(() => {});
    renderWithProviders(<LicensesPage />);
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty table", async () => {
    api.listLicenses = vi.fn().mockRejectedValue(new Error("boom"));
    renderWithProviders(<LicensesPage />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/No licenses yet/i)).not.toBeInTheDocument();
  });

  it("says there are no licenses when the list is genuinely empty", async () => {
    api.listLicenses = vi.fn().mockResolvedValue([]);
    renderWithProviders(<LicensesPage />);
    expect(await screen.findByText("No licenses yet.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("counts stats from the data rather than a constant", async () => {
    renderWithProviders(<LicensesPage />);
    await screen.findByText("Acme Office Suite");
    const stats = document.querySelectorAll(".stat-card");
    const values = [...stats].map((card) => card.querySelector(".stat-value")?.textContent);
    // v3's tile order: 0 compliance breaches, 0 renewals, 1 product, 0
    // compliant products in the mocked data -- never the prototype's mock
    // figures ($184k / 31 / 42 / 5 / 1).
    expect(values).toEqual(["0", "0", "1", "0"]);
    expect(values).not.toContain("31");
  });

  // Cycle 5. `renews_at` is a `date` in schema.py -- a calendar day with no
  // time and no zone -- so it is pinned to UTC. The operator's profile zone is
  // Asia/Tokyo here; a zone-aware rendering of "2026-08-01" would land on
  // 01 Aug there but on 31 Jul for anyone west of UTC, an off-by-one-day that
  // reads as real data. One format, one day, everywhere.
  it("renders the renewal date as the day recorded, not a zone-shifted one", async () => {
    api.listLicenses = vi.fn().mockResolvedValue([{ ...LICENSES[0], renews_at: "2026-08-01" }]);
    renderWithProviders(<LicensesPage />);

    expect(await screen.findByText("01 Aug 2026")).toBeInTheDocument();
  });

  // An absent renewal must be the em dash, never "Invalid Date" -- which is
  // what `new Date(null).toLocaleDateString()` produces.
  it("renders an absent renewal date as an em dash, never Invalid Date", async () => {
    renderWithProviders(<LicensesPage />);

    await screen.findByText("Acme Office Suite");
    const row = screen.getByText("Acme Office Suite").closest("tr") as HTMLElement;
    expect(within(row).getByText("—")).toBeInTheDocument();
    expect(row.textContent).not.toContain("Invalid Date");
  });

  // Cycle 4 / the swallowed-failure class. `compliance.data ?? []` made a
  // refused /licenses/compliance render "Compliance breaches: 0" -- a clean
  // compliance figure that no data supports.
  it("does not report a failed compliance query as zero breaches", async () => {
    api.licenseCompliance = vi.fn().mockRejectedValue(bodylessUnavailable());
    renderWithProviders(<LicensesPage />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/service is unavailable right now/i);

    const stats = document.querySelectorAll(".stat-card");
    const values = [...stats].map((card) => card.querySelector(".stat-value")?.textContent);
    // The breach card and the compliant card are "n/a", not "0" --
    // both read the compliance query; the two cards whose own queries
    // succeeded still report their real counts.
    expect(values).toEqual(["n/a", "0", "1", "n/a"]);
  });

  // "all tiles and all numbers or kpis need to be clickable and direct the
  // user to the specific menus, i dont want dead tiles everywhere" -- the
  // operator, verbatim. This is the assertion that keeps that true: three
  // tiles, three anchors, three hrefs the router actually serves.
  it("gives every stat tile a link to the page its number lives on", async () => {
    renderWithProviders(<LicensesPage />);
    await screen.findByText("Acme Office Suite");

    const tiles = [...document.querySelectorAll<HTMLElement>(".stat-card")];
    expect(tiles).toHaveLength(4);
    expect(tiles.map((tile) => tile.tagName)).toEqual(["A", "A", "A", "A"]);
    expect(tiles.map((tile) => tile.getAttribute("href"))).toEqual([
      "/licenses/compliance",
      "/licenses/renewals",
      "/licenses/products",
      "/licenses/compliance",
    ]);

    const routes = staticRoutePaths();
    // Guard the guard: an empty route list would pass every href vacuously.
    expect(routes.length).toBeGreaterThan(10);
    for (const tile of tiles) {
      expect(isServedRoute(tile.getAttribute("href"), routes)).toBe(true);
    }
  });

  // WEIGHT IS URGENCY, COLOUR IS CATEGORY. An over-deployed license is the one
  // number on this row that needs a person today, so it is the only tile that
  // may take the saturated fill -- and only while there is a breach to act on.
  it("weights the breach tile only when a breach was actually measured", async () => {
    const breach: ComplianceRow = {
      client_id: "o1",
      product_id: "p1",
      name: "Acme Office Suite",
      entitled: 50,
      installed: 55,
      delta: 5,
      breach: true,
    };
    api.licenseCompliance = vi.fn().mockResolvedValue([breach]);
    renderWithProviders(<LicensesPage />);
    // The product name is on the page twice once the breach lands: the
    // license row, and the breach tile's foot line naming what is over.
    await screen.findAllByText("Acme Office Suite");

    const tiles = [...document.querySelectorAll<HTMLElement>(".stat-card")];
    expect(tiles.map((tile) => tile.classList.contains("urgent"))).toEqual([true, false, false, false]);
    expect(tiles.map((tile) => tile.classList.contains("tone-red"))).toEqual([true, false, false, false]);
    expect(tiles[0]?.querySelector(".stat-foot")).toHaveTextContent("Acme Office Suite");
  });

  // Compliance is one row per (client, product) since MSP clients phase 2.
  // The tile says PRODUCTS ("Productos excedidos"): two clients over on one
  // product are one product over, and the foot line names it once.
  it("counts an over-deployed product once however many clients are over", async () => {
    const breach: ComplianceRow = {
      client_id: "o1",
      product_id: "p1",
      name: "Acme Office Suite",
      entitled: 50,
      installed: 55,
      delta: 5,
      breach: true,
    };
    api.licenseCompliance = vi.fn().mockResolvedValue([breach, { ...breach, client_id: "c2" }]);
    renderWithProviders(<LicensesPage />);
    await screen.findAllByText("Acme Office Suite");

    const tiles = [...document.querySelectorAll<HTMLElement>(".stat-card")];
    expect(tiles[0]?.querySelector(".stat-value")).toHaveTextContent(/^1$/);
    expect(tiles[0]?.querySelector(".stat-foot")?.textContent).toBe("Acme Office Suite");
  });

  // ZERO IS A MEASUREMENT, NOT AN ABSENCE. "0 breaches" and "the compliance
  // query never answered" are the same shape on a dashboard unless the tile
  // says which it is -- the audit found nine tiles reading 0 on a page where
  // nothing had been measured at all.
  it("says which zero a zero is, and shouts about none of them", async () => {
    renderWithProviders(<LicensesPage />);
    await screen.findByText("Acme Office Suite");

    expect(screen.getByText("nothing over-deployed")).toBeInTheDocument();
    expect(screen.getByText("nothing renews in this window")).toBeInTheDocument();
    const tiles = [...document.querySelectorAll<HTMLElement>(".stat-card")];
    expect(tiles.some((tile) => tile.classList.contains("urgent"))).toBe(false);
  });

  // The compliance report is one row per (client, product) since MSP clients
  // phase 2. The Compliant tile and its foot say PRODUCTS ("of 2 products
  // reconciled"), so both count distinct products, and a product is compliant
  // only when every client's position on it is.
  it("counts compliant products, not per-client positions", async () => {
    const position: ComplianceRow = {
      client_id: "o1",
      product_id: "p1",
      name: "Acme Office Suite",
      entitled: 5,
      installed: 3,
      delta: 2,
      breach: false,
    };
    api.licenseCompliance = vi.fn().mockResolvedValue([
      position,
      { ...position, client_id: "c2" },
      { ...position, product_id: "p2", name: "Visio" },
      { ...position, product_id: "p2", name: "Visio", client_id: "c2", installed: 9, delta: -4, breach: true },
    ]);
    renderWithProviders(<LicensesPage />);
    await screen.findByText("of 2 products reconciled");

    const tiles = [...document.querySelectorAll<HTMLElement>(".stat-card")];
    expect(tiles[3]?.querySelector(".stat-value")).toHaveTextContent(/^1$/);
    expect(tiles[3]?.querySelector(".stat-foot")?.textContent).toBe("of 2 products reconciled");
  });

  // The other half of the same rule: the failed query renders the em dash, so
  // it must NOT pick up the zero label -- "nothing over-deployed" over a
  // compliance check that never ran is the reassuring lie this whole cluster
  // of tests exists to stop.
  it("gives a failed compliance query no zero label", async () => {
    api.licenseCompliance = vi.fn().mockRejectedValue(bodylessUnavailable());
    renderWithProviders(<LicensesPage />);

    await screen.findByRole("alert");
    expect(screen.queryByText("nothing over-deployed")).not.toBeInTheDocument();
  });

  it("hides the New license control when the caller lacks licenses.manage", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME,
      permissions: ME.permissions.filter((p) => p !== "licenses.manage"),
    });
    renderWithProviders(<LicensesPage />);

    await screen.findByText("Acme Office Suite");
    expect(screen.queryByRole("button", { name: "New license" })).not.toBeInTheDocument();
  });

  it("shows the New license control when the caller has licenses.manage", async () => {
    renderWithProviders(<LicensesPage />);

    await screen.findByText("Acme Office Suite");
    expect(screen.getByRole("button", { name: "New license" })).toBeInTheDocument();
  });

  // The form-kernel findings on this route. Each of these fails against the
  // pre-kernel dialog, which rendered `<Button disabled={create.isPending ||
  // !productId}>` with no <form>, no rules and no live region.
  async function openNewLicense() {
    const user = userEvent.setup();
    renderWithProviders(<LicensesPage />);
    await screen.findByText("Acme Office Suite");
    await user.click(screen.getByRole("button", { name: "New license" }));
    return user;
  }

  // Audit: "Empty submit in the license modal is a silent no-op". The click
  // used to land on a disabled control -- nothing moved and no field was
  // named. The control must now be live, and the click must produce the
  // reason.
  it("names the missing field on an empty submit instead of disabling Save", async () => {
    const user = await openNewLicense();

    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeEnabled();
    await user.click(save);

    expect(await screen.findByRole("alert")).toHaveTextContent("Product is required");
    expect(api.createLicense).not.toHaveBeenCalled();
  });

  // `Number("")` is NaN and NaN serialises to null, so a cleared Entitlements
  // reached the API as an absent seat count rather than as an error.
  it("refuses a cleared Entitlements rather than sending NaN", async () => {
    const user = await openNewLicense();

    await pick(user, "Product", /Acme Office Suite/);
    await user.clear(screen.getByLabelText("Entitlements"));
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Entitlements is required");
    expect(api.createLicense).not.toHaveBeenCalled();
  });

  // Audit: "Money and currency are unvalidated free-text fields". Cost is a
  // Decimal server-side carried as a string, so a thousands separator is
  // accepted by the input and rejected by pydantic -- 422, rendered raw.
  it("refuses a cost that is not a plain decimal amount", async () => {
    const user = await openNewLicense();

    await pick(user, "Product", /Acme Office Suite/);
    await user.type(screen.getByLabelText("Cost (optional)"), "1,200");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/Cost must be an amount/);
    expect(api.createLicense).not.toHaveBeenCalled();
  });

  // An amount whose currency is unknown cannot be reconciled or summed --
  // the same rule that makes /licenses/costs report per currency.
  it("refuses a cost with no currency beside it", async () => {
    const user = await openNewLicense();

    await pick(user, "Product", /Acme Office Suite/);
    await user.type(screen.getByLabelText("Cost (optional)"), "1200.00");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Currency is required");
    expect(api.createLicense).not.toHaveBeenCalled();
  });

  it("refuses a currency that is not a 3-letter code", async () => {
    const user = await openNewLicense();

    await pick(user, "Product", /Acme Office Suite/);
    await user.type(screen.getByLabelText("Cost (optional)"), "1200.00");
    await user.type(screen.getByLabelText("Currency"), "US");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/3-letter code/);
    expect(api.createLicense).not.toHaveBeenCalled();
  });

  // The valid path, so the rules above cannot be satisfied by a form that
  // refuses everything: money travels as the typed STRING, never a number.
  it("sends the entered values once they are valid, with money as a string", async () => {
    const user = await openNewLicense();

    await pick(user, "Product", /Acme Office Suite/);
    await pick(user, "Vendor (optional)", /Acme Software/);
    await user.clear(screen.getByLabelText("Entitlements"));
    await user.type(screen.getByLabelText("Entitlements"), "50");
    await user.type(screen.getByLabelText("Cost (optional)"), "1200.00");
    await user.type(screen.getByLabelText("Currency"), "eur");
    await user.click(screen.getByRole("button", { name: "Save" }));

    // The internal client (`/me`'s `org_id`) until phase 3's selector.
    expect(api.createLicense).toHaveBeenCalledWith({
      client_id: "o1",
      product_id: "p1",
      vendor_id: "v1",
      type: "perpetual",
      entitlements: 50,
      key: null,
      cost: "1200.00",
      currency: "EUR",
    });
  });

  // The dialog is a real <form> now, which is what makes Enter work at all --
  // ui.tsx's Button sets no type, so before this the submit control was an
  // orphan and Enter did nothing anywhere in the product.
  it("submits on Enter from a field, not only on a mouse click", async () => {
    const user = await openNewLicense();

    await pick(user, "Product", /Acme Office Suite/);
    await user.click(screen.getByLabelText("Entitlements"));
    await user.keyboard("{Enter}");

    expect(api.createLicense).toHaveBeenCalledWith(
      expect.objectContaining({ product_id: "p1", entitlements: 1 }),
    );
  });
});

describe("LicenseDetail", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      getLicense: vi.fn().mockResolvedValue(LICENSES[0]),
      getProduct: vi.fn().mockResolvedValue(PRODUCTS[0]),
      listAllocations: vi.fn().mockResolvedValue([]),
      listUsers: vi.fn().mockResolvedValue({ users: [] }),
      revealLicenseKey: vi.fn().mockResolvedValue({ key: "ABCD-1234" }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  function renderDetail() {
    return renderWithProviders(
      <Routes>
        <Route path="/licenses/:licenseId" element={<LicenseDetail />} />
      </Routes>,
      ["/licenses/l1"],
    );
  }

  it("shows the Reveal key control when the caller has licenses.keys.read", async () => {
    renderDetail();

    await screen.findByText("Acme Office Suite");
    expect(screen.getByRole("button", { name: "Reveal key" })).toBeInTheDocument();
  });

  // DESIGN REVIEW M14. The licence-type pill was the first child of the head's
  // `actions`, so `.ph-act` -- a row whose whole job is "the things you can
  // press here" -- opened with something that cannot be pressed, and the
  // reader met "Perpetual" between the heading and "Reveal key". Every other
  // record page in this console puts its state in a pill row directly under
  // the head: /service-desk/tickets/:id, /portal/tickets/:id, /devices/:id.
  it("keeps the licence type under the title, not in the head's button row", async () => {
    renderDetail();

    await screen.findByText("Acme Office Suite");

    const head = document.querySelector(".ph") as HTMLElement;
    // Nothing unpressable in the action row.
    expect(head.querySelectorAll("[data-kind]")).toHaveLength(0);
    expect(head.querySelector(".ph-act")).toContainElement(
      screen.getByRole("button", { name: "Reveal key" }),
    );

    // The state is a pill, and it sits outside the head.
    const pill = screen.getByText("Perpetual").closest("[data-kind]") as HTMLElement;
    expect(pill).not.toBeNull();
    expect(pill.closest(".ph")).toBeNull();
  });

  it("hides the Reveal key control when the caller lacks licenses.keys.read", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME,
      permissions: ME.permissions.filter((p) => p !== "licenses.keys.read"),
    });
    renderDetail();

    await screen.findByText("Acme Office Suite");
    expect(screen.queryByRole("button", { name: "Reveal key" })).not.toBeInTheDocument();
  });

  // Cycle 5. `allocated_at`/`released_at` are `datetime` -- instants -- so
  // they take the operator's PROFILE zone, and an allocation still held
  // renders NO_DATE rather than "Invalid Date".
  it("renders allocation instants in the profile timezone and an open one as an em dash", async () => {
    const allocation: AllocationOut = {
      id: "a1",
      license_id: "l1",
      target_type: "device",
      target_id: "d1",
      allocated_at: "2026-01-01T23:00:00.000Z",
      released_at: null,
    };
    api.listAllocations = vi.fn().mockResolvedValue([allocation]);
    renderDetail();

    // 23:00Z is 08:00 the next day in Asia/Tokyo.
    expect(await screen.findByText("02 Jan 2026, 08:00")).toBeInTheDocument();
    const row = screen.getByText("02 Jan 2026, 08:00").closest("tr") as HTMLElement;
    expect(within(row).getByText("—")).toBeInTheDocument();
    expect(row.textContent).not.toContain("Invalid Date");
  });

  // A row that names a person shows the person (brief: `Person` leads rows
  // that name one). The directory is the same `listUsers` the subscriptions
  // owner column reads; a user the caller cannot read, or a device, keeps the
  // id the API returned, because an id is still more than nothing.
  it("renders a user allocation as the person the directory names, and the id otherwise", async () => {
    api.listUsers = vi.fn().mockResolvedValue({
      users: [{ id: "u1", email: "jane@example.com", display_name: "Jane Doe", status: "active", roles: [] }],
    });
    api.listAllocations = vi.fn().mockResolvedValue([
      { id: "a1", license_id: "l1", target_type: "user", target_id: "u1", allocated_at: "2026-01-01T00:00:00Z", released_at: null },
      { id: "a2", license_id: "l1", target_type: "user", target_id: "u-unknown", allocated_at: "2026-01-01T00:00:00Z", released_at: null },
      { id: "a3", license_id: "l1", target_type: "device", target_id: "d1", allocated_at: "2026-01-01T00:00:00Z", released_at: null },
    ]);
    renderDetail();

    const jane = await screen.findByText("Jane Doe");
    expect(jane.closest("tr")?.textContent).not.toContain("u1");
    expect(screen.getByText("u-unknown")).toBeInTheDocument();
    expect(screen.getByText("d1")).toBeInTheDocument();
  });

  // The other half of cycle 5, and the easier one to get wrong: `purchased_at`
  // is a `date`, so it must NOT move with the zone. Asia/Tokyo is +9, so a
  // zone-aware rendering of "2025-01-01" still says 01 Jan -- the fixture
  // below uses a UTC-midnight boundary that a western zone would shift, and
  // the assertion is the recorded day either way.
  it("renders a purchase DATE as the day recorded, not a zone-shifted one", async () => {
    api.me = vi.fn().mockResolvedValue({ ...ME, timezone: "America/Los_Angeles" });
    renderDetail();

    // "2025-01-01" through America/Los_Angeles (-8) would read 31 Dec 2024.
    expect(await screen.findByText(/01 Jan 2025/)).toBeInTheDocument();
    expect(screen.queryByText(/31 Dec 2024/)).not.toBeInTheDocument();
  });

  // Cycle 4. Revealing a key is an audited, privileged call; a refusal that
  // renders "ApiError:" tells the operator nothing and announces nothing.
  it("announces a bodyless 503 from Reveal key as a sentence, not as \"ApiError:\"", async () => {
    const user = userEvent.setup();
    api.revealLicenseKey = vi.fn().mockRejectedValue(bodylessUnavailable());
    renderDetail();

    await screen.findByText("Acme Office Suite");
    await user.click(screen.getByRole("button", { name: "Reveal key" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/service is unavailable right now/i);
    expect(alert.textContent).not.toContain("ApiError");
  });

  // A 403 is the likely refusal on this control specifically -- the server
  // owns `licenses.keys.read` and hiding the button here is cosmetic.
  it("says a refused reveal was a permission problem", async () => {
    const user = userEvent.setup();
    api.revealLicenseKey = vi.fn().mockRejectedValue(new ApiError(403, ""));
    renderDetail();

    await screen.findByText("Acme Office Suite");
    await user.click(screen.getByRole("button", { name: "Reveal key" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You do not have permission to do that.",
    );
  });

  it("reveals the key once via a dedicated call, not the cached license query", async () => {
    const user = userEvent.setup();
    renderDetail();

    await screen.findByText("Acme Office Suite");
    await user.click(screen.getByRole("button", { name: "Reveal key" }));

    expect(await screen.findByText("ABCD-1234")).toBeInTheDocument();
    expect(api.revealLicenseKey).toHaveBeenCalledWith("l1");
  });

  // A SWALLOWED FAILURE ON A ONE-SHOT SECRET. `void navigator.clipboard
  // ?.writeText(revealed)` threw the promise away, and `writeText` REJECTS on
  // a document that is not focused and on a denied permission. The operator
  // saw a Copy button that looked like it had worked, closed a dialog that
  // shows the key exactly once, and pasted whatever was on the clipboard
  // before. Both outcomes are now reported.
  it("says the copy failed rather than letting a one-shot key look copied", async () => {
    const user = userEvent.setup();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockRejectedValue(new Error("Document is not focused")) },
    });
    renderDetail();

    await screen.findByText("Acme Office Suite");
    await user.click(screen.getByRole("button", { name: "Reveal key" }));
    await screen.findByText("ABCD-1234");
    await user.click(screen.getByRole("button", { name: "Copy" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Document is not focused");
  });

  it("confirms a copy that actually succeeded", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    renderDetail();

    await screen.findByText("Acme Office Suite");
    await user.click(screen.getByRole("button", { name: "Reveal key" }));
    await screen.findByText("ABCD-1234");
    await user.click(screen.getByRole("button", { name: "Copy" }));

    expect(await screen.findByRole("status")).toHaveTextContent("Copied.");
    expect(writeText).toHaveBeenCalledWith("ABCD-1234");
    // Nothing claims a failure on the path that worked.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

// These three pages had no tests before this migration. The assertions
// worth having are the three states DevicesPage.test.tsx establishes: a mock
// is always loaded, always populated and never fails, but a console is
// regularly all three, and before this migration each of these pages
// rendered an identical empty table body for "still loading", "nothing
// here" and "the request failed" alike.
describe("ComplianceDashboard", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = { me: vi.fn().mockResolvedValue(ME) };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  const ROW: ComplianceRow = {
    client_id: "o1",
    product_id: "p1",
    name: "Acme Office Suite",
    entitled: 50,
    installed: 55,
    delta: 5,
    breach: true,
  };

  it("says it is loading rather than showing an empty table", async () => {
    api.licenseCompliance = () => new Promise<never>(() => {});
    renderWithProviders(<ComplianceDashboard />);
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty table", async () => {
    api.licenseCompliance = vi.fn().mockRejectedValue(new Error("boom"));
    renderWithProviders(<ComplianceDashboard />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/No compliance data/i)).not.toBeInTheDocument();
  });

  it("says there is no compliance data when the list is genuinely empty", async () => {
    api.licenseCompliance = vi.fn().mockResolvedValue([]);
    renderWithProviders(<ComplianceDashboard />);
    expect(await screen.findByText("No compliance data yet.")).toBeInTheDocument();
  });

  it("renders compliance rows from the mocked api", async () => {
    api.licenseCompliance = vi.fn().mockResolvedValue([ROW]);
    renderWithProviders(<ComplianceDashboard />);
    expect(await screen.findByText("Acme Office Suite")).toBeInTheDocument();
    // v3's pill says by how much: 55 installed against 50 entitled.
    expect(screen.getByText("5 over")).toBeInTheDocument();
    expect(screen.getByText("55 / 50")).toBeInTheDocument();
  });

  // MSP clients phase 2: a license is one client's, so compliance is one row
  // per (client, product) and a client's breach is never hidden by another's
  // spare seats. Each row says whose it is.
  it("shows a product once per client, each row naming its client", async () => {
    api.me = vi.fn().mockResolvedValue(ME_READS_CLIENTS);
    api.listClients = vi.fn().mockResolvedValue([clientOut("o1", "Novieri"), clientOut("c2", "Globex")]);
    api.licenseCompliance = vi.fn().mockResolvedValue([
      ROW,
      { ...ROW, client_id: "c2", installed: 10, delta: -40, breach: false },
    ]);
    renderWithProviders(<ComplianceDashboard />);

    expect(await screen.findByRole("columnheader", { name: "Client" })).toBeInTheDocument();
    const titles = await screen.findAllByText("Acme Office Suite");
    expect(titles).toHaveLength(2);
    const [mine, theirs] = titles.map((title) => title.closest("tr") as HTMLElement);
    expect(await within(mine as HTMLElement).findByText("Novieri")).toBeInTheDocument();
    expect(within(mine as HTMLElement).getByText("5 over")).toBeInTheDocument();
    expect(within(theirs as HTMLElement).getByText("Globex")).toBeInTheDocument();
    expect(within(theirs as HTMLElement).getByText("40 free")).toBeInTheDocument();
    expect(api.listClients).toHaveBeenCalledWith({ include_archived: true });
  });

  it("names a row's client by its id for someone who cannot read clients", async () => {
    api.listClients = vi.fn();
    api.licenseCompliance = vi.fn().mockResolvedValue([ROW]);
    renderWithProviders(<ComplianceDashboard />);
    const row = (await screen.findByText("Acme Office Suite")).closest("tr") as HTMLElement;
    expect(within(row).getByText("o1")).toBeInTheDocument();
    expect(api.listClients).not.toHaveBeenCalled();
  });
});

describe("RenewalCalendar (licenses)", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = { me: vi.fn().mockResolvedValue(ME), listProducts: vi.fn().mockResolvedValue(PRODUCTS) };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  const ROW: RenewalRow = {
    license_id: "l1",
    client_id: "o1",
    product_id: "p1",
    trigger: "renewal",
    due_date: "2026-09-18",
    due_in_days: 5,
  };

  it("says it is loading rather than showing an empty table", async () => {
    api.licenseRenewals = () => new Promise<never>(() => {});
    renderWithProviders(<RenewalCalendar />);
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty table", async () => {
    api.licenseRenewals = vi.fn().mockRejectedValue(new Error("boom"));
    renderWithProviders(<RenewalCalendar />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/No upcoming renewals/i)).not.toBeInTheDocument();
  });

  it("says there are no renewals when the list is genuinely empty", async () => {
    api.licenseRenewals = vi.fn().mockResolvedValue([]);
    renderWithProviders(<RenewalCalendar />);
    expect(await screen.findByText("No upcoming renewals.")).toBeInTheDocument();
  });

  it("renders renewal rows from the mocked api", async () => {
    api.licenseRenewals = vi.fn().mockResolvedValue([ROW]);
    renderWithProviders(<RenewalCalendar />);
    expect(await screen.findByText("Acme Office Suite")).toBeInTheDocument();
    expect(screen.getByText("5 days")).toBeInTheDocument();
  });

  it("names each renewal's client (MSP clients phase 2)", async () => {
    api.me = vi.fn().mockResolvedValue(ME_READS_CLIENTS);
    api.listClients = vi.fn().mockResolvedValue([clientOut("o1", "Novieri"), clientOut("c2", "Globex")]);
    api.licenseRenewals = vi.fn().mockResolvedValue([ROW, { ...ROW, license_id: "l9", client_id: "c2" }]);
    renderWithProviders(<RenewalCalendar />);

    expect(await screen.findByRole("columnheader", { name: "Client" })).toBeInTheDocument();
    const titles = await screen.findAllByText("Acme Office Suite");
    const clients = await Promise.all(
      titles.map(async (title) => {
        const row = title.closest("tr") as HTMLElement;
        return (await within(row).findByText(/Novieri|Globex/)).textContent;
      }),
    );
    expect(clients.sort()).toEqual(["Globex", "Novieri"]);
  });

  // Cycle 5. `due_date` is a `date` in schema.py, and `due_in_days` beside it
  // is computed from that same day server-side. Rendering the date through a
  // zone west of UTC moves it a day earlier and leaves the two cells in the
  // same row contradicting each other.
  it("renders the due date as the day recorded, in one format, not zone-shifted", async () => {
    api.licenseRenewals = vi.fn().mockResolvedValue([ROW]);
    renderWithProviders(<RenewalCalendar />);

    const row = (await screen.findByText("Acme Office Suite")).closest("tr") as HTMLElement;
    // "Sept", not "Sep": en-GB's short month for September is four
    // letters. Asserting the string the formatter actually produces is the
    // point -- a loose matcher here would pass against the ad-hoc rendering.
    expect(within(row).getByText("18 Sept 2026")).toBeInTheDocument();
    expect(row.textContent).not.toContain("17 Sept 2026");
  });
});

describe("ReviewQueue", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  const ROW: ReviewQueueRow = { name: "Unknown PDF Editor", publisher: "", device_count: 4 };

  it("says it is loading rather than showing an empty table", async () => {
    api.reviewQueue = () => new Promise<never>(() => {});
    renderWithProviders(<ReviewQueue />);
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty table", async () => {
    api.reviewQueue = vi.fn().mockRejectedValue(new Error("boom"));
    renderWithProviders(<ReviewQueue />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/Nothing to review/i)).not.toBeInTheDocument();
  });

  it("says there is nothing to review when the queue is genuinely empty", async () => {
    api.reviewQueue = vi.fn().mockResolvedValue([]);
    renderWithProviders(<ReviewQueue />);
    expect(await screen.findByText("Nothing to review.")).toBeInTheDocument();
  });

  it("renders review-queue rows from the mocked api", async () => {
    api.reviewQueue = vi.fn().mockResolvedValue([ROW]);
    renderWithProviders(<ReviewQueue />);
    expect(await screen.findByText("Unknown PDF Editor")).toBeInTheDocument();
    expect(screen.getByText("4")).toBeInTheDocument();
  });

  // DESIGN REVIEW M2, the mixed-cell case. `Map` was the bordered `outline`
  // button and `Ignore` beside it was `ghost`, which paints as bare accent
  // text -- the shape a link has. One cell, two treatments, and no way for
  // the reader to tell whether the second one navigated somewhere.
  //
  // THE CONVENTION IS THE CONSOLE'S OWN, written down at
  // `subscriptions/SubscriptionDetail.tsx`: "a row control is a control, not
  // a link". Every control in a row's action cell carries an edge, whether
  // the element under it is a `<button>` or a `<Link>`; `ghost` is not a
  // row-action treatment. Navigation versus mutation is carried by the
  // ELEMENT, which stays honest either way -- it is not a second visual
  // language layered on top.
  it("draws every control in a row's action cell with one treatment, not a button beside a bare link", async () => {
    api.reviewQueue = vi.fn().mockResolvedValue([ROW]);
    renderWithProviders(<ReviewQueue />);

    await screen.findByText("Unknown PDF Editor");
    const map = screen.getByRole("button", { name: "Map" });
    const ignore = screen.getByRole("button", { name: "Ignore" });

    // Same cell...
    const cell = map.closest("td");
    expect(cell).not.toBeNull();
    expect(cell).toContainElement(ignore);

    // ...and the same treatment: the kit's bordered `.btn`, neither of them
    // borderless.
    for (const control of [map, ignore]) {
      expect(control).toHaveClass("btn");
      expect(control).not.toHaveClass("ghost");
    }
  });

  // SWALLOWED ENTIRELY BEFORE THIS. `ignore.mutate(...)` had no `onError` and
  // `ignore.isError` was rendered nowhere on the page, so a refused Ignore
  // looked exactly like an accepted one: the click landed, the control
  // re-enabled, the row stayed put, and nothing said why. Auditor A's
  // swallowed-503 class on this route.
  it("announces a failed Ignore instead of leaving the row looking untouched", async () => {
    const user = userEvent.setup();
    api.reviewQueue = vi.fn().mockResolvedValue([ROW]);
    api.ignoreSoftware = vi.fn().mockRejectedValue(bodylessUnavailable());
    renderWithProviders(<ReviewQueue />);

    await screen.findByText("Unknown PDF Editor");
    await user.click(screen.getByRole("button", { name: "Ignore" }));
    // Cycle 8 put a confirmation in front of this: ignoring is permanent and
    // there is no un-ignore route.
    await confirmWith(user, "Ignore title");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/service is unavailable right now/i);
    expect(alert.textContent).not.toContain("ApiError");
    // And the row is still there, which is now consistent with what the page
    // says rather than contradicting it.
    expect(screen.getByText("Unknown PDF Editor")).toBeInTheDocument();
  });

  it("announces a failed Map instead of printing the raw error object", async () => {
    const user = userEvent.setup();
    api.reviewQueue = vi.fn().mockResolvedValue([ROW]);
    api.listProducts = vi.fn().mockResolvedValue(PRODUCTS);
    api.mapSoftware = vi.fn().mockRejectedValue(bodylessUnavailable());
    renderWithProviders(<ReviewQueue />);

    await screen.findByText("Unknown PDF Editor");
    await user.click(screen.getByRole("button", { name: "Map" }));
    await pick(user, "Product", /Acme Office Suite/);
    // Two controls read "Map" once the dialog is open -- the row's opener and
    // the dialog's submit. The submit is the later one in the tree.
    await user.click(screen.getAllByRole("button", { name: "Map" }).at(-1) as HTMLElement);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/service is unavailable right now/i);
    expect(alert.textContent).not.toContain("ApiError");
  });

  it("names the missing product on an empty Map submit instead of disabling it", async () => {
    const user = userEvent.setup();
    api.reviewQueue = vi.fn().mockResolvedValue([ROW]);
    api.listProducts = vi.fn().mockResolvedValue(PRODUCTS);
    api.mapSoftware = vi.fn();
    renderWithProviders(<ReviewQueue />);

    await screen.findByText("Unknown PDF Editor");
    await user.click(screen.getByRole("button", { name: "Map" }));

    const submit = screen.getAllByRole("button", { name: "Map" }).at(-1) as HTMLElement;
    expect(submit).toBeEnabled();
    await user.click(submit);

    expect(await screen.findByRole("alert")).toHaveTextContent("Product is required");
    expect(api.mapSoftware).not.toHaveBeenCalled();
  });
});

describe("license state vocabulary", () => {
  it("maps a real backend license type to a non-fallback badge variant", () => {
    // license.type vocabulary is sourced from the backend
    // (backend/app/modules/licenses/schema.py's LicenseType Literal) —
    // "perpetual" is a real type value.
    expect(licenseTypeVariant("perpetual")).not.toBe("muted");
    expect(licenseTypeVariant("perpetual")).toBe("info");
  });

  it("falls back to muted for an unknown/invented license type", () => {
    // Guards against the Phase-5 regression: no invented type name may
    // resolve to a real variant.
    expect(licenseTypeVariant("not_a_real_type")).toBe("muted");
  });

  it("maps a compliance breach to destructive", () => {
    expect(complianceVariant(true)).toBe("destructive");
  });

  it("maps compliance ok to success", () => {
    expect(complianceVariant(false)).toBe("success");
  });
});

// ---------------------------------------------------------------------------
// UX remediation cycles 6, 7 and 8 on this module.
// ---------------------------------------------------------------------------

// Auditor B, [HIGH], /licenses -> New license: "The Product `<select>` contains
// exactly 1 option -- the placeholder 'Select a product…' ... With no product
// selectable, Save is a silent no-op and no license can ever be created. The
// tab bar offers a 'Products' and a 'Vendors' tab that would seed these lists,
// but the modal neither says so nor links to them."
describe("New license: the empty-lookup blocked flow", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listLicenses: vi.fn().mockResolvedValue([]),
      listProducts: vi.fn().mockResolvedValue([]),
      listVendors: vi.fn().mockResolvedValue([]),
      licenseCompliance: vi.fn().mockResolvedValue([]),
      licenseRenewals: vi.fn().mockResolvedValue([]),
      createLicense: vi.fn(),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  async function openDialog() {
    const user = userEvent.setup();
    renderWithProviders(<LicensesPage />);
    await screen.findByText("No licenses yet.");
    await user.click(screen.getByRole("button", { name: "New license" }));
    return user;
  }

  it("says what would fill an empty product catalog instead of offering a dead list", async () => {
    const user = await openDialog();

    await user.click(screen.getByRole("combobox", { name: "Product" }));

    const list = await screen.findByRole("listbox", { name: "Product" });
    expect(list).toHaveTextContent(/No products in the catalog/i);
    expect(list).toHaveTextContent(/Licenses → Products/);
    // The second live reason: these reads are permission-gated, so a hint that
    // asserted only "there are none" would send a reader to a screen they
    // cannot use.
    expect(list).toHaveTextContent(/permission/i);
  });

  it("links to the screen that fills the catalog, so the escape hatch is one click", async () => {
    await openDialog();

    const link = await screen.findByRole("link", { name: /Add a product to the catalog/ });
    expect(link).toHaveAttribute("href", "/licenses/products");
    expect(screen.getByRole("link", { name: /Add a vendor/ })).toHaveAttribute(
      "href",
      "/licenses/vendors",
    );
  });

  it("does not offer the add-a-product link while the catalog is still loading", async () => {
    // A list in flight is not an empty list. Telling somebody to go and create
    // a product during the first fetch is a false blocked-flow report.
    api.listProducts = () => new Promise<never>(() => {});
    const user = userEvent.setup();
    renderWithProviders(<LicensesPage />);
    await screen.findByText("No licenses yet.");
    await user.click(screen.getByRole("button", { name: "New license" }));

    expect(
      screen.queryByRole("link", { name: /Add a product to the catalog/ }),
    ).not.toBeInTheDocument();
  });

  it("hides the add-a-product link once the catalog has something in it", async () => {
    api.listProducts = vi.fn().mockResolvedValue(PRODUCTS);
    const user = await openDialog();

    await user.click(screen.getByRole("combobox", { name: "Product" }));
    await screen.findByRole("option", { name: /Acme Office Suite/ });
    expect(
      screen.queryByRole("link", { name: /Add a product to the catalog/ }),
    ).not.toBeInTheDocument();
  });
});

describe("New license: choosing rather than typing", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listLicenses: vi.fn().mockResolvedValue(LICENSES),
      listProducts: vi.fn().mockResolvedValue(PRODUCTS),
      listVendors: vi.fn().mockResolvedValue(VENDORS),
      licenseCompliance: vi.fn().mockResolvedValue([]),
      licenseRenewals: vi.fn().mockResolvedValue([]),
      createLicense: vi.fn().mockResolvedValue(LICENSES[0]),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  async function openDialog() {
    const user = userEvent.setup();
    renderWithProviders(<LicensesPage />);
    await screen.findByText("Acme Office Suite");
    await user.click(screen.getByRole("button", { name: "New license" }));
    return user;
  }

  // The picker header's own rule: the sublabel is what tells two options
  // apart. A catalog normalized on name carries the same product from two
  // publishers, and a license attached to the wrong one misreports compliance
  // for both.
  it("shows the publisher beside each product, not just its name", async () => {
    const user = await openDialog();

    await user.click(screen.getByRole("combobox", { name: "Product" }));
    const list = await screen.findByRole("listbox", { name: "Product" });
    expect(
      within(list).getByRole("option", { name: /Acme Office Suite\s*Acme Software/ }),
    ).toBeInTheDocument();
  });

  it("refuses a product the operator only typed into the picker without choosing", async () => {
    const user = await openDialog();

    // The box FILTERS; it does not accept. A typed name that was never chosen
    // must not become a product id.
    await user.type(screen.getByRole("combobox", { name: "Product" }), "Acme Office Suite");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Product is required");
    expect(api.createLicense).not.toHaveBeenCalled();
  });

  // Auditor B, [LOW]: "License keys are long alphanumeric strings that are
  // usually pasted and then visually verified; masking without a reveal makes
  // transcription errors undetectable."
  it("masks the license key by default and reveals it on request", async () => {
    const user = await openDialog();

    const key = screen.getByLabelText("Key (optional)");
    expect(key).toHaveAttribute("type", "password");

    await user.click(screen.getByRole("button", { name: "Show key" }));
    expect(screen.getByLabelText("Key (optional)")).toHaveAttribute("type", "text");

    await user.click(screen.getByRole("button", { name: "Hide key" }));
    expect(screen.getByLabelText("Key (optional)")).toHaveAttribute("type", "password");
  });

  // Auditor B, [MEDIUM]: "`min=""` and `max=""` -- no bounds ... a negative or
  // zero entitlement count would corrupt the compliance calculation that is
  // this module's whole purpose."
  it("refuses a zero entitlement count rather than sending it", async () => {
    const user = await openDialog();

    await pick(user, "Product", /Acme Office Suite/);
    await user.clear(screen.getByLabelText("Entitlements"));
    await user.type(screen.getByLabelText("Entitlements"), "0");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/at least 1/);
    expect(api.createLicense).not.toHaveBeenCalled();
  });
});

// Cycle 8. Ignoring writes a `software_ignore` row and this API serves no
// route that removes one, so the title leaves the compliance reconciliation
// permanently -- on a ghost button beside "Map".
describe("ReviewQueue: Ignore asks first", () => {
  const ROW: ReviewQueueRow = { name: "Unknown PDF Editor", publisher: "Unknown", device_count: 12 };
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      reviewQueue: vi.fn().mockResolvedValue([ROW]),
      listProducts: vi.fn().mockResolvedValue(PRODUCTS),
      ignoreSoftware: vi.fn().mockResolvedValue({ id: "i1", name: ROW.name }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("does not ignore anything on the first click", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ReviewQueue />);

    await screen.findByText("Unknown PDF Editor");
    await user.click(screen.getByRole("button", { name: "Ignore" }));

    expect(api.ignoreSoftware).not.toHaveBeenCalled();
  });

  it("names the title and its device count, and says there is no way back", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ReviewQueue />);

    await screen.findByText("Unknown PDF Editor");
    await user.click(screen.getByRole("button", { name: "Ignore" }));

    const dialog = await screen.findByRole("dialog", { name: /Ignore this software title/i });
    // The count is the difference between ignoring a stray utility and
    // ignoring something installed on twelve endpoints.
    expect(dialog).toHaveTextContent("Unknown PDF Editor (12 devices)");
    expect(dialog).toHaveTextContent(/cannot be brought back from the console/i);
  });

  it("ignores the title once the confirmation is accepted", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ReviewQueue />);

    await screen.findByText("Unknown PDF Editor");
    await user.click(screen.getByRole("button", { name: "Ignore" }));
    await confirmWith(user, "Ignore title");

    expect(api.ignoreSoftware).toHaveBeenCalledWith({ name: "Unknown PDF Editor" });
  });

  it("ignores nothing when the confirmation is dismissed", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ReviewQueue />);

    await screen.findByText("Unknown PDF Editor");
    await user.click(screen.getByRole("button", { name: "Ignore" }));
    const dialog = await screen.findByRole("dialog", { name: /Ignore this software title/i });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(api.ignoreSoftware).not.toHaveBeenCalled();
    expect(screen.getByText("Unknown PDF Editor")).toBeInTheDocument();
  });

  it("says what would fill an empty product catalog in the Map dialog too", async () => {
    api.listProducts = vi.fn().mockResolvedValue([]);
    const user = userEvent.setup();
    renderWithProviders(<ReviewQueue />);

    await screen.findByText("Unknown PDF Editor");
    await user.click(screen.getByRole("button", { name: "Map" }));
    await user.click(screen.getByRole("combobox", { name: "Product" }));

    expect(await screen.findByRole("listbox", { name: "Product" })).toHaveTextContent(
      /No products in the catalog/i,
    );
    expect(screen.getByRole("link", { name: /Add a product to the catalog/ })).toHaveAttribute(
      "href",
      "/licenses/products",
    );
  });
});

// Cycle 7. `DELETE /licenses/{license_id}` and `useDeleteLicenseMutation` both
// existed and nothing called either: a license entered against the wrong
// product could only be corrected by someone with database access.
describe("LicenseDetail: deleting a license", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      getLicense: vi.fn().mockResolvedValue(LICENSES[0]),
      getProduct: vi.fn().mockResolvedValue(PRODUCTS[0]),
      listAllocations: vi.fn().mockResolvedValue([]),
      listUsers: vi.fn().mockResolvedValue({ users: [] }),
      revealLicenseKey: vi.fn().mockResolvedValue({ key: "ABCD-1234" }),
      deleteLicense: vi.fn().mockResolvedValue(undefined),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  function renderDetail() {
    return renderWithProviders(
      <Routes>
        <Route path="/licenses/:licenseId" element={<LicenseDetail />} />
      </Routes>,
      ["/licenses/l1"],
    );
  }

  it("offers the delete the API has always served", async () => {
    renderDetail();

    await screen.findByText("Acme Office Suite");
    expect(screen.getByRole("button", { name: "Delete license" })).toBeInTheDocument();
  });

  it("hides it from a caller without licenses.manage", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME,
      permissions: ME.permissions.filter((p) => p !== "licenses.manage"),
    });
    renderDetail();

    await screen.findByText("Acme Office Suite");
    expect(screen.queryByRole("button", { name: "Delete license" })).not.toBeInTheDocument();
  });

  it("deletes nothing on the first click, and names what would go", async () => {
    const user = userEvent.setup();
    renderDetail();

    await screen.findByText("Acme Office Suite");
    await user.click(screen.getByRole("button", { name: "Delete license" }));

    expect(api.deleteLicense).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("dialog", { name: /Delete license/i });
    // Product AND entitlement count: an estate carries several licenses for
    // one product and the count is what tells them apart on this screen.
    expect(dialog).toHaveTextContent("Acme Office Suite, 50 entitlements");
    expect(dialog).toHaveTextContent(/over-deployed/i);
  });

  it("deletes once the confirmation is accepted", async () => {
    const user = userEvent.setup();
    renderDetail();

    await screen.findByText("Acme Office Suite");
    await user.click(screen.getByRole("button", { name: "Delete license" }));
    await confirmWith(user, "Delete license");

    expect(api.deleteLicense).toHaveBeenCalledWith("l1");
  });

  // The swallowed-503 class, on the one control here that destroys data: the
  // page must not look like the record went away when the server refused.
  it("announces a bodyless 503 from the delete rather than looking successful", async () => {
    const user = userEvent.setup();
    api.deleteLicense = vi.fn().mockRejectedValue(bodylessUnavailable());
    renderDetail();

    await screen.findByText("Acme Office Suite");
    await user.click(screen.getByRole("button", { name: "Delete license" }));
    await confirmWith(user, "Delete license");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/service is unavailable right now/i);
    expect(alert.textContent).not.toContain("ApiError");
    // Still on the detail page, which is now consistent with the server's
    // answer rather than contradicting it.
    expect(screen.getByText("Acme Office Suite")).toBeInTheDocument();
  });
});

// The sublabels are what make a picker safer than a text box, so they are
// tested as data rather than by inspecting a JSX tree.
describe("licenses picker options", () => {
  it("distinguishes products by publisher", () => {
    expect(productPickerOptions(PRODUCTS)).toEqual([
      { id: "p1", label: "Acme Office Suite", sublabel: "Acme Software" },
    ]);
  });

  it("says so rather than rendering a blank when a product has no publisher", () => {
    // An empty sublabel reads as "same publisher as the row above".
    expect(productPickerOptions([{ ...PRODUCTS[0]!, publisher: "" }])[0]!.sublabel).toBe(
      "no publisher recorded",
    );
  });

  it("distinguishes vendors by website", () => {
    expect(vendorPickerOptions(VENDORS)).toEqual([
      { id: "v1", label: "Acme Software", sublabel: "https://acme.example" },
    ]);
    expect(vendorPickerOptions([{ ...VENDORS[0]!, website: null }])[0]!.sublabel).toBe(
      "no website recorded",
    );
  });

  it("names the console surface that fills each list, and the permission that hides it", () => {
    expect(PRODUCT_PICKER_EMPTY_HINT).toMatch(/Licenses → Products/);
    expect(PRODUCT_PICKER_EMPTY_HINT).toMatch(/permission/i);
    expect(VENDOR_PICKER_EMPTY_HINT).toMatch(/Licenses → Vendors/);
    expect(VENDOR_PICKER_EMPTY_HINT).toMatch(/permission/i);
  });
});

// ---------------------------------------------------------------------------
// Cycle 12.
// ---------------------------------------------------------------------------

/**
 * Auditor B, [MEDIUM]: "`INPUT[number]` with `min=""` and `max=""` -- no
 * bounds ... A negative or zero entitlement count would corrupt the compliance
 * calculation that is this module's whole purpose." And [HIGH] on the same
 * dialog: "Not one of the six fields is marked `required`."
 *
 * `refuses a zero entitlement count rather than sending it` above covers the
 * submit-time refusal. This covers what the auditor MEASURED -- the attributes
 * on the control, which are what the spinner obeys and what assistive tech
 * announces before anything is submitted.
 */
describe("New license: the bounds are on the control, not only in the rule", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listLicenses: vi.fn().mockResolvedValue(LICENSES),
      listProducts: vi.fn().mockResolvedValue(PRODUCTS),
      listVendors: vi.fn().mockResolvedValue(VENDORS),
      licenseCompliance: vi.fn().mockResolvedValue([]),
      licenseRenewals: vi.fn().mockResolvedValue([]),
      createLicense: vi.fn().mockResolvedValue(LICENSES[0]),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  async function openNewLicense() {
    const user = userEvent.setup();
    renderWithProviders(<LicensesPage />);
    await user.click(await screen.findByRole("button", { name: "New license" }));
    return user;
  }

  it("bounds Entitlements to whole seats of at least one", async () => {
    await openNewLicense();

    const entitlements = screen.getByLabelText("Entitlements");
    expect(entitlements).toHaveAttribute("type", "number");
    expect(entitlements).toHaveAttribute("min", "1");
    expect(entitlements).toHaveAttribute("step", "1");
    expect(entitlements).toBeRequired();
  });

  // ISO 4217 is three letters, and the audit's "ZZZZZ" reached the server
  // because nothing here stopped it.
  it("caps Currency at the length of an ISO 4217 code", async () => {
    await openNewLicense();

    expect(screen.getByLabelText("Currency")).toHaveAttribute("maxlength", "3");
  });
});
