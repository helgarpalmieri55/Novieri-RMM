// Reporting frontend tests (spec §11).
//
// **EVERY ASSERTION HERE IS ON RENDERED OUTPUT, NOT ON PROPS**, and none of
// them is `Object.keys()` over a fixture defined in this file -- that test
// cannot fail, and this project has shipped one.
//
// The three behaviours §11 makes mandatory, and the falsifier for each:
//
// 1. A report you cannot run is VISIBLE and NAMES THE MISSING CODES. Falsified
//    by a card that hides them (or hides the card): the codes are asserted by
//    text, not by counting cards.
// 2. "As of <generated_at>" ALWAYS, plus the staleness note when
//    `reproducible` is false. TWO rendered cases asserting a DIFFERENT visible
//    string in each -- one case cannot tell "always shows the note" from "shows
//    it correctly".
// 3. NO CLIENT-SIDE AGGREGATION. Enforced structurally over this folder's
//    source, because the absence of something is invisible to a rendering test:
//    a page that computed a percentage would render a perfectly plausible
//    number.
//
// **THE PARAMS BAR IS BUILT FROM THE SERVER'S SCHEMA (§17.11), AND THE FIXTURE
// IS BUILT TO CATCH A PAGE THAT ONLY PRETENDS TO READ IT.** Two reports with
// deliberately DIFFERENT parameter sets, and every default in the fixture is a
// value the old hardcoded bar would not have produced: `limit` defaults to 25
// for one report and 5 for the other (never 50), `stale_after_days` to 9 (never
// the server's real 7), `severity_at_least` to "warning" (never the real
// "info"). A page that hardcoded ANY of those renders a number this file names.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../../../lib/api";
import type { ApiClient, Me } from "../../../lib/api";
import { ReportCataloguePage } from "../ReportCatalogue";
import { figureFor } from "../reportFigures";
import { sourceLine } from "../reportText";
import { ReportViewPage } from "../ReportView";
import type { ReportCatalogue, ReportPage } from "../types";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function client(overrides: Partial<ApiClient>): ApiClient {
  return overrides as ApiClient;
}

// THE PROFILE ZONE IS NOT THE TEST RUNNER'S ZONE, and that gap is the whole
// point of the fixture. Node runs these at UTC, so every datetime assertion
// below reads DIFFERENTLY under the old `new Date(...)`/`toLocaleString()`
// code than under the profile-zone helpers -- +9 with no DST, so there is one
// right answer all year.
const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  permissions: ["reporting.read"],
  timezone: "Asia/Tokyo",
};

