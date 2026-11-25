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

/** The fixture session with `/me` held: the chip's client is not decided yet. */
function meHeld() {
  return fixtureClient({ ...fixtureRegistry(), me: () => new Promise<never>(() => {}) });
}

describe("no empty state before the narrowed read (coordinator's ruling 10)", () => {
  it.each([
    ["the register", `/licenses?client=${ANDES}`, <LicensesPage key="l" />, ["No licenses yet."]],
    ["compliance", `/licenses/compliance?client=${ANDES}`, <ComplianceDashboard key="c" />, ["No compliance data yet."]],
    ["the renewal calendar", `/licenses/renewals?client=${ANDES}`, <RenewalCalendar key="r" />, ["No license renews in this period."]],
  ])("%s draws loading while the Client chip's client is still being decided", async (_name, path, page, empties) => {
    mockUseApi.mockReturnValue(meHeld());
    renderPage(path, page);
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    for (const empty of [...empties, "Nothing matches these filters."]) expect(screen.queryByText(empty)).toBeNull();
  });
});

/**
 * The coordinator's ruling 10a (corrected): a tile with no data, pending or
 * failed, draws the kit's absent mark (`shell.viz.na`, "n/a") and no line
 * under it -- never "—", never "0".
 */
function expectNoFigure(tile: HTMLElement) {
  expect(tile.querySelector(".v")?.textContent).toBe("n/a");
  expect(tile.querySelector(".z")).toBeNull();
}

describe("no tile figure before its read (coordinator's ruling 10a)", () => {
  it("draws no count and no claim on the register's narrowed tiles while the chip's client is being decided", async () => {
    mockUseApi.mockReturnValue(meHeld());
    renderPage(`/licenses?client=${ANDES}`, <LicensesPage />, "/licenses");
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    for (const name of [/Over-deployed/, /Renewals in 90 days/, /Compliant/]) expectNoFigure(screen.getByRole("link", { name }));
    for (const claim of ["nothing over-deployed", "nothing renews in this window", "nothing reconciled yet"]) {
      expect(screen.queryByText(claim)).toBeNull();
    }
  });

  it.each([
    ["before its allocations answer", () => new Promise<never>(() => {}), false],
    ["when its allocations fail", () => Promise.reject(new Error("unavailable")), true],
  ])("draws no seat count on a license's record %s", async (_when, listAllocations, fails) => {
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), listAllocations } as never));
    renderPage("/licenses/lic-1", <LicenseDetail />, "/licenses/:licenseId");
    const inUse = (await screen.findByText("In use", { selector: ".k" })).closest(".tile") as HTMLElement;
    // A failed read: the allocations list beside the tiles says why.
    if (fails) expect(await screen.findByRole("alert")).toBeInTheDocument();
    expectNoFigure(inUse);
    expectNoFigure(screen.getByText("Free", { selector: ".k" }).closest(".tile") as HTMLElement);
  });
});

/**
 * The coordinator's ruling 3a: the leave question is drawn AFTER its form's
 * dialog, never before it or inside it. Both are `.modal`s at one z-index, so
 * the later one in the document paints on top; drawn first, the form covered
 * the question in a real browser.
 */
