/**
 * Enrollment on the kit (redesign plan, Task 9, 4.11): the tabs are the
 * kit's `UrlTabs` on the `?tab=` they always had, the revoked and unlimited
 * toggles are chips in the URL (`?revoked=1`, `?unlimited=1`), the search is
 * `?q=`, and New token opens from `?new=token`.
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { fixtureClient, fixtureRegistry, type FixtureMap } from "../../fixtures/client";
import { CUSTOMER_ID } from "../../fixtures/core";
import type { ApiClient, Me } from "../../lib/api";
import { setSelectedClient } from "../../lib/clientChoice";
import { renderPage } from "../../test/renderPage";
import { EnrollmentPage } from "./EnrollmentPage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

/** The fixture session, given `enrollment.manage` and `devices.read`. */
function managing(): Me {
  const me = (fixtureRegistry().me as () => Me)();
  const codes = ["enrollment.manage", "devices.read"];
  return {
    ...me,
    permissions: [...new Set([...me.permissions, ...codes])],
    clients: (me.clients ?? []).map((client) => ({ ...client, permissions: [...new Set([...client.permissions, ...codes])] })),
  };
}

function api(over: FixtureMap = {}): ApiClient {
  return fixtureClient({ ...fixtureRegistry(), me: managing, ...over });
}

beforeEach(() => mockUseApi.mockReturnValue(api()));

