/**
 * The SLA page on the form kit (redesign plan, Task 7, 2.6): policies and
 * calendars as two sections, each create dialog opened from the URL
 * (`?new=policy`, `?new=calendar`, decision D8), and the Client chip on the
 * policies, which narrows their read and only their read (decision D6). On
 * the fixture session, with the real client scope.
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry } from "../../../fixtures/client";
import { ApiError } from "../../../lib/api";
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

  // FIX ROUND 1 (review, Important): a create dialog opened from the URL --
  // by its button, by Back, or by the policy form's "Add a business
  // calendar" -- starts empty, with no refusal of an earlier attempt.
  it("opens Add a business calendar empty and without the error of a failed attempt", async () => {
    mockUseApi.mockReturnValue(
      fixtureClient({
        ...fixtureRegistry(),
        listSlaCalendars: () => [],
        createSlaCalendar: () => Promise.reject(new ApiError(503, "")),
      }),
    );
    renderPage("/sla", <SlaAdminPage />);
    const section = (await screen.findByRole("heading", { name: "Business calendars" })).closest("section") as HTMLElement;
    await userEvent.click(await within(section).findByRole("button", { name: "Add calendar" }));
    let dialog = screen.getByRole("dialog", { name: "Add calendar" });
    await userEvent.type(within(dialog).getByLabelText("Name"), "Porto warehouse");
    await userEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/unavailable/i);
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Add calendar" })).toBeNull());

    await userEvent.click(screen.getByRole("button", { name: "Add policy" }));
    await userEvent.click(within(screen.getByRole("dialog", { name: "Add policy" })).getByRole("button", { name: "Add a business calendar" }));
    dialog = await screen.findByRole("dialog", { name: "Add calendar" });
    expect(within(dialog).getByLabelText("Name")).toHaveValue("");
    expect(within(dialog).queryByRole("alert")).toBeNull();
  });

  it("opens ?new=policy empty after a policy was edited", async () => {
    const { router } = renderPage("/sla", <SlaAdminPage />);
    const table = await screen.findByRole("table", { name: "SLA policies" });
    await userEvent.click((await within(table).findAllByRole("button", { name: "Edit" }))[0] as HTMLElement);
    const edit = screen.getByRole("dialog", { name: "Edit policy" });
    await userEvent.clear(within(edit).getByLabelText(/^Response target in minutes/));
    await userEvent.type(within(edit).getByLabelText(/^Response target in minutes/), "7");
    await userEvent.click(within(edit).getByRole("button", { name: "Cancel" }));
    await act(() => router.navigate("/sla?new=policy"));
    const add = await screen.findByRole("dialog", { name: "Add policy" });
    expect(within(add).getByLabelText(/^Response target in minutes/)).toHaveValue(60);
    expect(within(add).getByLabelText(/^Resolution target in minutes/)).toHaveValue(480);
  });
});
