/**
 * The SLA page on the form kit (redesign plan, Task 7, 2.6): policies and
 * calendars as two sections, each create dialog opened from the URL
 * (`?new=policy`, `?new=calendar`, decision D8), and the Client chip on the
 * policies, which narrows their read and only their read (decision D6). On
 * the fixture session, with the real client scope.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { fixtureClient } from "../../../fixtures/client";
import { recordingNarrowing } from "../../../test/narrowing";
import { renderPage } from "../../../test/renderPage";
import { SlaAdminPage } from "../SlaAdminPage";

const ANDES = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57"; // src/fixtures/core.ts CUSTOMER_ID

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

beforeEach(() => mockUseApi.mockReturnValue(fixtureClient()));

describe("the SLA page on the form kit", () => {
  it("opens Add policy from ?new=policy", async () => {
    renderPage("/sla?new=policy", <SlaAdminPage />);
    expect(await screen.findByRole("dialog", { name: "Add policy" })).toBeInTheDocument();
  });

  it("opens Add calendar from ?new=calendar, and the button writes it", async () => {
    const { router } = renderPage("/sla", <SlaAdminPage />);
    const section = (await screen.findByRole("heading", { name: "Business calendars" })).closest("section") as HTMLElement;
    await userEvent.click(await within(section).findByRole("button", { name: "Add calendar" }));
    expect(router.state.location.search).toBe("?new=calendar");
    expect(screen.getByRole("dialog", { name: "Add calendar" })).toBeInTheDocument();
  });

  it("draws policies and calendars as two sections, with no primary button in the body", async () => {
    renderPage("/sla", <SlaAdminPage />);
    expect(await screen.findByRole("heading", { name: "SLA policies" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Business calendars" })).toBeInTheDocument();
    await screen.findByRole("table", { name: "SLA policies" });
    expect(document.querySelectorAll(".btn.primary")).toHaveLength(0);
  });

  it("reads the list only through the narrowed API of the Client chip's client (decision D6)", async () => {
    // What the page reads on the console's own API, legitimately: the
    // calendars, the SLA settings and the queue names are the organisation's
    // (a calendar has no client), and the prerequisites checklist reads the
    // organisation's own set-up (the ticket categories, the health settings,
    // the rules). The policies, wherever this page shows them, go through
    // `forClient(ANDES)`.
    const narrowing = recordingNarrowing(fixtureClient(), {
      console: [
        "listSlaCalendars",
        "getSlaSettings",
        "listTicketQueues",
        "listTicketCategories",
        "getHealthSettings",
        "listNotificationRules",
      ],
    });
    mockUseApi.mockReturnValue(narrowing.api);
    renderPage(`/sla?client=${ANDES}`, <SlaAdminPage />);
    expect(await screen.findByRole("table", { name: "SLA policies" })).toBeInTheDocument();
    await waitFor(() => expect(narrowing.client(ANDES).listSlaPolicies).toHaveBeenCalledWith());
    expect(screen.getByRole("combobox", { name: "Client" })).toHaveValue(ANDES);
    expect(narrowing.unnarrowed).toEqual([]);
  });

  it("still shows a policy for every client with the Client chip on", async () => {
    renderPage(`/sla?client=${ANDES}`, <SlaAdminPage />);
    const table = await screen.findByRole("table", { name: "SLA policies" });
    await waitFor(() => expect(within(table).getAllByRole("row").length).toBeGreaterThan(1));
    // The fixture's policies are every client's (`client_id: null`): the
    // server includes them in a client's answer, and the list shows them.
    expect(within(table).getAllByRole("row").length).toBeGreaterThan(1);
  });
});