describe("enrollment on the kit (4.11)", () => {
  it("puts the tab in the URL, and an unknown tab opens the default", async () => {
    const { router } = renderPage("/enrollment?tab=certificates", <EnrollmentPage />);
    expect(await screen.findByRole("tab", { name: "Certificates" })).toHaveAttribute("aria-selected", "true");
    await userEvent.click(screen.getByRole("tab", { name: "Enrollment flow" }));
    expect(router.state.location.search).toBe("?tab=flow");
    await act(() => router.navigate("/enrollment?tab=nope"));
    expect(await screen.findByRole("tab", { name: "Enrollment tokens" })).toHaveAttribute("aria-selected", "true");
  });

  it("keeps the revoked chip in the URL, so /enrollment?tab=certificates&revoked=1 reloads in that view", async () => {
    const { router } = renderPage("/enrollment?tab=certificates&revoked=1", <EnrollmentPage />);
    const chip = await screen.findByRole("button", { name: /Show revoked certificates/ });
    expect(chip).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(chip);
    expect(router.state.location.search).toBe("?tab=certificates");
    await userEvent.click(screen.getByRole("button", { name: /Show revoked certificates/ }));
    expect(router.state.location.search).toBe("?tab=certificates&revoked=1");
  });

  it("asks the server for revoked tokens from the URL", async () => {
    const listEnrollmentTokens = vi.fn((fixtureRegistry().listEnrollmentTokens as (...args: unknown[]) => unknown));
    mockUseApi.mockReturnValue(api({ listEnrollmentTokens } as unknown as FixtureMap));
    renderPage("/enrollment?revoked=1", <EnrollmentPage />);
    expect(await screen.findByRole("button", { name: /Show revoked tokens/ })).toHaveAttribute("aria-pressed", "true");
    expect(listEnrollmentTokens).toHaveBeenCalledWith(expect.objectContaining({ include_revoked: true }));
  });

  it("keeps the certificates' search in the URL, and says why the list is empty", async () => {
    const { router } = renderPage("/enrollment?tab=certificates", <EnrollmentPage />);
    await userEvent.type(await screen.findByRole("textbox", { name: "Search certificates" }), "nothing-like-it");
    expect(router.state.location.search).toBe("?tab=certificates&q=nothing-like-it");
    expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(router.state.location.search).toBe("?tab=certificates");
    expect(screen.getByRole("textbox", { name: "Search certificates" })).toHaveValue("");
  });

  it("opens New token from ?new=token, on the Tokens tab whichever tab the URL names", async () => {
    const { router } = renderPage("/enrollment?tab=certificates&new=token", <EnrollmentPage />);
    expect(await screen.findByRole("tab", { name: "Enrollment tokens" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByLabelText("Maximum uses")).toBeInTheDocument();
    // The head's button is the form's Cancel while it is open.
    await userEvent.click(within(screen.getByRole("heading", { level: 1 }).closest("section") as HTMLElement).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(router.state.location.search).toBe("?tab=certificates"));
    expect(screen.queryByLabelText("Maximum uses")).toBeNull();
  });

  it("opens the form from the head with a link-able URL, and leaving the Tokens tab closes it", async () => {
    const { router } = renderPage("/enrollment", <EnrollmentPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Create token" }));
    expect(router.state.location.search).toBe("?new=token");
    expect(screen.getByLabelText("Maximum uses")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Certificates" }));
    expect(router.state.location.search).toBe("?tab=certificates");
  });

  it("counts what each tab shows against what it has (Receta L; critique, wave O4)", async () => {
    renderPage("/enrollment", <EnrollmentPage />);
    expect(await screen.findByText(/^Showing \d+ of \d+$/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Certificates" }));
    expect(await screen.findByText(/^Showing \d+ of \d+$/)).toBeInTheDocument();
  });

  it("asks before another tab drops a typed token form, and opens it empty again (general rulings 3 and 4)", async () => {
    const { router } = renderPage("/enrollment?new=token", <EnrollmentPage />, "/enrollment");
    await userEvent.type(await screen.findByLabelText("Token name"), "Bogota");
    await userEvent.click(screen.getByRole("tab", { name: "Certificates" }));
    expect(await screen.findByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.search).toBe("?new=token");
    await userEvent.click(screen.getByRole("button", { name: "Leave without saving" }));
    await waitFor(() => expect(router.state.location.search).toBe("?tab=certificates"));
    await act(() => router.navigate("/enrollment?new=token"));
    expect(await screen.findByLabelText("Token name")).toHaveValue("");
  }, 15_000);

  // FIX ROUND 2 (re-review 1, minor 1): the site and the Client field are the
  // form too. A site picked and abandoned came back on the next opening, and
  // a changed site alone was not asked about.
  it("asks before another tab drops a site picked on its own, and opens with no site again (general rulings 3 and 4)", async () => {
    // A client chosen at the top: the form asks no Client, so the site is the one thing changed.
    setSelectedClient(CUSTOMER_ID);
    const { router } = renderPage("/enrollment?new=token", <EnrollmentPage />, "/enrollment");
    await screen.findByRole("option", { name: "Lisbon" });
    await userEvent.selectOptions(screen.getByLabelText("Site"), "site-lisbon");
    await userEvent.click(screen.getByRole("tab", { name: "Certificates" }));
    expect(await screen.findByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.search).toBe("?new=token");
    await userEvent.click(screen.getByRole("button", { name: "Leave without saving" }));
    await waitFor(() => expect(router.state.location.search).toBe("?tab=certificates"));
    await act(() => router.navigate("/enrollment?new=token"));
    await screen.findByRole("option", { name: "Lisbon" });
    expect(screen.getByLabelText("Site")).toHaveValue("");
  }, 15_000);

  it("asks before another tab drops a client picked in the form, and opens with no client again (general rulings 3 and 4)", async () => {
    // Every client chosen at the top: the form asks which one.
    const { router } = renderPage("/enrollment?new=token", <EnrollmentPage />, "/enrollment");
    await screen.findByRole("option", { name: "Andes Logistics" });
    await userEvent.selectOptions(screen.getByLabelText("Client"), CUSTOMER_ID);
    await userEvent.click(screen.getByRole("tab", { name: "Certificates" }));
    expect(await screen.findByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Leave without saving" }));
    await waitFor(() => expect(router.state.location.search).toBe("?tab=certificates"));
    await act(() => router.navigate("/enrollment?new=token"));
    await screen.findByRole("option", { name: "Andes Logistics" });
    expect(screen.getByLabelText("Client")).toHaveValue("");
    expect(screen.getByLabelText("Site")).toHaveValue("");
  }, 15_000);

  // FIX ROUND 2 (re-review 1, minor 2): an issue started on the Tokens tab
  // closes its form by going Back, as every `?new=` dialog does, so the list
  // is not left twice in history. From another tab it stays the one write in
  // place that I1 needs (EnrollmentPage.test.tsx).
  it("leaves the page on Back after an issue started on the Tokens tab", async () => {
    setSelectedClient(CUSTOMER_ID);
    const { router } = renderPage("/devices", <EnrollmentPage />, "/enrollment");
    await act(() => router.navigate("/enrollment"));
    await userEvent.click(await screen.findByRole("button", { name: "Create token" }));
    expect(router.state.location.search).toBe("?new=token");
    await userEvent.type(await screen.findByLabelText("Token name"), "Lisbon rollout");
    await screen.findByRole("option", { name: "Lisbon" });
    await userEvent.selectOptions(screen.getByLabelText("Site"), "site-lisbon");
    await userEvent.click(screen.getByRole("button", { name: "Issue token…" }));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Issue token" }));
    expect(await screen.findByText("ENR-fixture-7Qm2xVb9Lk4pR8sT1wYz3nHc6dJf0gA5")).toBeInTheDocument();
    await waitFor(() => expect(router.state.location.search).toBe(""));
    // The person's Back comes once the closed form is drawn.
    await waitFor(() => expect(screen.queryByLabelText("Token name")).toBeNull());
    expect(screen.getByText("ENR-fixture-7Qm2xVb9Lk4pR8sT1wYz3nHc6dJf0gA5")).toBeInTheDocument();
    await act(() => router.navigate(-1));
    expect(router.state.location.pathname).toBe("/devices");
  }, 15_000);
});
