/**
 * The suggested-match queue (asset-inventory spec §4).
 *
 * THE SCREEN'S JOB IS COMPARISON. The platform has already decided it cannot
 * be sure — that is why the row exists — so the only useful thing this panel
 * can do is put both records in front of the operator with what matched. A
 * list of ids, or of one row plus a "resolve" button, would make the decision
 * without giving them what it takes to make it.
 *
 * AND IT MUST SAY WHICH DIRECTION IS DESTRUCTIVE. "Link" retires one of the
 * two rows. An operator clicking a button labelled only "Link" has not been
 * told that.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient, AssetSuggestion } from "../../lib/api";
import { ApiError } from "../../lib/api";
import { NO_DATE } from "../../lib/datetime";
import { SuggestionsPanel } from "./SuggestionsPanel";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const resolve = vi.fn();

function suggestion(overrides: Partial<AssetSuggestion> = {}): AssetSuggestion {
  return {
    id: "s1",
    basis: "hostname",
    created_at: "2026-08-18T09:00:00Z",
    device: {
      id: "d-new",
      hostname: "wks-001",
      description: null,
      primary_ip: null,
      source: "agent",
      lifecycle: "in_service",
      last_seen_at: "2026-08-18T09:00:00Z",
      managed: true,
    },
    candidate: {
      id: "d-old",
      hostname: "wks-001",
      description: "the one somebody typed in",
      primary_ip: "172.22.104.55",
      source: "manual",
      lifecycle: "in_service",
      last_seen_at: null,
      managed: false,
    },
    ...overrides,
  };
}

function renderPanel(
  listAssetSuggestions: () => Promise<unknown>,
  zone?: string | null,
) {
  mockUseApi.mockReturnValue({
    listAssetSuggestions,
    resolveAssetSuggestion: resolve,
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <SuggestionsPanel zone={zone} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockUseApi.mockReset();
  resolve.mockReset().mockResolvedValue({ items: [] });
});

describe("SuggestionsPanel", () => {
  it("says it is loading rather than showing an empty queue", async () => {
    renderPanel(() => new Promise<never>(() => {}));
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty queue", async () => {
    renderPanel(() => Promise.reject(new Error("boom")));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it("renders nothing at all when there is nothing to decide", async () => {
    // Not an empty-state card: this panel sits above another screen's content,
    // and a permanent "no suggestions" box is furniture that teaches an
    // operator to stop looking at that part of the page.
    const { container } = renderPanel(() => Promise.resolve({ items: [] }));
    await screen.findByText("Loading…").catch(() => undefined);
    await vi.waitFor(() => expect(container.textContent).toBe(""));
  });

  it("shows BOTH records, so the operator can actually compare them", async () => {
    renderPanel(() => Promise.resolve({ items: [suggestion()] }));
    await screen.findByText(/might be the same machine/i);
    // The distinguishing facts, not just two ids.
    expect(screen.getByText("the one somebody typed in")).toBeInTheDocument();
    expect(screen.getByText("172.22.104.55")).toBeInTheDocument();
    expect(screen.getAllByText("wks-001")).toHaveLength(2);
  });

  it("says what matched", async () => {
    renderPanel(() => Promise.resolve({ items: [suggestion({ basis: "hostname" })] }));
    expect(await screen.findByText(/same hostname/i)).toBeInTheDocument();
  });

  it("warns that linking retires a row, on the button itself", async () => {
    // An operator clicking a button labelled only "Link" has not been told
    // that one of these two records is about to be retired.
    renderPanel(() => Promise.resolve({ items: [suggestion()] }));
    const button = await screen.findByRole("button", { name: /same machine/i });
    expect(within(button).getByText(/retires/i)).toBeInTheDocument();
  });

  it("resolves as rejected when they are different machines", async () => {
    renderPanel(() => Promise.resolve({ items: [suggestion()] }));
    await userEvent.click(
      await screen.findByRole("button", { name: /different machines/i }),
    );
    expect(resolve).toHaveBeenCalledWith("s1", "rejected");
  });

  it("resolves as linked when they are one", async () => {
    renderPanel(() => Promise.resolve({ items: [suggestion()] }));
    await userEvent.click(await screen.findByRole("button", { name: /same machine/i }));
    expect(resolve).toHaveBeenCalledWith("s1", "linked");
  });
});

/**
 * The two things this panel used to get wrong about failure and time.
 *
 * LINKING IS DESTRUCTIVE -- it retires one of the two rows -- and the failure
 * path rendered `String(resolve.error)`, which for the bodyless 503 the audit
 * filed as a BLOCKER is the single word "Error", in a paragraph with no
 * `role`, at the bottom of a panel whose content is two cards further up.
 *
 * AND THE CARDS ARE FOR COMPARING. "Which of these two was seen more
 * recently" is the entire question, so both dates must be read in one zone --
 * the operator's, not the browser's.
 */
describe("failures and times", () => {
  it("announces a bodyless 503 as a sentence rather than the word Error", async () => {
    resolve.mockRejectedValue(new ApiError(503, ""));
    renderPanel(() => Promise.resolve({ items: [suggestion()] }));
    await userEvent.click(await screen.findByRole("button", { name: /same machine/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/unavailable right now/i);
    expect(alert.textContent).not.toBe("Error");
  });

  it("translates a Pydantic 422 instead of printing its JSON", async () => {
    resolve.mockRejectedValue(
      new ApiError(
        422,
        JSON.stringify({
          detail: [{ type: "string_pattern_mismatch", loc: ["body", "resolution"], msg: "String should match pattern", input: "maybe" }],
        }),
      ),
    );
    renderPanel(() => Promise.resolve({ items: [suggestion()] }));
    await userEvent.click(await screen.findByRole("button", { name: /different machines/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Resolution: String should match pattern");
    expect(alert.textContent).not.toMatch(/string_pattern_mismatch|\{/);
  });

  it("renders both cards' last-seen in the zone it is given", async () => {
    renderPanel(() => Promise.resolve({ items: [suggestion()] }), "America/New_York");
    // 09:00Z in August is 05:00 in New York (EDT); the runner is TZ=UTC.
    expect(await screen.findByText("18 Aug 2026, 05:00")).toBeInTheDocument();
    expect(screen.queryByText("18 Aug 2026, 09:00")).not.toBeInTheDocument();
  });

  it("says a record has no last-seen rather than showing Invalid Date", async () => {
    // The manually-entered row in the fixture has never been seen by anything.
    renderPanel(() => Promise.resolve({ items: [suggestion()] }), "America/New_York");
    const card = (await screen.findByText("Already in the inventory")).parentElement as HTMLElement;
    // THE LAST SEEN VALUE, reached through its own term. The IP row renders an
    // em dash too when an address is unknown, so a card-wide query for NO_DATE
    // would pass whatever this field said.
    const term = within(card).getByText("Last seen");
    expect(term.nextElementSibling?.textContent).toBe(NO_DATE);
    expect(document.body.textContent).not.toMatch(/Invalid Date/);
    expect(document.body.textContent).not.toMatch(/1970/);
  });
});
