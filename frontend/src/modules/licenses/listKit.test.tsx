/**
 * Licenses on the kit (redesign plan, wave O5, 5.1 to 5.3): the register's
 * search, type and Client chips in the URL, New license from `?new=license`,
 * a license's tab title and seats, the compliance figures that open the list
 * explaining them, the calendar by month, the review queue's empty state, and
 * the product and vendor catalogs' search, `?new=` and `?edit=`. Over the
 * fixture session, with the real client scope.
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry } from "../../fixtures/client";
import { recordingNarrowing } from "../../test/narrowing";
import { renderPage } from "../../test/renderPage";
import { ComplianceDashboard } from "./ComplianceDashboard";
import { LicenseDetail } from "./LicenseDetail";
import { LicensesPage } from "./LicensesPage";
import { ProductsPage } from "./ProductsPage";
import { RenewalCalendar } from "./RenewalCalendar";
import { ReviewQueue } from "./ReviewQueue";
import { VendorsPage } from "./VendorsPage";

const ANDES = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57"; // src/fixtures/core.ts CUSTOMER_ID

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

describe("the license register on the list kit", () => {
  it("keeps its filters in the URL, so a filtered view is a link", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/licenses?type=per_user", <LicensesPage />, "/licenses");
    const chips = await screen.findByRole("group", { name: "License type" });
    expect(await within(chips).findByRole("button", { name: /Per user/ })).toHaveAttribute("aria-pressed", "true");
    // The count is the register's own: two licenses are per user.
    expect(within(chips).getByRole("button", { name: /Per user/ })).toHaveTextContent("2");
    expect(screen.getByRole("link", { name: "Adobe Acrobat Pro" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Microsoft Visio" })).toBeNull();
    await userEvent.click(within(chips).getByRole("button", { name: /Perpetual/ }));
    expect(router.state.location.search).toContain("type=perpetual");
    expect(await screen.findByRole("link", { name: "JetBrains IntelliJ IDEA" })).toBeInTheDocument();
  });

  it("keeps its search in the URL, so a reload finds the same rows", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/licenses", <LicensesPage />, "/licenses");
    await userEvent.type(await screen.findByRole("textbox", { name: "Search licenses" }), "visio");
    expect(router.state.location.search).toBe("?q=visio");
    await act(() => router.navigate("/licenses?q=visio"));
    expect(screen.getByRole("textbox", { name: "Search licenses" })).toHaveValue("visio");
    expect(screen.getByRole("link", { name: "Microsoft Visio" })).toBeInTheDocument();
  });

  it("says why the list is empty", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/licenses?type=oem", <LicensesPage />, "/licenses");
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("says no license exists yet, and offers the first one", async () => {
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), listLicenses: () => [] }));
    renderPage("/licenses", <LicensesPage />, "/licenses");
    expect(await screen.findByText("No licenses yet.")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "New license" })).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Clear filters" })).toBeNull();
  });

  it("reads the list only through the narrowed API of the Client chip's client (decision D6)", async () => {
    // What the page reads on the console's own API, and why none is the
    // register or a tile's count: the product catalog and the vendors are the
    // organisation's (configuration every client shares); the readiness
    // panel asks whether a computer runs the agent and how many installed
    // titles are unmapped (`listDevices`, `reviewQueue`: the organisation's
    // setup, decided by the top bar, D20).
    const narrowing = recordingNarrowing(fixtureClient(), {
      console: ["listProducts", "listVendors", "listDevices", "reviewQueue"],
    });
    mockUseApi.mockReturnValue(narrowing.api);
    renderPage(`/licenses?client=${ANDES}`, <LicensesPage />, "/licenses");
    expect(await screen.findByRole("table", { name: "Licenses" })).toBeInTheDocument();
    expect(narrowing.client(ANDES).listLicenses).toHaveBeenCalledWith();
    // The tiles count the same client's licenses.
    expect(narrowing.client(ANDES).licenseCompliance).toHaveBeenCalledWith();
    expect(narrowing.client(ANDES).licenseRenewals).toHaveBeenCalledWith(90);
    expect(narrowing.unnarrowed).toEqual([]);
  });

  it("opens New license from ?new=license, and closing it leaves the list", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/licenses?new=license", <LicensesPage />, "/licenses");
    const dialog = await screen.findByRole("dialog", { name: "New license" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "New license" })).toBeNull());
    expect(router.state.location.search).toBe("");
  });
});

describe("a license on the record kit", () => {
  it("names the record in the browser tab", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/licenses/lic-1", <LicenseDetail />, "/licenses/:licenseId", { page: "shell.nav.item.licenses" });
    await screen.findByRole("heading", { level: 1, name: "Adobe Acrobat Pro" });
    await waitFor(() => expect(document.title).toBe("Adobe Acrobat Pro · Licenses · Novieri RMM"));
  });

  it("counts its seats: entitled, in use and free", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/licenses/lic-1", <LicenseDetail />, "/licenses/:licenseId");
    await screen.findByRole("heading", { level: 1, name: "Adobe Acrobat Pro" });
    // 50 entitled; three allocations, one of them released: two in use.
    const tile = (label: string) => screen.getByText(label).closest(".tile") as HTMLElement;
    await waitFor(() => expect(within(tile("In use")).getByText("2")).toBeInTheDocument());
    expect(within(tile("Entitled")).getByText("50")).toBeInTheDocument();
    expect(within(tile("Free")).getByText("48")).toBeInTheDocument();
    // The head goes back to the register.
    expect(screen.getByRole("link", { name: "All licenses" })).toHaveAttribute("href", "/licenses");
  });
});

describe("license compliance", () => {
  it("leads each figure to the list that explains it", async () => {
    mockUseApi.mockReturnValue(
      fixtureClient({
        ...fixtureRegistry(),
        licenseCompliance: () => [
          { client_id: ANDES, product_id: "prod-acrobat", name: "Adobe Acrobat Pro", entitled: 50, installed: 58, delta: -8, breach: true },
        ],
      }),
    );
    renderPage("/licenses/compliance", <ComplianceDashboard />, "/licenses/compliance");
    const figure = await screen.findByRole("link", { name: "58 / 50" });
    // The licenses of that product, narrowed to the row's client.
    expect(figure).toHaveAttribute("href", `/licenses?q=Adobe+Acrobat+Pro&client=${ANDES}`);
  });

  it("says a product with no usage data has none, never 0 installed", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/licenses/compliance", <ComplianceDashboard />, "/licenses/compliance");
    // The CAL has no match rule: nothing can count its installs.
    const row = (await screen.findByText("Windows Server 2022 user CAL")).closest("tr") as HTMLElement;
    expect(within(row).getByRole("link", { name: "no usage data, 300 owned" })).toBeInTheDocument();
    expect(within(row).getByText("No usage data")).toBeInTheDocument();
    expect(within(row).queryByText("300 free")).toBeNull();
  });
});

describe("the renewal calendar (licenses)", () => {
  it("says why the calendar is empty", async () => {
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), licenseRenewals: () => [] }));
    renderPage("/licenses/renewals", <RenewalCalendar />, "/licenses/renewals");
    expect(await screen.findByText("No license renews in this period.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clear filters" })).toBeNull();
  });

  it("groups the renewals by month, each date as recorded", async () => {
    mockUseApi.mockReturnValue(
      fixtureClient({
        ...fixtureRegistry(),
        licenseRenewals: () => [
          { license_id: "lic-1", client_id: ANDES, product_id: "prod-acrobat", trigger: "renewal", due_date: "2026-12-03", due_in_days: 8 },
          { license_id: "lic-3", client_id: ANDES, product_id: "prod-visio", trigger: "maintenance", due_date: "2027-01-31", due_in_days: 67 },
        ],
      }),
    );
    renderPage("/licenses/renewals", <RenewalCalendar />, "/licenses/renewals");
    const december = await screen.findByRole("rowheader", { name: "December 2026" });
    const january = screen.getByRole("rowheader", { name: "January 2027" });
    expect(december.compareDocumentPosition(january) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByText("31 Jan 2027")).toBeInTheDocument();
  });
});

describe("the review queue", () => {
  it("says why the list is empty", async () => {
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), reviewQueue: () => [] }));
    renderPage("/licenses/review-queue", <ReviewQueue />, "/licenses/review-queue");
    expect(await screen.findByText("Nothing to review.")).toBeInTheDocument();
  });
});

describe("the product catalog", () => {
  it("keeps its search in the URL, so a reload finds the same rows", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/licenses/products", <ProductsPage />, "/licenses/products");
    await userEvent.type(await screen.findByRole("textbox", { name: "Search products" }), "adobe");
    expect(router.state.location.search).toBe("?q=adobe");
    await act(() => router.navigate("/licenses/products?q=adobe"));
    expect(screen.getByRole("textbox", { name: "Search products" })).toHaveValue("adobe");
    expect(screen.getByText("Adobe Acrobat Pro", { selector: ".cell-title" })).toBeInTheDocument();
    expect(screen.queryByText("Microsoft Visio", { selector: ".cell-title" })).toBeNull();
  });

  it("says why the list is empty", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/licenses/products?q=nothing-like-it", <ProductsPage />, "/licenses/products");
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("opens a product's editor from ?edit=<id>", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/licenses/products?edit=prod-visio", <ProductsPage />, "/licenses/products");
    const dialog = await screen.findByRole("dialog", { name: "Edit product" });
    // The editor starts from the product it opens.
    expect(within(dialog).getByLabelText("Name")).toHaveValue("Microsoft Visio");
    expect(within(dialog).getByLabelText("Rule 1 pattern")).toHaveValue("Microsoft Visio");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(router.state.location.search).toBe(""));
  });

  it("saves an edit's match rules as it did before", async () => {
    const updateProduct = vi.fn((id: string, body: object) => ({ ...body, id, created_at: "2026-01-01T00:00:00Z" }));
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), updateProduct } as never));
    renderPage("/licenses/products", <ProductsPage />, "/licenses/products");
    const row = (await screen.findByText("Microsoft Visio", { selector: ".cell-title" })).closest("tr") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: "Edit" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit product" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(updateProduct).toHaveBeenCalledWith("prod-visio", {
        name: "Microsoft Visio",
        publisher: "Microsoft",
        match_rules: [{ type: "prefix", pattern: "Microsoft Visio", publisher: "Microsoft Corporation" }],
      }),
    );
  });
});

describe("vendors", () => {
  it("keeps its search in the URL, so a reload finds the same rows", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/licenses/vendors", <VendorsPage />, "/licenses/vendors");
    await userEvent.type(await screen.findByRole("textbox", { name: "Search vendors" }), "jet");
    expect(router.state.location.search).toBe("?q=jet");
    await act(() => router.navigate("/licenses/vendors?q=jet"));
    expect(screen.getByRole("textbox", { name: "Search vendors" })).toHaveValue("jet");
    expect(screen.getByText("JetBrains", { selector: ".cell-title" })).toBeInTheDocument();
    expect(screen.queryByText("Adobe", { selector: ".cell-title" })).toBeNull();
  });

  it("says why the list is empty", async () => {
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), listVendors: () => [] }));
    renderPage("/licenses/vendors", <VendorsPage />, "/licenses/vendors");
    expect(await screen.findByText("No vendors yet.")).toBeInTheDocument();
  });

  it("opens New vendor from ?new=vendor", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/licenses/vendors?new=vendor", <VendorsPage />, "/licenses/vendors");
    expect(await screen.findByRole("dialog", { name: "New vendor" })).toBeInTheDocument();
  });

  it("counts each vendor's licenses as before", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/licenses/vendors", <VendorsPage />, "/licenses/vendors");
    const row = (await screen.findByText("Microsoft", { selector: ".cell-title" })).closest("tr") as HTMLElement;
    expect(await within(row).findByText("3 licenses")).toBeInTheDocument();
  });
});
