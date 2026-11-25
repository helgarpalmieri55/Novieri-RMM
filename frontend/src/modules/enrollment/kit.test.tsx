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
import type { ApiClient, Me } from "../../lib/api";
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
});