function expectDrawnAbove(question: HTMLElement, form: HTMLElement) {
  expect(form.contains(question)).toBe(false);
  expect(form.compareDocumentPosition(question) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
}

describe("the leave question is drawn on top of its dialog (coordinator's ruling 3a)", () => {
  it.each([
    ["New license", "/licenses?new=license", <LicensesPage key="l" />, "/licenses", "New license", "Cost (optional)"],
    ["New product", "/licenses/products?new=product", <ProductsPage key="p" />, "/licenses/products", "New product", "Name"],
    ["Edit product", "/licenses/products?edit=prod-visio", <ProductsPage key="e" />, "/licenses/products", "Edit product", "Name"],
    ["New vendor", "/licenses/vendors?new=vendor", <VendorsPage key="v" />, "/licenses/vendors", "New vendor", "Name"],
  ])("%s", async (_name, path, page, pattern, title, field) => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage(path, page, pattern);
    const form = await screen.findByRole("dialog", { name: title });
    await userEvent.type(within(form).getByLabelText(field), "12");
    await userEvent.click(within(form).getByRole("button", { name: "Close" }));
    expectDrawnAbove(await screen.findByRole("dialog", { name: "Leave without saving?" }), form);
  });
});

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

  it("opens, from a tile counted for the chip's client, exactly the rows it counted", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage(`/licenses?client=${ANDES}`, <LicensesPage />, "/licenses");
    // Once /me has said the chip's client is the person's (until then nothing is narrowed or counted).
    await waitFor(() =>
      expect(screen.getByRole("link", { name: /Over-deployed/ })).toHaveAttribute("href", `/licenses/compliance?client=${ANDES}`),
    );
    expect(screen.getByRole("link", { name: /Renewals in 90 days/ })).toHaveAttribute("href", `/licenses/renewals?client=${ANDES}`);
    expect(screen.getByRole("link", { name: /Compliant/ })).toHaveAttribute("href", `/licenses/compliance?client=${ANDES}`);
    // The catalog is the organisation's: its tile counts no client.
    expect(screen.getByRole("link", { name: /License products/ })).toHaveAttribute("href", "/licenses/products");
  });

  it("asks before leaving New license with something typed, and opens empty again", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/licenses", <LicensesPage />, "/licenses");
    await userEvent.click(await screen.findByRole("button", { name: "New license" }));
    let dialog = await screen.findByRole("dialog", { name: "New license" });
    await userEvent.type(within(dialog).getByLabelText("Cost (optional)"), "12");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    const leave = await screen.findByRole("dialog", { name: "Leave without saving?" });
    expect(router.state.location.search).toBe("?new=license");
    await userEvent.click(within(leave).getByRole("button", { name: "Leave without saving" }));
    await waitFor(() => expect(router.state.location.search).toBe(""));
    // The blocked Back has settled once both dialogs are gone.
    await waitFor(() => expect(screen.queryAllByRole("dialog")).toEqual([]));
    // Opened again, it starts empty (it is mounted only while open).
    await userEvent.click(screen.getByRole("button", { name: "New license" }));
    dialog = await screen.findByRole("dialog", { name: "New license" });
    expect(within(dialog).getByLabelText("Cost (optional)")).toHaveValue("");
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
  it("reads the positions only through the narrowed API of the Client chip's client (decision D6)", async () => {
    // On the console's own API: the product catalog (the organisation's, for
    // which products have a match rule), the fleet and the review queue (the
    // readiness panel and the no-usage rule: the organisation's setup, D20).
    const narrowing = recordingNarrowing(fixtureClient(), {
      console: ["listProducts", "listDevices", "reviewQueue"],
    });
    mockUseApi.mockReturnValue(narrowing.api);
    renderPage(`/licenses/compliance?client=${ANDES}`, <ComplianceDashboard />, "/licenses/compliance");
    expect(await screen.findByRole("table", { name: "License compliance" })).toBeInTheDocument();
    expect(narrowing.client(ANDES).licenseCompliance).toHaveBeenCalledWith();
    expect(narrowing.unnarrowed).toEqual([]);
  });

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
  it("reads the renewals only through the narrowed API of the Client chip's client (decision D6)", async () => {
    // On the console's own API: the product catalog, which names each row
    // (the organisation's, shared by every client).
    const narrowing = recordingNarrowing(fixtureClient(), { console: ["listProducts"] });
    mockUseApi.mockReturnValue(narrowing.api);
    renderPage(`/licenses/renewals?client=${ANDES}`, <RenewalCalendar />, "/licenses/renewals");
    expect(await screen.findByRole("table", { name: "Upcoming renewals" })).toBeInTheDocument();
    expect(narrowing.client(ANDES).licenseRenewals).toHaveBeenCalledWith(undefined);
    expect(narrowing.unnarrowed).toEqual([]);
  });

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

  it("says a product no rule can detect has no usage data, never 0 installs", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/licenses/products", <ProductsPage />, "/licenses/products");
    const row = (await screen.findByText("Windows Server 2022 user CAL", { selector: ".cell-title" })).closest("tr") as HTMLElement;
    expect(await within(row).findByText("no usage data")).toBeInTheDocument();
    expect(within(row).queryByText("0 installs")).toBeNull();
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

  it("asks before leaving an edit with a change in it", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/licenses/products?edit=prod-visio", <ProductsPage />, "/licenses/products");
    const dialog = await screen.findByRole("dialog", { name: "Edit product" });
    await userEvent.type(within(dialog).getByLabelText("Name"), " 2024");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(await screen.findByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.search).toBe("?edit=prod-visio");
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

  it("leaves New vendor without asking once it is saved", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    const { router } = renderPage("/licenses/vendors", <VendorsPage />, "/licenses/vendors");
    await userEvent.click(await screen.findByRole("button", { name: "New vendor" }));
    const dialog = await screen.findByRole("dialog", { name: "New vendor" });
    await userEvent.type(within(dialog).getByLabelText("Name"), "Globex");
    await userEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(router.state.location.search).toBe(""));
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
  });

  it("counts each vendor's licenses as before", async () => {
    mockUseApi.mockReturnValue(fixtureClient());
    renderPage("/licenses/vendors", <VendorsPage />, "/licenses/vendors");
    const row = (await screen.findByText("Microsoft", { selector: ".cell-title" })).closest("tr") as HTMLElement;
    expect(await within(row).findByText("3 licenses")).toBeInTheDocument();
  });
});
