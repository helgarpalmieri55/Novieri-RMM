/**
 * The two shared primitives, as they behave on the Settings page.
 *
 * WHY A FILE OF ITS OWN. The per-tab suites test what each tab is FOR. These
 * are properties of the page as a whole — one way of rendering a time, one way
 * of saying a failure — and every one of them is a defect that was found in
 * the field rather than a feature somebody asked for. Grouping them means the
 * next person changing `lib/datetime.ts` or `components/form.tsx` has one
 * place to look for what the Settings module relies on them to do.
 *
 * Each `it` below names the observation that would show the behaviour broken,
 * because that is the only part of a test that is worth anything.
 */

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../lib/api";
import { NO_DATE } from "../../../lib/datetime";
import {
  BUDGET_SETTINGS_FIXTURE,
  EMAIL_SETTINGS_FIXTURE,
  apiClient,
  calls,
  installDefaults,
  renderSettings,
} from "./harness";

vi.mock("../../../lib/auth", () => ({
  accessToken: () => "test-token",
  oidcAuthority: "https://id.example.test/realms/it-suite",
}));

vi.mock("../../../lib/ApiContext", () => ({ useApi: () => apiClient() }));

beforeEach(() => {
  installDefaults();
});

async function openTab(name: string) {
  const user = userEvent.setup();
  renderSettings();
  await user.click(await screen.findByRole("tab", { name }));
  return user;
}

// --------------------------------------------------------------------------
// A. Dates and times
// --------------------------------------------------------------------------

