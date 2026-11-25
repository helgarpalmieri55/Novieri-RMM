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
    // The tile counted on the narrowed read opens that client's (general ruling 2).
    expect(screen.getByRole("link", { name: /Active policies/ })).toHaveAttribute("href", `/sla?client=${ANDES}`);
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

  // General ruling 10: no empty policy list before the narrowed read.
  it("draws no empty policy list while /me has not answered with the Client chip set", async () => {
    mockUseApi.mockReturnValue(fixtureClient({ ...fixtureRegistry(), me: () => new Promise(() => {}) }));
    renderPage(`/sla?client=${ANDES}`, <SlaAdminPage />);
    const section = (await screen.findByRole("heading", { name: "SLA policies" })).closest("section") as HTMLElement;
    expect(within(section).getByText("Loading…")).toBeInTheDocument();
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)));
    expect(within(section).queryByRole("table")).toBeNull();
  });

  // General ruling 3: Back asks before a half-written calendar is dropped; Cancel does not.
  it("asks before Back drops a half-written calendar, and Cancel does not ask", async () => {
    const { router } = renderPage("/sla", <SlaAdminPage />);
    const section = (await screen.findByRole("heading", { name: "Business calendars" })).closest("section") as HTMLElement;
    await userEvent.click(await within(section).findByRole("button", { name: "Add calendar" }));
    const dialog = screen.getByRole("dialog", { name: "Add calendar" });
    await userEvent.type(within(dialog).getByLabelText("Name"), "Porto");
    // React reports the held Back's promise as an un-awaited suspension
    // (shell/formKit.test.tsx, "LEAVING IS A CLICK ON A LINK"); asserted, not tolerated.
    const heldBack = vi.spyOn(console, "error").mockImplementation(() => {});
    await act(() => router.navigate(-1));
    const leave = await screen.findByRole("dialog", { name: "Leave without saving?" });
    expect(heldBack.mock.calls.every(([message]) => String(message).includes("suspended inside an `act` scope"))).toBe(true);
    heldBack.mockRestore();
    await userEvent.click(within(leave).getByRole("button", { name: "Cancel" }));
    await userEvent.click(within(screen.getByRole("dialog", { name: "Add calendar" })).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Add calendar" })).toBeNull());
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
  });

  // FIX ROUND 2, M3: the guard counts the Client field too.
  it("asks before Back drops an Add policy whose only change is its client", async () => {
    const { router } = renderPage("/sla", <SlaAdminPage />);
    const section = (await screen.findByRole("heading", { name: "SLA policies" })).closest("section") as HTMLElement;
    await userEvent.click(await within(section).findByRole("button", { name: "Add policy" }, { timeout: 5000 }));
    const dialog = await screen.findByRole("dialog", { name: "Add policy" });
    const client = await within(dialog).findByRole("combobox", { name: "Client" });
    const choice = [...client.querySelectorAll("option")].find((option) => option.value !== "");
    await userEvent.selectOptions(client, choice?.value ?? "");
    const heldBack = vi.spyOn(console, "error").mockImplementation(() => {});
    await act(() => router.navigate(-1));
    expect(await screen.findByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(heldBack.mock.calls.every(([message]) => String(message).includes("suspended inside an `act` scope"))).toBe(true);
    heldBack.mockRestore();
  });
});
