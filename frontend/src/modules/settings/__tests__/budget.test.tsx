// Budget tab — `GET /budget/settings`, `PUT /budget/settings`.

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../lib/api";
import {
  CASEY_USER,
  PLANTED_SECRET,
  apiClient,
  calls,
  installDefaults,
  renderSettings,
} from "./harness";

// The OIDC client is not under test and constructs a UserManager (with a
// silent-renew timer) at import time. `../api.ts`'s org request only needs the
// bearer token, so the token source is stubbed.
vi.mock("../../../lib/auth", () => ({
  accessToken: () => "test-token",
  // The Profile tab deep-links into Keycloak's account console, so the
  // authority is now part of what `SettingsPage` needs from this module.
  // A factory mock is a strict namespace: a missing export throws.
  oidcAuthority: "https://id.example.test/realms/it-suite",
}));

// `apiClient` is referenced LAZILY — inside the returned closure, never while
// the factory runs. Importing the harness from within the factory instead
// deadlocks: the harness imports `SettingsPage`, which imports the very module
// this factory is mocking, so the import waits on a factory that is waiting on
// the import.
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => apiClient() }));

beforeEach(() => {
  installDefaults();
});

async function openTab(): Promise<void> {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("tab", { name: "Budget" }));
}

describe("Settings — Budget tab", () => {
  it("loads the current base currency and fiscal year start", async () => {
    renderSettings();
    await openTab();
    expect(await screen.findByLabelText<HTMLInputElement>("Base currency")).toHaveValue(
      "USD",
    );
    // Fiscal year start is a chip group now, not a `<select>` — see
    // "Settings — Budget tab, fiscal year as chips" below for the dedicated
    // coverage.
    expect(
      within(screen.getByRole("group", { name: "Fiscal year starts in" })).getByRole("button", {
        name: "January",
      }),
    ).toHaveAttribute("aria-pressed", "true");
  });

  // Design review, 2026-09-11: "usr-helgar is rendered as a person" —
  // `updated_by` is a raw user id (`budget/api.py:_settings_out` puts
  // `row.updated_by`, a uuid FK, on the wire with no join), and the one
  // sentence naming a human on this panel printed it unstyled, as if it were
  // one. `CASEY_USER.id` is in `USERS_FIXTURE`, so a directory this session
  // can read resolves it to her actual name.
  it("resolves 'last changed by' to the actor's name when the directory has it", async () => {
    calls.getBudgetSettings.mockResolvedValue({
      client_id: "org-1",
      base_currency: "USD",
      fiscal_year_start_month: 1,
      updated_by: CASEY_USER.id,
      updated_at: "2026-08-01T00:00:00Z",
    });
    renderSettings();
    await openTab();

    const stamp = await screen.findByText(/Last changed/);
    expect(stamp).toHaveTextContent(`by ${CASEY_USER.display_name}`);
    expect(stamp.textContent).not.toContain(CASEY_USER.id);
  });

  // The other half: an id the directory does not hold (a deleted account, a
  // service actor, or — the default fixture's own case — a legacy id like
  // "admin" that predates real user ids) must still be SAID, but not dressed
  // up as a name. `.mono` is the mark every other raw wire handle in this
  // console carries, so this is the falsifier for "make the string not
  // pretend it is a name": the id must render inside that mark, not as plain
  // text indistinguishable from `Casey Doe` two lines up.
  it("prints an unresolved actor id honestly, as an id and not as a name", async () => {
    renderSettings();
    await openTab();

    const stamp = await screen.findByText(/Last changed/);
    expect(stamp).toHaveTextContent("by admin");
    const idNode = within(stamp).getByText("admin");
    expect(idNode.tagName).toBe("SPAN");
    expect(idNode).toHaveClass("mono");
  });

  // `users.read` is not implied by `budget.read`/`budget.manage`
  // (`patch/api.ts`'s identical `useApprovalActorDirectory` documents the
  // same gap), so this asks for the directory only when this session can
  // actually read one — a hopeful request on every visit would 403 for many
  // operators. `installDefaults` without `users.read` is exactly that
  // session; the id must still be said, just never resolved against a
  // directory this session was never allowed to fetch.
  it("never asks for the user directory without users.read, and still shows the honest id", async () => {
    installDefaults(["budget.read", "budget.manage"]);
    renderSettings();
    await openTab();

    const stamp = await screen.findByText(/Last changed/);
    expect(stamp).toHaveTextContent("by admin");
    expect(calls.listUsers).not.toHaveBeenCalled();
  });

  // MSP clients phase 2 (plan 2026-10-26 Task 8): each client has its own
  // base currency and fiscal year. Until the selector exists (PHASE 3) this
  // tab shows the internal client's, whose id is `/me`'s `org_id`.
  it("reads the internal client's settings", async () => {
    renderSettings();
    await openTab();
    await screen.findByLabelText("Base currency");
    expect(calls.getBudgetSettings).toHaveBeenCalledWith("org-1");
    for (const call of calls.getBudgetSettings.mock.calls) expect(call).toEqual(["org-1"]);
  });

  // A client's settings are the documented defaults until somebody sets
  // them, and then no row exists: nothing was changed, by nobody, so the
  // panel says nothing about a change.
  it("says nothing about a change when the client's settings were never set", async () => {
    calls.getBudgetSettings.mockResolvedValue({
      client_id: "org-1",
      base_currency: "USD",
      fiscal_year_start_month: 1,
      updated_by: null,
      updated_at: null,
    });
    renderSettings();
    await openTab();
    expect(await screen.findByLabelText<HTMLInputElement>("Base currency")).toHaveValue("USD");
    expect(screen.queryByText(/Last changed/)).not.toBeInTheDocument();
  });

  it("puts the ISO 4217 hint in the description, not in the label", async () => {
    renderSettings();
    await openTab();
    const currency = await screen.findByRole("textbox", { name: "Base currency" });
    expect(currency).toHaveAccessibleDescription("ISO 4217, e.g. EUR");
  });

  it("PUTs the edited values with the mixed-basis acknowledgement, shows success, and refetches", async () => {
    const user = userEvent.setup();
    renderSettings();
    await openTab();

    const currency = await screen.findByLabelText<HTMLInputElement>("Base currency");
    await user.clear(currency);
    await user.type(currency, "COP");
    // The acknowledgement only exists once the currency is actually changing.
    await user.click(screen.getByRole("checkbox"));
    calls.getBudgetSettings.mockResolvedValue({
      client_id: "org-1",
      base_currency: "COP",
      fiscal_year_start_month: 1,
      updated_by: "admin",
      updated_at: "2026-08-16T00:00:00Z",
    });

    await user.click(screen.getByRole("button", { name: "Save budget settings" }));

    await waitFor(() => expect(calls.updateBudgetSettings).toHaveBeenCalledTimes(1));
    // The internal client's settings: its id is `/me`'s `org_id` (PHASE 3:
    // the selector's).
    expect(calls.updateBudgetSettings).toHaveBeenCalledWith("org-1", {
      base_currency: "COP",
      fiscal_year_start_month: 1,
      acknowledge_mixed_basis: true,
    });
    expect(await screen.findByRole("status")).toHaveTextContent(/Saved\./);
    await waitFor(() => expect(calls.getBudgetSettings.mock.calls.length).toBeGreaterThan(1));
  });

  it("a failed save is unmistakable: the server's error is shown and success is not claimed", async () => {
    const user = userEvent.setup();
    renderSettings();
    await openTab();

    const currency = await screen.findByLabelText<HTMLInputElement>("Base currency");
    await user.clear(currency);
    await user.type(currency, "COP");
    calls.updateBudgetSettings.mockRejectedValue(
      new ApiError(
        409,
        JSON.stringify({ detail: "expenses already exist on the previous basis" }),
      ),
    );

    await user.click(screen.getByRole("button", { name: "Save budget settings" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("expenses already exist on the previous basis");
    // NOT the HTTP status any more. Cycle 4 moved every failure sentence onto
    // `describeSubmitError`, which leads with what the operator can DO and
    // leaves the status code out — it is diagnostic, not actionable. What is
    // asserted instead is the property that function exists to guarantee and
    // that the old module-local `describeError` broke: the JSON envelope the
    // sentence arrived in never reaches the screen. A regression to any
    // raw-body fallback puts `{"detail":` on screen and turns this red.
    expect(alert).not.toHaveTextContent("detail");
    expect(alert.textContent).not.toContain("{");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("sends no acknowledgement it was not given", async () => {
    const user = userEvent.setup();
    renderSettings();
    await openTab();

    const group = await screen.findByRole("group", { name: "Fiscal year starts in" });
    await user.click(within(group).getByRole("button", { name: "April" }));
    await user.click(screen.getByRole("button", { name: "Save budget settings" }));

    await waitFor(() => expect(calls.updateBudgetSettings).toHaveBeenCalledTimes(1));
    expect(calls.updateBudgetSettings).toHaveBeenCalledWith("org-1", {
      base_currency: "USD",
      fiscal_year_start_month: 4,
    });
  });

  it("renders no secret and offers no field that could carry one", async () => {
    renderSettings();
    await openTab();
    await screen.findByLabelText("Base currency");
    expect(document.body.textContent ?? "").not.toContain(PLANTED_SECRET);
    expect(document.querySelectorAll('input[type="password"]')).toHaveLength(0);
  });

  it("disables the form, with the reason, for a budget.read holder without budget.manage", async () => {
    installDefaults(["budget.read"]);
    renderSettings();
    await openTab();
    expect(await screen.findByLabelText("Base currency")).toBeDisabled();
    // `Fiscal year starts in` is a chip group, not a form element — `Chips`
    // disables each button individually rather than the wrapping group, so
    // this checks the buttons the way `email.test.tsx` and
    // `shell/viz.test.tsx` do for the same shape.
    const fiscalYear = screen.getByRole("group", { name: "Fiscal year starts in" });
    for (const button of within(fiscalYear).getAllByRole("button")) {
      expect(button).toBeDisabled();
    }
    expect(screen.getByRole("button", { name: "Save budget settings" })).toBeDisabled();
    expect(screen.getByText(/requires the budget\.manage permission/i)).toBeInTheDocument();
  });
});

/**
 * FISCAL YEAR START IS A FIXED, TWELVE-MEMBER ENUMERATION — the design's
 * chip group, not the browser's own `<select>` painted through the kit's
 * box, now through the shared `ChipsField` (`viz.tsx`) that closed this call
 * site, Email's Encryption and Organization's per-key choice fields into
 * one. `Chips` carries a real `disabled` state as of this cycle, so the
 * read-only branch is the SAME chip group, disabled, rather than a parallel
 * `<select disabled>`.
 */
describe("Settings — Budget tab, fiscal year as chips", () => {
  it("offers the twelve months as a group, not a menu", async () => {
    renderSettings();
    await openTab();

    const group = await screen.findByRole("group", { name: "Fiscal year starts in" });
    for (const name of [
      "January",
      "February",
      "March",
      "April",
      "May",
      "June",
      "July",
      "August",
      "September",
      "October",
      "November",
      "December",
    ]) {
      expect(within(group).getByRole("button", { name })).toBeInTheDocument();
    }
    expect(within(group).getByRole("button", { name: "January" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    // The select is gone in the editable case, not merely hidden.
    expect(document.querySelector("select")).toBeNull();
  });

  it("the read-only branch is the same chip group, genuinely disabled — not a parallel <select>", async () => {
    installDefaults(["budget.read"]);
    renderSettings();
    await openTab();

    // The select is gone. There is exactly one control for this field now,
    // not two markups behind a branch.
    expect(document.querySelector("select")).toBeNull();

    const group = await screen.findByRole("group", { name: "Fiscal year starts in" });
    // Every chip disabled — not merely styled grey. `shell/viz.test.tsx`
    // pins the keyboard half of this (not focusable, not clickable)
    // directly against `Chips` itself.
    for (const button of within(group).getAllByRole("button")) {
      expect(button).toBeDisabled();
    }
    // The stored value still reaches the disabled control — `budget.read`
    // alone still loads real data, unlike Email's identical-permission
    // Encryption field.
    expect(within(group).getByRole("button", { name: "January" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
});