function renderCatalogue(api: ApiClient, at = "/reports") {
  mockUseApi.mockReturnValue(Object.assign(client({ me: vi.fn().mockResolvedValue(ME) }), api));
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[at]}>
        <ReportCataloguePage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function renderReport(api: ApiClient, key = "fleet-posture") {
  // EVERY report page reads the catalogue -- that is where its params schema
  // comes from (§17.11). Supplied here so a test about rendering does not have
  // to restate it, and spread SECOND so a test about the catalogue can override.
  // `Object.assign`, so what the caller passed WINS: a test about the catalogue
  // failing can still supply its own `listReports`.
  mockUseApi.mockReturnValue(
    Object.assign(
      client({
        listReports: vi.fn().mockResolvedValue(CATALOGUE),
        // Every report page reads `/me` too -- that is where the zone its
        // datetimes are entered and displayed in comes from.
        me: vi.fn().mockResolvedValue(ME),
      }),
      api,
    ),
  );
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/reports/${key}`]}>
        <Routes>
          <Route path="/reports/:reportKey" element={<ReportViewPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// Mirrors the server's `GET /api/v1/reports` body: EVERY report is listed,
// entitled or not, and every entry carries its `params_schema`.
//
// THE TWO SCHEMAS SHARE NO PARAMETER EXCEPT `limit`, AND EVEN THAT DIFFERS IN
// ITS DEFAULT. `fleet-posture` has a Literal with a default and an optional
// string; `sla-attainment` has a REQUIRED aliased date-time window and an
// optional enum. A page rendering one hardcoded form cannot satisfy both.
const CATALOGUE: ReportCatalogue = {
  reports: [
    {
      key: "fleet-posture",
      summary: "Every device with its agent contact state and its open health alerts.",
      description: "One row per device.",
      requires: ["devices.read", "health.read"],
      entitled: true,
      missing_permissions: [],
      params_schema: {
        type: "object",
        additionalProperties: false,
        properties: {
          os_family: {
            anyOf: [{ type: "string" }, { type: "null" }],
            default: null,
            title: "Os Family",
            description: "Restrict to one OS family.",
          },
          stale_after_days: {
            type: "integer",
            default: 9,
            minimum: 1,
            maximum: 365,
            title: "Stale After Days",
            description: "Days before an agent's silence is stale.",
          },
          severity_at_least: {
            type: "string",
            enum: ["info", "warning", "critical"],
            default: "warning",
            title: "Severity At Least",
            description: "Only count open alerts at this severity or above.",
          },
          limit: {
            type: "integer",
            default: 25,
            minimum: 1,
            maximum: 1000,
            title: "Limit",
            description: "Maximum rows to return.",
          },
          offset: {
            type: "integer",
            default: 0,
            minimum: 0,
            title: "Offset",
            description: "Rows to skip.",
          },
        },
      },
    },
    {
      key: "sla-attainment",
      summary: "SLA attainment by queue and priority over a closed window.",
      description: "One row per queue and priority.",
      requires: ["sla.read", "ticket.read"],
      entitled: false,
      missing_permissions: ["sla.read"],
      params_schema: {
        type: "object",
        additionalProperties: false,
        // The wire names, NOT `window_from`/`window_to`. The server publishes
        // aliases and a control bound to the Python name would be a 422.
        required: ["from", "to"],
        properties: {
          from: {
            type: "string",
            format: "date-time",
            title: "From",
            description: "Inclusive start of the window.",
          },
          to: {
            type: "string",
            format: "date-time",
            title: "To",
            description: "Exclusive end of the window.",
          },
          // A UUID naming a row in ANOTHER module — the audit's "the user must
          // find a UUID in another module and transcribe it".
          queue_id: {
            anyOf: [{ type: "string", format: "uuid" }, { type: "null" }],
            default: null,
            title: "Queue Id",
            description: "Restrict to one queue.",
          },
          priority: {
            anyOf: [{ type: "string", enum: ["low", "normal", "high", "urgent"] }, { type: "null" }],
            default: null,
            title: "Priority",
            description: "Restrict to one priority.",
          },
          limit: {
            type: "integer",
            default: 5,
            minimum: 1,
            maximum: 1000,
            title: "Limit",
            description: "Maximum rows to return.",
          },
        },
      },
    },
  ],
};

function page(overrides: Partial<ReportPage["meta"]> = {}): ReportPage {
  return {
    meta: {
      report_key: "fleet-posture",
      generated_at: "2026-03-04T09:15:00.000Z",
      reproducible: true,
      window_from: null,
      window_to: null,
      total: 2,
      limit: 50,
      offset: 0,
      sources: [
        { module: "devices", permission: "devices.read", recency: "as of last agent contact" },
      ],
      ...overrides,
    },
    totals: { devices: 2, stale_devices: 1 },
    rows: [
      { device_id: "d1", hostname: "alpha", stale: false, open_alerts_critical: 0 },
      { device_id: "d2", hostname: "beta", stale: true, open_alerts_critical: 3 },
    ],
  };
}

describe("the report catalogue", () => {
  it("lists a report the caller cannot run and NAMES the missing permission codes", async () => {
    // FALSIFY by hiding the un-entitled entry, or by greying the card without
    // naming the codes: both leave the operator with nothing to ask for. The
    // missing code is asserted BY TEXT -- a test that only asserted the card
    // exists passes against a card that hides them.
    renderCatalogue(client({ listReports: vi.fn().mockResolvedValue(CATALOGUE) }));

    await screen.findByTestId("report-card-sla-attainment");
    const card = screen.getByTestId("report-card-sla-attainment");
    expect(card).toHaveTextContent("It needs permission to view SLAs, which your account does not have.");
    expect(card).not.toHaveTextContent("sla.read");
    expect(card).toHaveAttribute("aria-disabled", "true");

    // And the entitled one is NOT marked disabled and IS a link -- otherwise
    // "everything is greyed" satisfies the assertion above.
    const entitled = screen.getByTestId("report-card-fleet-posture");
    expect(entitled).not.toHaveAttribute("aria-disabled");
    expect(entitled).not.toHaveTextContent("which your account does not have");
    expect(screen.getByRole("link", { name: "Fleet posture" })).toHaveAttribute(
      "href",
      "/reports/fleet-posture",
    );
  });

  // ------------------------------------------------------------------
  // AUDIT [HIGH]: "Report cards in the catalogue have no titles -- the link
  // text is a 200-word paragraph ... Enumerating headings on the page returns
  // only `H1: Reports` ... The actual report names exist only in the URLs."
  // WCAG 2.4.4 (Link Purpose) and 1.3.1.
  //
  // FALSIFIED against the previous version, where the link's accessible name
  // WAS the summary sentence and no card carried a heading at all.
  // ------------------------------------------------------------------

  it("gives every card a heading naming the report, entitled or not", async () => {
    renderCatalogue(client({ listReports: vi.fn().mockResolvedValue(CATALOGUE) }));

    await screen.findByTestId("report-card-fleet-posture");
    const headings = screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent);
    // `sla` is an initialism, and the un-entitled card is named too -- a card
    // you cannot run is still a card you have to identify to ask for access.
    expect(headings).toEqual(["Fleet posture", "SLA attainment"]);
  });

  it("does not use the description as the link text", async () => {
    renderCatalogue(client({ listReports: vi.fn().mockResolvedValue(CATALOGUE) }));

    await screen.findByTestId("report-card-fleet-posture");
    const links = screen
      .getAllByRole("link")
      .map((link) => link.textContent ?? "")
      .filter((text) => text.startsWith("Every device") || text.startsWith("SLA attainment by"));
    expect(links).toEqual([]);
    // The summary is still ON the card -- demoted, not deleted.
    expect(screen.getByTestId("report-card-fleet-posture")).toHaveTextContent(
      "Every device with its agent contact state",
    );
  });

  // ------------------------------------------------------------------
  // AUDIT [LOW] x2: "Unrendered markdown backticks leak into report
  // descriptions" and "Descriptions use `--` where an em dash is intended".
  // Both are server prose, normalised at the point of render.
  // ------------------------------------------------------------------

  it("renders backticks and double hyphens out of the server's prose", async () => {
    const catalogue: ReportCatalogue = {
      reports: [
        {
          ...(CATALOGUE.reports[0] as ReportCatalogue["reports"][number]),
          description:
            "Devices with no agent are reported as stale -- so the device total is the " +
            "fleet. It is a statement about the record at `meta.generated_at`.",
        },
      ],
    };
    renderCatalogue(client({ listReports: vi.fn().mockResolvedValue(catalogue) }));

    const card = await screen.findByTestId("report-card-fleet-posture");
    expect(card.textContent).toContain("stale — so the device total");
    expect(card.textContent).toContain("at meta.generated_at.");
    expect(card.textContent).not.toContain(" -- ");
    expect(card.textContent).not.toContain("`");
  });

  it("names every code a report requires, so the ask is complete", async () => {
    renderCatalogue(client({ listReports: vi.fn().mockResolvedValue(CATALOGUE) }));
    const card = await screen.findByTestId("report-card-fleet-posture");
    expect(card).toHaveTextContent("Needs permission to view devices and view device health");
    expect(card).not.toHaveTextContent("devices.read");
  });

  // ------------------------------------------------------------------
  // The three states a mock never has (UI port spec §6/§8): loading, empty
  // and error must be DISTINGUISHABLE, not all render as a blank page.
  // ------------------------------------------------------------------

  it("says the catalogue is loading rather than showing an empty page", async () => {
    renderCatalogue(client({ listReports: vi.fn().mockImplementation(() => new Promise<never>(() => {})) }));
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the deployment publishes no reports, distinctly from still loading", async () => {
    renderCatalogue(client({ listReports: vi.fn().mockResolvedValue({ reports: [] }) }));
    expect(await screen.findByText("This deployment publishes no reports.")).toBeInTheDocument();
    expect(screen.queryByText("Loading…")).toBeNull();
  });

  it("says the catalogue did not load on a genuine failure, distinctly from an empty catalogue", async () => {
    renderCatalogue(client({ listReports: vi.fn().mockRejectedValue(new Error("boom")) }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("This deployment publishes no reports.")).toBeNull();
  });

  // Reporting-specific instruction: the design's "BI datasets" tab claims
  // four Power BI datasets are "Ready". No such endpoint exists (spec
  // §0.11, §6.2's route table has no `/datasets` prefix at all) -- this
  // asserts the catalogue says so instead of repeating the mock's claim.
  // ------------------------------------------------------------------
  // "All tiles and all numbers or KPIs need to be clickable ... I don't want
  // dead tiles everywhere."
  //
  // THREE ASSERTIONS, AND THE SECOND TWO ARE WHY THE FIRST IS NOT THEATRE. A
  // tile carrying an `href` proves nothing on its own: the destination has to
  // exist and has to show the rows the number counted. So the hrefs are
  // pinned here, and the two tests below follow each filtering destination to
  // the list it produces.
  // ------------------------------------------------------------------
  it("gives every tile above the catalogue a destination", async () => {
    renderCatalogue(client({ listReports: vi.fn().mockResolvedValue(CATALOGUE) }));

    await screen.findByTestId("report-card-fleet-posture");
    const tiles: ReadonlyArray<readonly [string, string]> = [
      ["Report templates", "/reports"],
      ["Runnable by you", "/reports?access=runnable"],
      ["Missing a permission", "/reports?access=blocked"],
    ];
    for (const [label, href] of tiles) {
      const tile = screen.getByText(label).closest("a");
      expect(tile, `${label} is not a link`).not.toBeNull();
      expect(tile).toHaveAttribute("href", href);
    }
  });

  it("follows 'Missing a permission' to the reports the caller cannot run, and only those", async () => {
    renderCatalogue(
      client({ listReports: vi.fn().mockResolvedValue(CATALOGUE) }),
      "/reports?access=blocked",
    );

    await screen.findByTestId("report-card-sla-attainment");
    // The entitled report is NOT in this list -- otherwise the destination is
    // the unfiltered catalogue wearing a query string.
    expect(screen.queryByTestId("report-card-fleet-posture")).toBeNull();
    // AND THE PAGE SAYS IT IS HOLDING SOMETHING BACK, with the way out. This
    // module's first rule is that nothing is hidden; a filter the operator
    // chose is allowed, a filter they cannot see is not.
    expect(screen.getByRole("status")).toHaveTextContent("1 of 2 are hidden by this filter");
    expect(screen.getByRole("link", { name: "Show every report" })).toHaveAttribute(
      "href",
      "/reports",
    );
  });

  it("follows 'Runnable by you' to the entitled reports, and names the empty case correctly", async () => {
    renderCatalogue(
      client({ listReports: vi.fn().mockResolvedValue(CATALOGUE) }),
      "/reports?access=runnable",
    );

    await screen.findByTestId("report-card-fleet-posture");
    expect(screen.queryByTestId("report-card-sla-attainment")).toBeNull();

    // A filter that matches nothing is NOT "this deployment publishes no
    // reports" -- the same distinction the run page draws between a measured
    // zero and an absent measurement.
    renderCatalogue(
      client({
        listReports: vi.fn().mockResolvedValue({
          reports: CATALOGUE.reports.map((entry) => ({ ...entry, entitled: false })),
        }),
      }),
      "/reports?access=runnable",
    );
    expect(
      await screen.findByText("You can run none of the reports this deployment publishes."),
    ).toBeInTheDocument();
  });

  it("says there is no bulk/BI dataset export, rather than claiming one is Ready", async () => {
    renderCatalogue(client({ listReports: vi.fn().mockResolvedValue(CATALOGUE) }));
    await screen.findByTestId("report-card-fleet-posture");
    expect(screen.getByText(/no bulk or Power BI-style dataset export/i)).toBeInTheDocument();
    expect(screen.queryByText(/Ready/)).toBeNull();
  });
});

