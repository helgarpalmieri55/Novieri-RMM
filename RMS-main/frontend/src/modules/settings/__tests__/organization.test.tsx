// Organization tab — `GET/PATCH /api/v1/orgs/current`.
//
// The 404 case is tested as carefully as the happy path: that route is being
// written by a concurrent workstream, and it is also what an operator sees if
// the route is ever removed. "The form went quietly editable and posted into
// the void" is the failure this file exists to prevent.

import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../lib/api";
import { ORG_FIXTURE, PLANTED_SECRET, apiClient, calls, installDefaults, renderSettings } from "./harness";

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

/**
 * Confirm a rename. The org NAME is tenant identity, so a submit that changes
 * it asks first — audit [MEDIUM], "Renaming the organization is a tenant-wide
 * change with no confirmation". Settings keys beside it save without one.
 */
async function confirmRename(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  await user.click(await screen.findByRole("button", { name: "Rename the organization" }));
}

describe("Settings — Organization tab", () => {
  /**
   * The form renders before the org request settles — deliberately, so the
   * page has structure while loading. So a test that asserts VALUES must wait
   * for a control that only exists once the server answered; "Default
   * timezone" is rendered from the response's own key set (`default_timezone`,
   * shown in words, with no trace of the raw key left on screen — see
   * "does not print the raw setting key anywhere on the panel" below).
   */
  async function loadedForm(): Promise<HTMLInputElement> {
    await screen.findByLabelText("Default timezone");
    return screen.getByLabelText<HTMLInputElement>("Organization name");
  }

  it("loads the org's current values into the form", async () => {
    renderSettings();
    const name = await loadedForm();
    expect(name.value).toBe(ORG_FIXTURE.name);
    expect(screen.getByLabelText<HTMLInputElement>("Default timezone").value).toBe(
      "America/Bogota",
    );
    expect(screen.getByLabelText<HTMLInputElement>("Evidence retention days").value).toBe("365");
    // A SWITCH, not a dropdown of the wire enums: the boolean key is drawn
    // with the kit's `.tog`, so its state is `aria-checked` rather than the
    // string "true" in a `<select>`.
    expect(screen.getByRole("switch", { name: "Self service portal" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  /**
   * RE-POINTED, design review 2026-09-11 (`docs/design/settings-language.md`):
   * "the tab the operator lands on prints six database keys in a code font
   * ... it is now the only tab with any .mono content at all." The two tests
   * this replaced ("wires the raw key ... as its accessible description" and
   * "renders a text setting through Field's own help slot") pinned exactly
   * that leak on purpose, back when review finding 4 was about the MARKUP
   * carrying the key correctly rather than about whether the key belonged on
   * screen at all. The decision reversed: none of the six keys on this panel
   * (`date_format`, `default_page_size`, `locale`, `show_device_serials`,
   * `support_email`, `timezone`) are ones an operator acts on, so the fix is
   * the same one the notification event cell already made — removal, not a
   * disclosure — and this is the new target that actually falsifies it: the
   * raw key must not be readable, announced, or wrapped in `.field-wrap` (the
   * marker `Field` only emits when it is carrying a `help` node) anywhere on
   * the panel.
   */
  it("does not print the raw setting key anywhere on the panel", async () => {
    renderSettings();
    await loadedForm();

    for (const key of [
      "default_timezone",
      "evidence_retention_days",
      "self_service_portal",
    ]) {
      expect(screen.queryByText(key)).not.toBeInTheDocument();
    }
    expect(screen.getByLabelText("Default timezone")).not.toHaveAccessibleDescription(
      "default_timezone",
    );
    expect(screen.getByLabelText("Evidence retention days")).not.toHaveAccessibleDescription(
      "evidence_retention_days",
    );
    // No `help` is passed any more, so `Field` renders a plain `.field` —
    // `.field-wrap` only exists when there is a help line to wrap.
    expect(screen.getByLabelText("Default timezone").closest(".field-wrap")).toBeNull();
    expect(
      screen.getByLabelText("Evidence retention days").closest(".field-wrap"),
    ).toBeNull();
  });

  it("offers a closed-vocabulary key as words, and sends the token back", async () => {
    // `date_format` was a text box holding `dmy`: the one value on this panel
    // that a person could neither read nor guess the alternatives to, beside
    // six they could. The server declares the set (`SettingSpec.choices`), so
    // the console names it — and still sends exactly what the server stores.
    const user = userEvent.setup();
    const withFormat = {
      ...ORG_FIXTURE,
      settings: { ...ORG_FIXTURE.settings, date_format: "dmy" },
    };
    calls.getCurrentOrg.mockResolvedValue(withFormat);
    calls.updateCurrentOrg.mockResolvedValue(withFormat);
    renderSettings();

    const group = await screen.findByRole("group", { name: "Date format" });
    expect(within(group).getAllByRole("button")).toHaveLength(3);
    // The stored value is the pressed one, in words.
    const stored = within(group).getByRole("button", { name: /Day\/month\/year/ });
    expect(stored).toHaveAttribute("aria-pressed", "true");
    // RE-POINTED, design review 2026-09-11: the key used to stay on screen
    // deliberately ("the panel's subject is which keys this server admits
    // exist"); the decision reversed with the rest of this panel's mono
    // removal, so the genuine target now is that it is gone.
    expect(screen.queryByText("date_format")).not.toBeInTheDocument();
    // And the raw token is not what a person reads and retypes.
    expect(screen.queryByDisplayValue("dmy")).not.toBeInTheDocument();

    await user.click(within(group).getByRole("button", { name: /Year-month-day/ }));
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(calls.updateCurrentOrg).toHaveBeenCalledTimes(1));
    expect(calls.updateCurrentOrg.mock.calls[0]?.[0]?.settings?.date_format).toBe("iso");
  });

  it("keeps a stored value the console has never heard of, rather than re-pointing the control", async () => {
    // The one drift that would matter: a server that adds a fourth date format
    // must not have this console silently select the first option and write a
    // value nobody chose.
    calls.getCurrentOrg.mockResolvedValue({
      ...ORG_FIXTURE,
      settings: { ...ORG_FIXTURE.settings, date_format: "ymd_dots" },
    });
    renderSettings();

    const group = await screen.findByRole("group", { name: "Date format" });
    expect(within(group).getAllByRole("button")).toHaveLength(4);
    expect(within(group).getByRole("button", { name: "ymd_dots" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  /**
   * A REAL FIX, NOT JUST A REFACTOR. Before `Chips` carried its own
   * `disabled` state, this field's `onSelect` silently no-opped on
   * `!editable` while every chip stayed a live, focusable, clickable
   * button — the read-only render looked editable and swallowed the click.
   * `ChipsField`'s extraction (closing this call site with Email's and
   * Budget's) is what makes `disabled={!editable}` free to wire correctly
   * here too, the same way every other control on this panel already is.
   */
  it("disables the choice field for a reader without orgs.manage, rather than swallowing the click", async () => {
    const user = userEvent.setup();
    installDefaults(["sla.read"]);
    calls.getCurrentOrg.mockResolvedValue({
      ...ORG_FIXTURE,
      settings: { ...ORG_FIXTURE.settings, date_format: "dmy" },
    });
    renderSettings();

    const group = await screen.findByRole("group", { name: "Date format" });
    for (const button of within(group).getAllByRole("button")) {
      expect(button).toBeDisabled();
    }

    // Not merely unresponsive to a real click (a disabled button refuses
    // that on its own): the callback itself must never fire, even from a
    // dispatched click a disabled DOM node cannot actually stop.
    await user.click(within(group).getByRole("button", { name: /Year-month-day/ }));
    expect(
      within(group).getByRole("button", { name: /Day\/month\/year/ }),
    ).toHaveAttribute("aria-pressed", "true");
  });

  it("PATCHes the edited values, shows success, and refetches", async () => {
    const user = userEvent.setup();
    renderSettings();
    const name = await loadedForm();

    await user.clear(name);
    await user.type(name, "Toeshee Holdings");
    // The server answers with what it stored, which is what the form re-seeds
    // from — no optimistic anything.
    calls.getCurrentOrg.mockResolvedValue({ ...ORG_FIXTURE, name: "Toeshee Holdings" });
    calls.updateCurrentOrg.mockResolvedValue({ ...ORG_FIXTURE, name: "Toeshee Holdings" });
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await confirmRename(user);

    await screen.findByRole("status");
    expect(calls.updateCurrentOrg, "a PATCH must have been sent").toHaveBeenCalledTimes(1);
    expect(calls.updateCurrentOrg).toHaveBeenCalledWith({
      name: "Toeshee Holdings",
      settings: {
        default_timezone: "America/Bogota",
        evidence_retention_days: 365,
        self_service_portal: true,
      },
    });
    expect(screen.getByRole("status")).toHaveTextContent(/Saved\./);
    // Refetched: a GET after the PATCH.
    await waitFor(() => {
      const lastGet = calls.getCurrentOrg.mock.invocationCallOrder.at(-1) ?? -1;
      const patch = calls.updateCurrentOrg.mock.invocationCallOrder[0] ?? Number.POSITIVE_INFINITY;
      expect(lastGet).toBeGreaterThan(patch);
    });
  });

  it("a failed save is unmistakable: the server's error is shown and success is not claimed", async () => {
    const user = userEvent.setup();
    renderSettings();
    const name = await loadedForm();
    await user.type(name, "!");

    calls.updateCurrentOrg.mockRejectedValue(
      new ApiError(400, JSON.stringify({ detail: "name may not contain '!'" })),
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await confirmRename(user);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("name may not contain '!'");
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
    expect(screen.queryByText(/^Saved\./)).not.toBeInTheDocument();
  });

  it("never renders a credential-shaped setting value, and names the key instead", async () => {
    renderSettings();
    await loadedForm();
    // Invariant 3. The fixture's `smtp_password` is what a server that
    // allowlisted a credential-shaped key by mistake would send.
    expect(document.body.textContent ?? "").not.toContain(PLANTED_SECRET);
    expect(screen.queryByLabelText("smtp_password")).not.toBeInTheDocument();
    for (const input of document.querySelectorAll("input")) {
      expect(input.value).not.toContain(PLANTED_SECRET);
    }
    // Named, not silently dropped — including the structured key.
    expect(screen.getByText(/smtp_password/)).toBeInTheDocument();
    expect(screen.getByText(/escalation_matrix/)).toBeInTheDocument();
  });

  it("disables every control, with the reason, when the operator lacks orgs.manage", async () => {
    installDefaults(["sla.read"]);
    renderSettings();
    const name = await loadedForm();
    expect(name).toBeDisabled();
    expect(screen.getByLabelText("Default timezone")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(screen.getByText(/requires the orgs\.manage permission/i)).toBeInTheDocument();
  });

  it("says the route is not available on a 404 rather than offering an empty form", async () => {
    calls.getCurrentOrg.mockRejectedValue(new ApiError(404, JSON.stringify({ detail: "Not Found" })));
    renderSettings();
    expect(
      await screen.findByText(/Organization profile is not available on this server/i),
    ).toBeInTheDocument();
    const name = screen.getByLabelText<HTMLInputElement>("Organization name");
    expect(name).toBeDisabled();
    expect(name.value).toBe("");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
  });

  // ----------------------------------------------------------------------
  // The submit gap (UX cycle 3). This tab was a `<div>` of inputs and a button
  // that disabled itself for anything it disliked and never said what.
  // ----------------------------------------------------------------------

  /** Every PATCH the tab sent. Asserted as a LIST so a failure prints the body
   *  that should never have left the browser. */
  function patches(): unknown[] {
    return calls.updateCurrentOrg.mock.calls;
  }

  // ----------------------------------------------------------------------
  // UX AUDIT [MEDIUM], /settings → Organization: "Renaming the organization is
  // a tenant-wide change with no confirmation". Both tests below fail against
  // the version that PATCHed straight from the Save click.
  // ----------------------------------------------------------------------

  it("asks before renaming the tenant, naming the old and the new name", async () => {
    const user = userEvent.setup();
    renderSettings();
    const name = await loadedForm();

    await user.clear(name);
    await user.type(name, "Toeshee Holdings");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    const dialog = await screen.findByRole("dialog");
    // The SUBJECT is what lets somebody notice they are renaming the wrong
    // thing, so both names are in it.
    expect(dialog).toHaveTextContent("Toeshee Group → Toeshee Holdings");
    expect(dialog).toHaveTextContent(/appears everywhere from now on/i);
    // The one thing an operator would be surprised by, and the reason this
    // sentence survived the copy cut: the old name stays in the written audit.
    expect(dialog).toHaveTextContent(/audit entries already written keep the old name/i);
    expect(patches()).toEqual([]);

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(patches()).toEqual([]);
    // Cancelling the confirmation is not discarding the edit.
    expect(screen.getByLabelText<HTMLInputElement>("Organization name").value).toBe(
      "Toeshee Holdings",
    );
  });

  it("says the tab has unsaved changes, and stops saying it once saved", async () => {
    // AUDIT [MEDIUM]'s first ask on this tab: "Add a dirty-state indicator".
    // FALSIFIER: delete the indicator and the first assertion below fails; make
    // it unconditional and the last one does.
    const user = userEvent.setup();
    renderSettings();
    const name = await loadedForm();

    expect(screen.queryByText(/unsaved changes on this tab/i)).not.toBeInTheDocument();
    await user.type(name, " Holdings");
    expect(screen.getByText(/unsaved changes on this tab/i)).toBeInTheDocument();

    calls.getCurrentOrg.mockResolvedValue({ ...ORG_FIXTURE, name: "Toeshee Group Holdings" });
    calls.updateCurrentOrg.mockResolvedValue({ ...ORG_FIXTURE, name: "Toeshee Group Holdings" });
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await confirmRename(user);

    await screen.findByRole("status");
    expect(screen.queryByText(/unsaved changes on this tab/i)).not.toBeInTheDocument();
  });

  it("does not ask when only a settings key changed — the modal is for identity", async () => {
    // Over-confirming is its own defect: a dialog in front of every save
    // teaches people to click through the one that matters.
    const user = userEvent.setup();
    renderSettings();
    await loadedForm();

    const retention = screen.getByLabelText<HTMLInputElement>("Evidence retention days");
    await user.clear(retention);
    await user.type(retention, "400");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(patches()).toHaveLength(1));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("names the emptied field on submit and sends nothing, instead of PATCHing a blank name", async () => {
    // FALSIFIED BY THE OLD BUTTON: it was enabled (an emptied name is a dirty
    // form), so the click sent `{"name": ""}` and the server's 422 was the only
    // account of what happened. The label asserted is the one ON SCREEN.
    const user = userEvent.setup();
    renderSettings();
    const name = await loadedForm();

    await user.clear(name);
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Organization name is required");
    expect(patches()).toEqual([]);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("refuses a negative value in a numeric settings key, naming the key", async () => {
    // `evidence_retention_days` is a window. The old submit ran `Number(raw)`
    // and sent whatever came out, so -5 became a retention of minus five days
    // on a key the server allowlists.
    const user = userEvent.setup();
    renderSettings();
    await loadedForm();
    const retention = screen.getByLabelText<HTMLInputElement>("Evidence retention days");

    await user.clear(retention);
    await user.type(retention, "-5");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "evidence_retention_days cannot be negative",
    );
    expect(patches()).toEqual([]);
  });

  it("saves on Enter, because the tab is a real form", async () => {
    // There was no `<form>` at all — `components/ui`'s `Button` sets no `type`,
    // so the save control was an orphan and Enter did nothing on this tab.
    const user = userEvent.setup();
    renderSettings();
    const name = await loadedForm();

    await user.clear(name);
    // WRAPPED IN ITS OWN `act`, AND THIS IS A TEST DEFECT, NOT A PRODUCT ONE.
    // `{Enter}` inside `user.type` does not send a `submit` event itself --
    // user-event's keypress behaviour plugin looks up the form's submit
    // button and dispatches a synthetic CLICK on it, as a side effect fired
    // from inside the keypress event's own handling rather than as its own
    // top-level, individually-awaited interaction. That click opens
    // `ConfirmDialog`, whose `useDialogSurface` effect steals focus in the
    // same tick -- and that combination leaves React with scheduled work its
    // own internal act queue never saw awaited, which is exactly React's
    // "component suspended inside an `act` scope... call was not awaited"
    // diagnostic (nothing here suspends on data; it is `act`'s generic name
    // for "I flushed and something was still pending"). Confirmed by
    // bisection: the warning fires during this exact `user.type` call and
    // nowhere else in the test, it does not fire when Enter is pressed on an
    // unchanged (not-dirty) form where no dialog opens, and it does not fire
    // when the same dialog is opened by `user.click` on Save instead of by
    // Enter (see "asks before renaming the tenant..." above, which does that
    // and is not wrapped). The explicit `act` here closes exactly that gap by
    // flushing the dialog's mount effect before this interaction is
    // considered done, which is what user-event's own wrapper should have
    // covered. Reproduces identically against this file's pre-branch
    // version, so it predates this branch and is not a regression it
    // introduced -- it is fixed here because this test is in scope.
    await act(async () => {
      await user.type(name, "Toeshee Holdings{Enter}");
    });
    await confirmRename(user);

    await waitFor(() => {
      expect(patches()).toHaveLength(1);
    });
    const [body] = patches()[0] as [{ name: string }];
    expect(body.name).toBe("Toeshee Holdings");
  });

  it("says there is nothing to save rather than going dead on an untouched form", async () => {
    // The other silent no-op: SaveBar disabled the button whenever the form was
    // clean, which reads as "this page is broken", not "you changed nothing".
    const user = userEvent.setup();
    renderSettings();
    await loadedForm();

    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("There are no unsaved changes");
    expect(patches()).toEqual([]);
  });

  it("lets the field pair fold to one column instead of holding two at any width", async () => {
    // At 390px the two pairs stayed two-up at about 148px each while the
    // section list beside them folded, so `it@toeshee.example` filled its box
    // to the edge — the un-folded form read as an oversight. `.form-grid`'s
    // own 640px fold has since landed in the kit, so this grid would reflow
    // without the inline value; it keeps counting its own columns because
    // `auto-fit` also caps the pair at one field's width in a wide panel.
    renderSettings();
    const grid = (await loadedForm()).closest(".form-grid");
    expect(grid).not.toBeNull();
    const columns = (grid as HTMLElement).style.gridTemplateColumns;
    expect(columns).toContain("auto-fit");
    // A fixed two-column track is exactly what could not fold.
    expect(columns).not.toContain("repeat(2");
  });

  it("says a 403 is a permission answer, not an outage", async () => {
    calls.getCurrentOrg.mockRejectedValue(new ApiError(403, JSON.stringify({ detail: "forbidden" })));
    renderSettings();
    expect(
      await screen.findByText(/Organization profile not available \(insufficient permissions/i),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Organization name")).not.toBeInTheDocument();
  });

  it("leaves the MCP switch to its own tab: no control here, and a save never sends it", async () => {
    // `mcp_enabled` is edited on Settings -> AI connections, which asks before
    // turning it on. A second, unconfirmed copy here would be a way round that.
    calls.getCurrentOrg.mockResolvedValue({
      ...ORG_FIXTURE,
      settings: { ...ORG_FIXTURE.settings, mcp_enabled: true },
    });
    const user = userEvent.setup();
    renderSettings();
    await loadedForm();

    expect(screen.getAllByRole("switch").map((control) => control.textContent)).toEqual(["Self service portal"]);
    expect(screen.queryByText(/mcp/i)).toBeNull();

    const retention = screen.getByLabelText<HTMLInputElement>("Evidence retention days");
    await user.clear(retention);
    await user.type(retention, "400");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(calls.updateCurrentOrg).toHaveBeenCalledTimes(1));
    const body = calls.updateCurrentOrg.mock.calls[0]?.[0] as { settings?: Record<string, unknown> };
    expect(body.settings?.evidence_retention_days).toBe(400);
    expect(body.settings).not.toHaveProperty("mcp_enabled");
  });
});