describe("Settings — times are rendered in the signed-in user's zone", () => {
  /**
   * FALSIFIER: the paragraph shows "2026-08-01T00:00:00Z", or shows a time of
   * 00:00 on 01 Aug — either of which means the wire value reached the screen,
   * or reached it in UTC rather than in the zone the operator set.
   *
   * The fixture instant is midnight UTC and `/me` carries America/Bogota
   * (UTC-5), so the correct rendering is the PREVIOUS DAY at 19:00. Choosing a
   * fixture that straddles midnight is the point: a same-day assertion passes
   * whether or not the zone was applied.
   */
  it("renders the budget 'last changed' stamp in the profile zone, not the wire's UTC", async () => {
    await openTab("Budget");
    const stamp = await screen.findByText(/Last changed/);
    expect(stamp).toHaveTextContent("31 Jul 2026, 19:00");
    expect(stamp).toHaveTextContent("by admin");
    expect(stamp.textContent).not.toContain("2026-08-01T00:00:00Z");
  });

  /**
   * FALSIFIER: "Invalid Date" appears anywhere on the tab, or the paragraph
   * renders with an empty gap where the time should be.
   *
   * A server that has never had this setting written returns `updated_at:
   * null`. `new Date(null).toLocaleString()` is "Invalid Date" — the string
   * the helpers exist to keep off the screen.
   */
  it("says nothing rather than 'Invalid Date' when a server sends no timestamp", async () => {
    calls.getBudgetSettings.mockResolvedValue({
      ...BUDGET_SETTINGS_FIXTURE,
      updated_at: null,
      updated_by: null,
    });
    await openTab("Budget");
    await screen.findByLabelText("Base currency");
    expect(screen.queryByText(/Invalid Date/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Last changed/)).not.toBeInTheDocument();
    // `NO_DATE` is the em dash the helpers return; asserting it is absent here
    // pins that this tab suppresses the whole sentence rather than printing
    // "Last changed —.", which reads as a bug rather than as an absence.
    expect(screen.queryByText(new RegExp(`Last changed ${NO_DATE}`))).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// B. Failures, as sentences
// --------------------------------------------------------------------------

describe("Settings — every failure is said, once, in one voice", () => {
  /**
   * FALSIFIER: the alert is empty, or reads only "Save failed (HTTP 503)."
   *
   * THIS IS THE BLOCKER. `ApiError` is built as `new ApiError(status, await
   * response.text())`, and a 503 from a proxy generally has no body — so
   * `error.message` is `""`. Every call site guarded with `{error ? … }`
   * rendered nothing at all, because an empty string is falsy. The kernel is
   * driven off the STATUS first, so the sentence exists whether or not the
   * server wrote one.
   */
  it("a bodyless 503 on save produces a sentence, not an empty alert", async () => {
    const user = await openTab("Budget");
    const currency = await screen.findByLabelText<HTMLInputElement>("Base currency");
    await user.clear(currency);
    await user.type(currency, "COP");
    calls.updateBudgetSettings.mockRejectedValue(new ApiError(503, ""));

    await user.click(screen.getByRole("button", { name: "Save budget settings" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent?.trim().length ?? 0).toBeGreaterThan(20);
    expect(alert).toHaveTextContent(/unavailable right now/i);
    expect(alert).toHaveTextContent(/Nothing was changed/i);
    // No optimistic success behind the failure.
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  /**
   * FALSIFIER: the alert contains `{`, `"detail"`, `"loc"`, `"type"` or the
   * submitted value — any of which means a Pydantic payload was printed rather
   * than translated.
   *
   * `input` matters as much as the rest: it echoes what the user typed, so a
   * validation echo is a third door onto invariant 3's "credentials are never
   * returned in API responses". The planted value below stands in for one.
   */
  it("translates a 422 into named fields, and never prints the payload it arrived in", async () => {
    const user = await openTab("Budget");
    const currency = await screen.findByLabelText<HTMLInputElement>("Base currency");
    await user.clear(currency);
    await user.type(currency, "COP");
    calls.updateBudgetSettings.mockRejectedValue(
      new ApiError(
        422,
        JSON.stringify({
          detail: [
            {
              type: "string_too_short",
              loc: ["body", "base_currency"],
              msg: "String should have at least 3 characters",
              input: "ECHOED-SUBMITTED-VALUE",
            },
            {
              type: "int_parsing",
              loc: ["body", "fiscal_year_start_month"],
              msg: "Input should be a valid integer",
              input: "ECHOED-SUBMITTED-VALUE",
            },
          ],
        }),
      ),
    );

    await user.click(screen.getByRole("button", { name: "Save budget settings" }));

    const alert = await screen.findByRole("alert");
    // The FIELD is named, in the user's words, and BOTH problems are listed —
    // fixing one and resubmitting to discover the next is how a two-field form
    // takes two round trips.
    expect(alert).toHaveTextContent("Base currency: String should have at least 3 characters");
    expect(alert).toHaveTextContent("Fiscal year start month: Input should be a valid integer");
    for (const leak of ["{", "detail", "loc", "string_too_short", "ECHOED-SUBMITTED-VALUE"]) {
      expect(alert.textContent, `the payload's ${leak} must not reach the screen`).not.toContain(
        leak,
      );
    }
  });

  /**
   * FALSIFIER: the tab renders "That did not load." with no reason, or the
   * paragraph carries no `role`, so nothing is announced when a loading tab
   * becomes a failed one.
   *
   * Six tabs shared one copy of a sentence that discarded the error, which
   * made a 503 (wait and retry) indistinguishable from a 500 (someone has to
   * fix the deployment).
   */
  it("a read that fails says WHY it failed, and announces it", async () => {
    calls.getBudgetSettings.mockRejectedValue(new ApiError(503, ""));
    await openTab("Budget");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("That did not load.");
    expect(alert).toHaveTextContent(/unavailable right now/i);
    expect(alert).toHaveTextContent("check the platform status page");
  });

  /**
   * FALSIFIER: the failed test send reads as a failed SAVE, or drops the
   * reason entirely.
   *
   * The old code sliced the literal prefix "Save failed" off another
   * function's output and glued "Not sent" on. The kernel's sentences begin
   * with no such prefix, so that rewrite silently became a no-op — the button
   * that sends mail would have reported a save it never attempted.
   */
  it("a failed test send is reported as a send, with the server's reason", async () => {
    calls.getEmailSettings.mockResolvedValue(EMAIL_SETTINGS_FIXTURE);
    const user = await openTab("Email");
    calls.sendTestEmail.mockRejectedValue(
      new ApiError(500, JSON.stringify({ detail: "relay refused: 550 sender not permitted" })),
    );

    await user.click(await screen.findByRole("button", { name: "Send a test message" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Not sent.");
    expect(alert).toHaveTextContent("relay refused: 550 sender not permitted");
    expect(alert.textContent).not.toContain("Save failed");
    expect(screen.queryByText(/^Sent/)).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// C. Failures that were swallowed
// --------------------------------------------------------------------------

describe("Settings — a destructive action cannot fail quietly", () => {
  /**
   * FALSIFIER: the dialog closes, or stays open with no refusal inside it, or
   * a success message appears — any of which tells the operator the user was
   * deleted when the server refused.
   *
   * This is Auditor A's BLOCKER in the shape it takes on this tab. These four
   * mutations rendered their error into the Panel BEHIND the modal, so on a
   * 503 the operator saw an unchanged dialog and an offered button, with the
   * only account of what happened underneath the overlay.
   */
  it("a delete refused with a bodyless 503 is said inside the dialog", async () => {
    const user = await openTab("Users");
    calls.deleteUser.mockRejectedValue(new ApiError(503, ""));

    await user.click(await screen.findByRole("button", { name: "Delete casey@example.com" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(
      within(dialog).getByRole("button", { name: "Delete casey@example.com" }),
    );

    await waitFor(() => expect(calls.deleteUser).toHaveBeenCalled());
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent(/unavailable right now/i);
    expect(alert).toHaveTextContent(/Nothing was changed/i);
    // Still open, still refusable — the operator has not been told it worked.
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  /**
   * FALSIFIER: the dialog opens already showing the previous attempt's red
   * sentence.
   *
   * Nothing clears a mutation's error but another attempt, so a delete that
   * failed once left its refusal on screen for the life of the page — over the
   * next person's name, and over a retry that then succeeded.
   */
  it("reopening a confirmation does not show the last attempt's failure", async () => {
    const user = await openTab("Users");
    calls.deleteUser.mockRejectedValue(new ApiError(503, ""));

    await user.click(await screen.findByRole("button", { name: "Delete casey@example.com" }));
    const first = await screen.findByRole("dialog");
    await user.click(within(first).getByRole("button", { name: "Delete casey@example.com" }));
    await within(first).findByRole("alert");
    await user.click(within(first).getByRole("button", { name: "Cancel" }));

    await user.click(screen.getByRole("button", { name: "Delete operator@example.com" }));
    const second = await screen.findByRole("dialog");
    expect(within(second).queryByRole("alert")).not.toBeInTheDocument();
  });
});