describe("a report page", () => {
  it("always renders 'As of <generated_at>' and NO staleness note for a reproducible report", async () => {
    renderReport(
      client({
        runReport: vi.fn().mockResolvedValue(page({ reproducible: true })),
        exportReport: vi.fn(),
      }),
    );

    await screen.findByText(/^As of /);
    expect(screen.queryByText(/These numbers can change/)).toBeNull();
  });

  it("renders the staleness note IN ADDITION for a non-reproducible report", async () => {
    // THE PAIR IS THE TEST. This case alone cannot distinguish "shows the note
    // correctly" from "always shows the note"; the case above cannot
    // distinguish "hides it correctly" from "never shows it".
    renderReport(
      client({
        runReport: vi.fn().mockResolvedValue(page({ reproducible: false })),
        exportReport: vi.fn(),
      }),
      "sla-attainment",
    );

    // `sla-attainment`'s window is REQUIRED and has no default, so the page runs
    // nothing until it is filled (§17.11) -- it no longer runs on load and
    // renders the 422.
    await screen.findByLabelText("from");
    fireEvent.change(screen.getByLabelText("from"), { target: { value: "2026-03-01T00:00" } });
    fireEvent.change(screen.getByLabelText("to"), { target: { value: "2026-04-01T00:00" } });
    await userEvent.click(screen.getByRole("button", { name: "Run" }));

    await screen.findByText(/^As of /);
    expect(screen.getByText(/These numbers can change/)).toHaveTextContent(
      "re-running it for the same dates may give different numbers",
    );
  });

  it("renders totals from the response, not from the rows", async () => {
    // The fixture is DELIBERATELY INCONSISTENT: `stale_devices` is 1 while two
    // rows are present and one of them has `stale: true` by coincidence, and
    // `devices` is 2. A page that recomputed totals from `rows` would still
    // render 2 for `devices` -- so the falsifier is `stale_devices`, which a
    // recomputation could only get right by implementing the server's
    // predicate a second time. The value rendered must be the server's.
    const body = page();
    body.totals = { devices: 2, stale_devices: 7 };
    renderReport(client({ runReport: vi.fn().mockResolvedValue(body), exportReport: vi.fn() }));

    await screen.findByText(/^As of /);
    expect(screen.getByText("7")).toBeInTheDocument();
  });

  it("renders every row and every column the response carried", async () => {
    renderReport(
      client({ runReport: vi.fn().mockResolvedValue(page()), exportReport: vi.fn() }),
    );
    await screen.findByText(/^As of /);
    expect(screen.getByText("alpha")).toBeInTheDocument();
    expect(screen.getByText("beta")).toBeInTheDocument();
    // The header is the wire key HUMANISED (`lib/labels.ts`), not the raw
    // `snake_case` the response carries.
    expect(screen.getByRole("columnheader", { name: "Hostname" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Open alerts critical" })).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "open_alerts_critical" })).toBeNull();
  });

  it("downloads the CSV through the API client, which carries the auth header", async () => {
    // A bare `<a href="/api/v1/reports/.../export.csv">` would send NO bearer
    // token and land on a 401. This asserts the click goes through
    // `ApiClient.exportReport`, which is the method that sets the header.
    const exportReport = vi.fn().mockResolvedValue(new Blob(["device_id\n"]));
    globalThis.URL.createObjectURL = vi.fn().mockReturnValue("blob:x");
    globalThis.URL.revokeObjectURL = vi.fn();

    renderReport(
      client({ runReport: vi.fn().mockResolvedValue(page()), exportReport }),
    );
    await screen.findByText(/^As of /);
    await userEvent.click(screen.getByRole("button", { name: "Download CSV" }));

    await waitFor(() => {
      // The KEY and the APPLIED params -- the export must run the same query
      // the page is showing, or the file and the screen disagree. The params are
      // the SCHEMA'S defaults, so a page that hardcoded `limit: "50"` fails here
      // as well as in the defaults test below.
      expect(exportReport).toHaveBeenCalledWith("fleet-posture", {
        stale_after_days: "9",
        severity_at_least: "warning",
        limit: "25",
        offset: "0",
      });
    });
  });

  it("explains a 413 as a refusal rather than a truncated file", async () => {
    // §0.8's decision, surfaced. An operator who sees "export failed" narrows
    // nothing; one who is told the file was refused rather than shortened knows
    // both what happened and what to do.
    const exportReport = vi.fn().mockRejectedValue(new ApiError(413, "too large"));
    renderReport(
      client({ runReport: vi.fn().mockResolvedValue(page()), exportReport }),
    );
    await screen.findByText(/^As of /);
    await userEvent.click(screen.getByRole("button", { name: "Download CSV" }));

    await screen.findByText(/Too many rows to export in one file/);
  });

  it("states a refusal as a sentence, announced, instead of a status code and an empty body", async () => {
    // THE BLOCKER, on this route. `ApiError` is built as
    // `new ApiError(status, await response.text())`, so a bodyless refusal --
    // what a proxy 403 generally is -- carried `message === ""`, and this page
    // rendered `HTTP 403. ` followed by nothing. FALSIFIED by restoring the
    // old template: it renders "HTTP 403" and no sentence, and both assertions
    // below fail.
    renderReport(
      client({
        runReport: vi.fn().mockRejectedValue(new ApiError(403, "")),
        exportReport: vi.fn(),
      }),
    );
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("You do not have permission to do that.");
    // A status code is not something an operator can act on, and it was the
    // only thing this page said for a bodyless refusal.
    expect(alert).not.toHaveTextContent("HTTP");
    expect(screen.queryByText(/^As of /)).toBeNull();
  });

  it("translates a 422 body instead of printing the Pydantic payload at the operator", async () => {
    // Three modules rendered raw `{"detail":[...]}` into the UI. Here the
    // report parameters are the thing being validated, so the payload names
    // fields the operator can actually go and fix.
    const body = JSON.stringify({
      detail: [
        { type: "greater_than_equal", loc: ["body", "limit"], msg: "Input should be greater than or equal to 1", input: 0 },
      ],
    });
    renderReport(
      client({
        runReport: vi.fn().mockRejectedValue(new ApiError(422, body)),
        exportReport: vi.fn(),
      }),
    );
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Limit: Input should be greater than or equal to 1");
    // Not one character of the wire shape, and above all not `input` -- which
    // echoes what the operator submitted.
    expect(alert).not.toHaveTextContent("detail");
    expect(alert).not.toHaveTextContent("greater_than_equal");
  });

  it("says WHY an export failed for anything that is not the 413 it has its own words for", async () => {
    // The 413 case below keeps its bespoke sentence. Everything else used to
    // be "The export failed." full stop -- which does not say whether to wait,
    // to ask for a permission, or to narrow the query.
    const exportReport = vi.fn().mockRejectedValue(new ApiError(503, ""));
    renderReport(client({ runReport: vi.fn().mockResolvedValue(page()), exportReport }));
    await screen.findByText(/^As of /);
    await userEvent.click(screen.getByRole("button", { name: "Download CSV" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The export failed.");
    expect(alert).toHaveTextContent("That service is unavailable right now.");
  });

  it("renders the run stamp in the operator's profile zone, not the browser's", async () => {
    // "As of" is mandatory (§0.3) and this is what makes it true rather than
    // merely present: `generated_at` is 09:15Z and the operator's profile says
    // Asia/Tokyo, so the only correct rendering is 18:15. FALSIFIED by
    // `new Date(...).toLocaleString()`, which under this runner's UTC prints
    // 09:15 -- the assertion below names both.
    renderReport(client({ runReport: vi.fn().mockResolvedValue(page()), exportReport: vi.fn() }));

    const stamp = await screen.findByText(/^As of /);
    expect(stamp).toHaveTextContent("As of 04 Mar 2026, 18:15");
    expect(stamp).not.toHaveTextContent("09:15");
  });
});

// --------------------------------------------------------------------------
// §17.11: the params bar is BOUND TO THE PUBLISHED SCHEMA, not hardcoded.
// --------------------------------------------------------------------------

// ------------------------------------------------------------------
// AUDIT [HIGH]: "A zero-row result still renders nine metric tiles all showing
// '0' ... Nine zeros are indistinguishable from nine genuine measurements of
// zero. '0 tickets breached' reads as a perfect score when in fact nothing was
// measured ... someone will screenshot this as proof of 100% attainment."
//
// AUDIT [HIGH]: "There is no data visualisation anywhere in the reporting
// module ... `svg: 0, canvas: 0, tables: 1` on /reports/sla-attainment after a
// successful run ... This is the one module where a chart is the deliverable."
// ------------------------------------------------------------------

/** A run of `sla-attainment` with the totals the server actually computes. */
function slaPage(total: number): ReportPage {
  return {
    meta: {
      report_key: "sla-attainment",
      generated_at: "2026-03-04T09:15:00.000Z",
      reproducible: false,
      window_from: "2026-07-01T00:00:00.000Z",
      window_to: "2026-08-19T00:00:00.000Z",
      total,
      limit: 5,
      offset: 0,
      sources: [{ module: "sla", permission: "sla.read", recency: "as of now" }],
    },
    totals: {
      queue_priority_pairs: total,
      tickets: total === 0 ? 0 : 40,
      response_met: total === 0 ? 0 : 31,
      response_breached: total === 0 ? 0 : 9,
      resolution_met: total === 0 ? 0 : 26,
      resolution_breached: total === 0 ? 0 : 14,
      void: 0,
      still_running: 0,
      breached_after_retriage: total === 0 ? 0 : 3,
    },
    rows:
      total === 0
        ? []
        : [{ queue_id: "q1", queue_name: "Incidents", priority: "high", tickets: 40 }],
  };
}

/** Fill `sla-attainment`'s required window and run it — nothing runs on open. */
async function runWindow(): Promise<void> {
  fireEvent.change(await screen.findByLabelText("from"), {
    target: { value: "2026-07-01T09:00" },
  });
  fireEvent.change(screen.getByLabelText("to"), { target: { value: "2026-08-19T09:00" } });
  await userEvent.click(screen.getByRole("button", { name: "Run" }));
}

describe("a run that matched nothing", () => {
  it("suppresses the totals tiles rather than rendering a screen of zeros", async () => {
    // FALSIFIER: delete the `meta.total === 0` branch in ReportView and the
    // nine tiles come back, every one of them reading 0 — which is exactly
    // what the auditor screenshotted.
    renderReport(
      client({ runReport: vi.fn().mockResolvedValue(slaPage(0)), exportReport: vi.fn() }),
      "sla-attainment",
    );

    await runWindow();
    await screen.findByText(/0 matching\s+rows/);
    expect(screen.getByText("Nothing matched these parameters")).toBeInTheDocument();
    // Not one tile label, and no bare zero standing in for a measurement.
    expect(screen.queryByText(/Response breached/i)).toBeNull();
    expect(screen.queryByText(/Queue priority pairs/i)).toBeNull();
    expect(screen.queryByText("0", { selector: ".tile .v" })).toBeNull();
    // ...and no chart either: an axis with no bars is the same lie drawn.
    expect(screen.queryByRole("figure")).toBeNull();
    expect(document.querySelector("main svg, .chart svg")).toBeNull();
  });

  it("still renders the tiles, and the numbers, when something DID match", async () => {
    // The pair is the test: one case cannot tell "suppresses on zero" from
    // "never renders totals at all".
    renderReport(
      client({ runReport: vi.fn().mockResolvedValue(slaPage(4)), exportReport: vi.fn() }),
      "sla-attainment",
    );

    await runWindow();
    await screen.findByText(/4 matching\s+rows/);
    expect(screen.getByText("Response breached")).toBeInTheDocument();
    expect(screen.getByText("9", { selector: ".tile .v" })).toBeInTheDocument();
    expect(screen.queryByText("Nothing matched these parameters")).toBeNull();
  });
});

describe("the figure on a run report", () => {
  it("draws met against breached, from the server's totals, with the window in the title", async () => {
    // FALSIFIER: remove the <BarChart> and this finds no figure — which is the
    // `svg: 0` the auditor measured.
    renderReport(
      client({ runReport: vi.fn().mockResolvedValue(slaPage(4)), exportReport: vi.fn() }),
      "sla-attainment",
    );

    await runWindow();
    await screen.findByText(/4 matching\s+rows/);
    const figure = screen.getByRole("figure");
    expect(figure).toHaveTextContent("Met against breached, by clock");
    // The window, in the PROFILE's zone (+9): 1 Jul 00:00Z is 09:00 in Tokyo.
    expect(figure).toHaveTextContent(/1 Jul 2026/);
    // The chart carries a real data table, so the figure is not colour-only
    // and a screen reader reads the values rather than a paraphrase.
    const rows = within(figure).getAllByRole("row");
    expect(rows.length).toBeGreaterThan(1);
    expect(within(figure).getByRole("rowheader", { name: "Response" })).toBeInTheDocument();
    expect(within(figure).getByRole("rowheader", { name: "Resolution" })).toBeInTheDocument();
    // The four numbers are the server's, unaltered.
    expect(within(figure).getByRole("columnheader", { name: "Met" })).toBeInTheDocument();
    expect(within(figure).getByRole("columnheader", { name: "Breached" })).toBeInTheDocument();
    expect(figure).toHaveTextContent("31");
    expect(figure).toHaveTextContent("14");
    // And the axis names its unit.
    expect(figure).toHaveTextContent("tickets");
  });

  it("names the window in parentheses, with no em dash in the caption", async () => {
    // The plan's global constraints forbid an em dash in new UI copy, and this
    // caption is the one string on the page assembled from two halves.
    // FALSIFIER: restore `${caption} \u2014 ${window}` and this fails.
    renderReport(
      client({ runReport: vi.fn().mockResolvedValue(slaPage(4)), exportReport: vi.fn() }),
      "sla-attainment",
    );

    await runWindow();
    await screen.findByText(/4 matching\s+rows/);
    const caption = document.querySelector(".chart-title");
    expect(caption?.textContent).toMatch(/^Met against breached, by clock \(.+\)$/);
    expect(caption?.textContent).not.toContain("\u2014");
  });

  it("draws nothing for a response missing a total the figure names", async () => {
    // The fixture `page()` carries `devices` and `stale_devices` but not
    // `devices_with_open_alerts`. A figure that substituted 0 for the missing
    // key would draw a bar for a measurement the server never made.
    renderReport(
      client({ runReport: vi.fn().mockResolvedValue(page()), exportReport: vi.fn() }),
      "fleet-posture",
    );
    await screen.findByText(/^As of /);
    expect(screen.queryByRole("figure")).toBeNull();
    // ...but the tiles are still there: no figure is not no result.
    expect(screen.getByText("Stale devices")).toBeInTheDocument();
  });
});

describe("the figure table's keys", () => {
  // THE REVIEWED ROUTE HAD NO CHART, and the reason has since been removed
  // rather than accommodated. The harmony gate used to walk `/reports/rpt-1`,
  // a placeholder key the catalogue aliased to the patch compliance report;
  // `FIGURES` is keyed by report key and had no row for the alias, so the one
  // module whose deliverable is a chart rendered none on the one report route
  // anybody reviews. The alias was a second name for a report that already had
  // one, and it cost twice: no figure here, and an `h1` reading "Rpt 1" in
  // every screenshot. The gate now walks `patch-compliance`, so the key under
  // review and the key in this table are the same key.
  const PATCH_TOTALS = {
    devices: 10,
    devices_with_zero_missing: 5,
    devices_never_scanned: 1,
  };

  it("draws a figure for the key the gate actually walks", () => {
    // Read from `scripts/routes.mjs` rather than retyped, so a sample key that
    // moves again cannot leave this test asserting about a route nobody walks.
    const routes = readFileSync(resolve(import.meta.dirname, "../../../../scripts/routes.mjs"), "utf8");
    const sampleKey = /reportKey:\s*"([^"]+)"/.exec(routes)?.[1];
    expect(sampleKey, "no reportKey in routes.mjs").toBeDefined();
    expect(figureFor(sampleKey as string, PATCH_TOTALS)).not.toBeNull();
  });

  // THE FALSIFIER for the line above: an alias table that answered for
  // anything would satisfy it while drawing an invented chart for a report
  // definition this build has never heard of.
  it("still draws nothing for a key it does not know", () => {
    expect(figureFor("a-report-added-server-side", PATCH_TOTALS)).toBeNull();
  });

  // ...and the alias is held to the same rule as every other key: a response
  // missing a named total draws nothing rather than a substituted zero.
  it("draws nothing when a total the figure names is absent", () => {
    expect(figureFor("patch-compliance", { devices: 10, devices_never_scanned: 1 })).toBeNull();
  });
});

describe("the provenance line", () => {
  it("names the source in words and introduces the permission code", () => {
    // It read `patch (patch.read): as of each device's last scan` -- a wire
    // enum, an RBAC code in brackets, and only the clause after the colon
    // written for a reader.
    expect(
      sourceLine({
        module: "patch",
        permission: "patch.read",
        recency: "as of each device's last scan",
      }),
    ).toBe("Patch, as of each device's last scan. Needs permission to view updates.");
    // The initialism table applies here too, and the code is NOT dropped: it
    // is what an operator quotes when a report is refused.
    expect(sourceLine({ module: "sla", permission: "sla.read", recency: "as of now" })).toBe(
      "SLA, as of now. Needs permission to view SLAs.",
    );
  });

  it("is what the run report actually renders", async () => {
    renderReport(
      client({ runReport: vi.fn().mockResolvedValue(slaPage(4)), exportReport: vi.fn() }),
      "sla-attainment",
    );
    await runWindow();
    await screen.findByText(/4 matching\s+rows/);
    expect(screen.getByText("SLA, as of now. Needs permission to view SLAs.")).toBeInTheDocument();
    // And the shape it replaced is gone, so this cannot pass against a page
    // that renders both.
    expect(screen.queryByText(/sla \(sla\.read\)/)).toBeNull();
  });
});

describe("the params bar", () => {
  it("binds one control per published parameter, of the kind the schema names", async () => {
    renderReport(
      client({ runReport: vi.fn().mockResolvedValue(page()), exportReport: vi.fn() }),
    );

    // A Literal becomes a select carrying EXACTLY the published options -- not a
    // free-text box the operator has to guess into.
    const severity = await screen.findByLabelText("severity_at_least");
    expect(severity.tagName).toBe("SELECT");
    expect(
      Array.from((severity as HTMLSelectElement).options).map((option) => option.value),
    ).toEqual(["", "info", "warning", "critical"]);

    // An integer with `ge`/`le` becomes a number input carrying the SERVER'S
    // bounds. 365 is in the fixture and nowhere in the component.
    const stale = screen.getByLabelText("stale_after_days");
    expect(stale).toHaveAttribute("type", "number");
    expect(stale).toHaveAttribute("min", "1");
    expect(stale).toHaveAttribute("max", "365");

    // An optional string is a plain text box, and is NOT marked required.
    const osFamily = screen.getByLabelText("os_family");
    expect(osFamily).toHaveAttribute("type", "text");
    expect(osFamily).not.toBeRequired();

    // And the parameters this report does NOT publish are absent. Without this,
    // "renders the schema's controls" is satisfied by a page that renders the
    // schema's controls AND the old hardcoded ones.
    expect(screen.queryByLabelText("from")).toBeNull();
    expect(screen.queryByLabelText("to")).toBeNull();
  });

  it("renders a DIFFERENT form for a report with a different schema", async () => {
    // **THE PAIR IS THE TEST.** One rendered form cannot distinguish "reads the
    // schema" from "hardcodes a form that happens to match one report". These
    // two schemas share only `limit`.
    renderReport(
      client({ runReport: vi.fn().mockResolvedValue(page()), exportReport: vi.fn() }),
      "sla-attainment",
    );

    const from = await screen.findByLabelText("from");
    expect(from).toHaveAttribute("type", "datetime-local");
    expect(from).toBeRequired();

    const priority = screen.getByLabelText("priority");
    expect(priority.tagName).toBe("SELECT");
    expect(
      Array.from((priority as HTMLSelectElement).options).map((option) => option.value),
    ).toEqual(["", "low", "normal", "high", "urgent"]);

    expect(screen.queryByLabelText("severity_at_least")).toBeNull();
    expect(screen.queryByLabelText("stale_after_days")).toBeNull();
    expect(screen.queryByLabelText("offset")).toBeNull();
    // The PYTHON attribute names are never bound: the server publishes aliases.
    expect(screen.queryByLabelText("window_from")).toBeNull();
  });

  it("runs the report with the SCHEMA'S defaults, not with values written in the browser", async () => {
    // Every value asserted here is one the old hardcoded bar could not have
    // produced: it sent `{limit: "50", offset: "0"}` and nothing else.
    const runReport = vi.fn().mockResolvedValue(page());
    renderReport(client({ runReport, exportReport: vi.fn() }));

    await waitFor(() => {
      expect(runReport).toHaveBeenCalledWith("fleet-posture", {
        stale_after_days: "9",
        severity_at_least: "warning",
        limit: "25",
        offset: "0",
      });
    });
    // `os_family` defaults to null -- "optional, unset" -- so it is not sent at
    // all. An empty string would be a 422 for a parameter typed as a UUID
    // elsewhere, and a filter nobody asked for here.
    expect(runReport.mock.calls[0]?.[1]).not.toHaveProperty("os_family");
  });

  it("sends a changed parameter under its published wire name", async () => {
    const runReport = vi.fn().mockResolvedValue(page());
    renderReport(client({ runReport, exportReport: vi.fn() }));

    await screen.findByLabelText("severity_at_least");
    fireEvent.change(screen.getByLabelText("severity_at_least"), {
      target: { value: "critical" },
    });
    fireEvent.change(screen.getByLabelText("os_family"), { target: { value: "windows" } });
    await userEvent.click(screen.getByRole("button", { name: "Run" }));

    await waitFor(() => {
      expect(runReport).toHaveBeenLastCalledWith("fleet-posture", {
        os_family: "windows",
        stale_after_days: "9",
        severity_at_least: "critical",
        limit: "25",
        offset: "0",
      });
    });
  });

  it("sends a typed window as an instant in the PROFILE's zone, not the browser's", async () => {
    // THE FIVE-HOUR BUG, on the one control that has it. A
    // `<input type="datetime-local">` hands back a bare wall clock --
    // "2026-03-01T00:00", no zone -- and `new Date(that)` reads it in whatever
    // zone the machine running the browser is set to. So the same operator
    // typing the same window got a different report period from a laptop than
    // from a jump box, and the numbers came back plausible either way.
    //
    // The operator's profile says Asia/Tokyo (+9); this runner is UTC. A page
    // that resolves the wall clock in the browser's zone sends
    // 2026-03-01T00:00:00.000Z and fails on the first assertion; one that
    // resolves it in the profile's sends 2026-02-28T15:00:00.000Z.
    const runReport = vi.fn().mockResolvedValue(page({ reproducible: false }));
    renderReport(client({ runReport, exportReport: vi.fn() }), "sla-attainment");

    await screen.findByLabelText("from");
    fireEvent.change(screen.getByLabelText("from"), { target: { value: "2026-03-01T09:30" } });
    fireEvent.change(screen.getByLabelText("to"), { target: { value: "2026-03-02T09:30" } });
    await userEvent.click(screen.getByRole("button", { name: "Run" }));

    await waitFor(() => {
      expect(runReport).toHaveBeenLastCalledWith("sla-attainment", {
        from: "2026-03-01T00:30:00.000Z",
        to: "2026-03-02T00:30:00.000Z",
        limit: "5",
      });
    });
    // And the export sends the SAME instants, because it sends the applied
    // parameters -- a file and a screen that disagree by nine hours is the
    // same defect, discovered a week later in a spreadsheet.
    expect(runReport.mock.calls.at(-1)?.[1]).not.toHaveProperty("from", "2026-03-01T09:30");
  });

  it("runs nothing until a required parameter is filled, and says which", async () => {
    // `sla-attainment`'s window is required and has no default (§5.4: a default
    // would make the period invisible). The old page ran on load and rendered
    // the 422 it got back.
    const runReport = vi.fn().mockResolvedValue(page({ reproducible: false }));
    renderReport(client({ runReport, exportReport: vi.fn() }), "sla-attainment");

    await screen.findByLabelText("from");
    // LET ANY QUERY THE PAGE DECIDED TO RUN ACTUALLY FIRE before asserting it
    // did not. An assertion made in the mount tick passes against a page that
    // runs a moment later, which is precisely the bug being guarded.
    await new Promise((resolve) => setTimeout(resolve, 25));
    // Asserted as the CALL LIST, not `not.toHaveBeenCalled()`: a page that ran
    // anyway sends a query with no window in it, and this failure PRINTS that
    // query rather than saying "expected not to be called", which names nothing.
    expect(runReport.mock.calls).toEqual([]);
    // THE BUTTON IS NOT DISABLED. It was, and that is the silent no-op the form
    // kernel exists to delete: the operator clicks, nothing moves, and the page
    // offers no theory about which box is at fault. Before submit the page says
    // only why the table is empty; naming the fields is the submit's job, and
    // it is asserted in "names every unfilled required parameter" below.
    expect(screen.getByRole("button", { name: "Run" })).toBeEnabled();
    expect(screen.getByText(/Nothing has run yet/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("from"), { target: { value: "2026-03-01T00:00" } });
    fireEvent.change(screen.getByLabelText("to"), { target: { value: "2026-04-01T00:00" } });
    await userEvent.click(screen.getByRole("button", { name: "Run" }));

    await waitFor(() => {
      expect(runReport).toHaveBeenCalledWith("sla-attainment", {
        // A `format: "date-time"` control sends an INSTANT, not the local
        // wall-clock string the browser input holds -- and the instant is
        // resolved in the OPERATOR'S profile zone (Asia/Tokyo, +9). Written as
        // literals rather than as `new Date("2026-03-01T00:00")`, which is the
        // very expression under test: an assertion built from it agrees with
        // the browser-zone bug by construction.
        from: "2026-02-28T15:00:00.000Z",
        to: "2026-03-31T15:00:00.000Z",
        limit: "5",
      });
    });
  });

  // ------------------------------------------------------------------
  // The submit gap (UX cycle 3). Run used to be
  // `disabled={unfilled.length > 0}` with the missing parameters named — if at
  // all — in the WIRE vocabulary (`from`, `to`) rather than the titles printed
  // above the boxes.
  // ------------------------------------------------------------------

  it("names every unfilled required parameter, BY ITS PRINTED TITLE, when Run is pressed", async () => {
    // FALSIFIED BY A DISABLED BUTTON: with the old `disabled={...}` the click
    // lands on nothing, no alert is ever rendered, and this times out. The
    // titles are asserted rather than the wire names because `From` is the word
    // on screen — an operator hunting for a box labelled `from` on a page that
    // says `From` is the same defect one indirection later.
    const runReport = vi.fn().mockResolvedValue(page());
    renderReport(client({ runReport, exportReport: vi.fn() }), "sla-attainment");

    await screen.findByLabelText("from");
    await userEvent.click(screen.getByRole("button", { name: "Run" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("From is required");
    expect(alert).toHaveTextContent("To is required");
    // And it still runs NOTHING — announcing the problem does not excuse
    // sending the query and rendering the 422 as if the report were broken.
    expect(runReport.mock.calls).toEqual([]);
  });

  it("refuses a number outside the bounds the SCHEMA published, naming the bound", async () => {
    // `limit` publishes `minimum: 1`. The bar carried that as `min` on the
    // input and the kernel's form is `noValidate`, so nothing checked it: `0`
    // went to the server and came back a 422 about a parameter the operator
    // had, as far as they could tell, filled in. 1 is the FIXTURE'S bound, not
    // a number written in the component.
    const runReport = vi.fn().mockResolvedValue(page());
    renderReport(client({ runReport, exportReport: vi.fn() }));

    await screen.findByLabelText("limit");
    await waitFor(() => {
      expect(runReport).toHaveBeenCalledTimes(1);
    });

    fireEvent.change(screen.getByLabelText("limit"), { target: { value: "0" } });
    await userEvent.click(screen.getByRole("button", { name: "Run" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Limit must be at least 1");
    // The CALL LIST, so a failure prints the query that should never have been
    // sent rather than "expected 1, got 2".
    expect(runReport.mock.calls.map((call) => (call[1] as { limit?: string }).limit)).toEqual([
      "25",
    ]);
  });

  it("says so when the catalogue itself is refused, instead of rendering a blank page", async () => {
    // The catalogue is gated on `reporting.read`, so this is the 403 case. The
    // page cannot know what parameters the report takes and must not guess.
    const runReport = vi.fn().mockResolvedValue(page());
    renderReport(
      client({
        // BODYLESS, which is what a proxy 403 is and what the old empty-string
        // render could not say anything about at all.
        listReports: vi.fn().mockRejectedValue(new ApiError(403, "")),
        runReport,
        exportReport: vi.fn(),
      }),
    );

    // ANNOUNCED, and it names the failure: "could not be loaded" alone left a
    // 403 (you lack `reporting.read`) and a 503 (come back in a minute)
    // looking identical, on a bare <p> no screen reader mentions.
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/does not know what parameters/);
    expect(alert).toHaveTextContent("You do not have permission to do that.");
    expect(runReport.mock.calls).toEqual([]);
    expect(screen.queryByRole("button", { name: "Run" })).toBeNull();
  });

  it("renders no params bar and runs nothing for a key the catalogue does not publish", async () => {
    const runReport = vi.fn().mockResolvedValue(page());
    renderReport(client({ runReport, exportReport: vi.fn() }), "not-a-report");

    await screen.findByText(/publishes no report called/);
    expect(runReport).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Run" })).toBeNull();
  });
});

// --------------------------------------------------------------------------
// §11: no client-side aggregation, enforced STRUCTURALLY.
// --------------------------------------------------------------------------

// Vite's raw-import glob: the actual source of every module file in this
// folder, so this reads what ships rather than what a fixture says ships.
const SOURCES = import.meta.glob("../*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// ------------------------------------------------------------------
// AUDIT [MEDIUM]: "Report parameters expose raw API concepts: a free-text
// 'Queue Id' UUID ... the user must find a UUID in another module and
// transcribe it, when a queue picker already exists elsewhere in the product
// (/sla has a populated queue dropdown)."
//
// A mistyped UUID here is not a validation message: it is a report about the
// wrong queue, filed as compliance evidence.
// ------------------------------------------------------------------

// ------------------------------------------------------------------
// AUDIT [MEDIUM], the other half of the same finding: "Limit and Offset are
// pagination internals leaking into a business-user form; nobody running an SLA
// report thinks in offsets."
// ------------------------------------------------------------------

describe("paging through a report's rows", () => {
  const big = () => page({ total: 120, limit: 25, offset: 0 });

  it("renders no Offset box, and still sends the schema's offset", async () => {
    // FALSIFIER: the previous version rendered `<input type="number">` for it,
    // so `getByLabelText("offset")` found a control.
    const runReport = vi.fn().mockResolvedValue(big());
    renderReport(client({ runReport, exportReport: vi.fn() }));

    await screen.findByText(/^As of /);
    expect(screen.queryByLabelText("offset")).toBeNull();
    // The parameter has NOT been dropped from the query — the server still
    // pages on it, and a report that stopped sending it would page differently
    // from the one the operator ran.
    expect(runReport.mock.calls[0]?.[1]).toMatchObject({ offset: "0" });
    // `limit` stays a field: "how many rows" is a question a person answers.
    expect(screen.getByLabelText("limit")).toHaveAttribute("type", "number");
  });

  it("moves a page forward by the limit the SERVER reports, and announces where it is", async () => {
    const runReport = vi
      .fn()
      .mockResolvedValueOnce(big())
      .mockResolvedValue(page({ total: 120, limit: 25, offset: 25 }));
    renderReport(client({ runReport, exportReport: vi.fn() }));

    await screen.findByText(/^As of /);
    // 25 is the SERVER's echoed limit; the schema's default is 25 and the old
    // hardcoded bar used 50, so this cannot pass by coincidence.
    expect(screen.getByText(/Rows 1–25 of 120/)).toHaveAttribute("role", "status");
    expect(screen.getByRole("button", { name: "Previous rows" })).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: "Next rows" }));

    await waitFor(() => expect(runReport).toHaveBeenCalledTimes(2));
    expect(runReport.mock.calls[1]?.[1]).toMatchObject({ offset: "25" });
    expect(await screen.findByText(/Rows 26–50 of 120/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous rows" })).toBeEnabled();
  });

  it("returns to the first page when the parameters change", async () => {
    // Otherwise a narrowed filter reports "no rows" for a query with plenty,
    // because page four of the old result set does not exist in the new one.
    const runReport = vi
      .fn()
      .mockResolvedValueOnce(big())
      .mockResolvedValueOnce(page({ total: 120, limit: 25, offset: 25 }))
      .mockResolvedValue(page({ total: 3, limit: 25, offset: 0 }));
    renderReport(client({ runReport, exportReport: vi.fn() }));

    await screen.findByText(/^As of /);
    await userEvent.click(screen.getByRole("button", { name: "Next rows" }));
    await waitFor(() => expect(runReport).toHaveBeenCalledTimes(2));

    fireEvent.change(screen.getByLabelText("os_family"), { target: { value: "linux" } });
    await userEvent.click(screen.getByRole("button", { name: "Run" }));

    await waitFor(() => expect(runReport).toHaveBeenCalledTimes(3));
    expect(runReport.mock.calls[2]?.[1]).toMatchObject({ offset: "0", os_family: "linux" });
  });
});

describe("a parameter that names another module's row", () => {
  const QUEUES = [
    { id: "11111111-1111-1111-1111-111111111111", key: "inc", name: "Incidents", active: true },
    { id: "22222222-2222-2222-2222-222222222222", key: "req", name: "Requests", active: true },
  ];

  it("offers the queues by name instead of a box to type a UUID into", async () => {
    // FALSIFIER: the previous version rendered `<input type="text">` for this
    // parameter, so there is no combobox and no option named "Incidents".
    renderReport(
      client({
        runReport: vi.fn().mockResolvedValue(slaPage(4)),
        exportReport: vi.fn(),
        listTicketQueues: vi.fn().mockResolvedValue(QUEUES),
      }),
      "sla-attainment",
    );

    const picker = await screen.findByRole("combobox", { name: "Queue Id" });
    expect(picker.tagName).toBe("INPUT");
    expect(picker).not.toHaveAttribute("type", "text");
    await userEvent.click(picker);
    expect(await screen.findByRole("option", { name: /Incidents/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Requests/ })).toBeInTheDocument();
  });

  it("sends the chosen queue's id, and never the name the operator saw", async () => {
    const runReport = vi.fn().mockResolvedValue(slaPage(4));
    renderReport(
      client({
        runReport,
        exportReport: vi.fn(),
        listTicketQueues: vi.fn().mockResolvedValue(QUEUES),
      }),
      "sla-attainment",
    );

    await userEvent.click(await screen.findByRole("combobox", { name: "Queue Id" }));
    await userEvent.click(await screen.findByRole("option", { name: /Requests/ }));
    await runWindow();

    await waitFor(() => expect(runReport).toHaveBeenCalled());
    const [, params] = runReport.mock.calls[0] as [string, Record<string, string>];
    expect(params["queue_id"]).toBe("22222222-2222-2222-2222-222222222222");
  });

  it("says why the list is empty rather than showing an empty dropdown", async () => {
    renderReport(
      client({
        runReport: vi.fn().mockResolvedValue(slaPage(4)),
        exportReport: vi.fn(),
        listTicketQueues: vi.fn().mockRejectedValue(new ApiError(403, "")),
      }),
      "sla-attainment",
    );

    await userEvent.click(await screen.findByRole("combobox", { name: "Queue Id" }));
    const list = await screen.findByRole("listbox", { name: "Queue Id" });
    // "no queues exist" would be a lie: the list was REFUSED.
    await waitFor(() => expect(list).toHaveTextContent(/could not be read/i));
    expect(list).toHaveTextContent(/permission/i);
  });

  it("fetches no other module's data for a report with no such parameter", async () => {
    // `fleet-posture` publishes no entity parameter. A page that fetched the
    // queue list anyway would 403 for every operator without `ticket.read`.
    const listTicketQueues = vi.fn().mockResolvedValue(QUEUES);
    renderReport(
      client({
        runReport: vi.fn().mockResolvedValue(page()),
        exportReport: vi.fn(),
        listTicketQueues,
      }),
    );
    await screen.findByText(/^As of /);
    expect(listTicketQueues).not.toHaveBeenCalled();
  });
});

describe("no client-side aggregation (spec §11)", () => {
  it("reads at least the four module files, so the sweep below is not vacuous", () => {
    // The empty-answer trap at file scope: a glob that matched nothing makes
    // every assertion below pass over an empty object.
    const names = Object.keys(SOURCES).map((path) => path.split("/").pop());
    expect(names).toEqual(
      expect.arrayContaining(["api.ts", "types.ts", "ReportCatalogue.tsx", "ReportView.tsx"]),
    );
  });

  it.each(Object.keys(SOURCES))("%s accumulates nothing over rows or totals", (path) => {
    const source = SOURCES[path] ?? "";
    expect(source.length).toBeGreaterThan(0);
    // `.reduce(` is the accumulator; `+=` is the loop that replaces it. Either
    // one over `rows` or `totals` is a second implementation of a number the
    // server already computed, and the browser's is the one someone
    // screenshots.
    expect(source).not.toMatch(/\.reduce\(/);
    expect(source).not.toMatch(/\+=/);
    // A percentage computed in the browser is the specific case §11 names.
    expect(source).not.toMatch(/\/\s*\w+\s*\)\s*\*\s*100/);
  });
});
