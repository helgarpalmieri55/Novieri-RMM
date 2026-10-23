// Health tab — `GET /health/settings`, `PUT /health/settings`.

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../lib/api";
import {
  CATEGORY_FIXTURE,
  HEALTH_SETTINGS_FIXTURE,
  PLANTED_SECRET,
  QUEUE_FIXTURE,
  apiClient,
  calls,
  describeControl,
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
  // A `RegExp`, not the exact string "Health": the nav now appends "Set up"
  // / "Not set up" to this tab's accessible name (Task 7), and `getByRole`'s
  // `name` matches a string by whole-string equality regardless of `exact`
  // (text-query-only) -- so an exact string here would miss the tab as soon
  // as its own settings query answers `configured`.
  await user.click(await screen.findByRole("tab", { name: /^Health/ }));
}

describe("Settings — Health tab", () => {
  it("loads the current settings, including the silent-failure warning", async () => {
    renderSettings();
    await openTab();
    // The fixture stores 900 seconds. The duration control displays the
    // largest whole unit that divides it evenly -- 900 / 60 -- rather than
    // the raw seconds the wire still carries underneath.
    expect(await screen.findByLabelText<HTMLInputElement>("Mark a device offline after")).toHaveValue(
      15,
    );
    expect(screen.getByLabelText<HTMLInputElement>("Delete closed alerts after")).toHaveValue(400);
    expect(screen.getByLabelText(/Auto-resolve alert tickets/)).toBeChecked();
    // The fixture has no default queue or category, so no alert in this org
    // can open a ticket and nothing anywhere errors. That must be visible.
    expect(screen.getByText(/alerts can't raise tickets yet/i)).toBeInTheDocument();
  });

  it("puts the blank-for-none note in the description, not in the label", async () => {
    renderSettings();
    await openTab();
    const deepLink = await screen.findByRole("textbox", {
      name: "Monitoring deep-link template",
    });
    expect(deepLink).toHaveAccessibleDescription("Blank for none");
  });

  it("PUTs every field the endpoint accepts, shows success, and refetches", async () => {
    const user = userEvent.setup();
    calls.listTicketQueues.mockResolvedValue([QUEUE_FIXTURE]);
    renderSettings();
    await openTab();

    // The field loaded showing "15" under "minutes" (900 seconds). Typing
    // "20" in that same unit must PUT 1200 seconds, not 20 -- this is the
    // unit-to-seconds half of the duration control.
    const offline = await screen.findByLabelText<HTMLInputElement>("Mark a device offline after");
    await user.clear(offline);
    await user.type(offline, "20");
    await user.click(await screen.findByLabelText("Default alert queue"));
    await user.click(await screen.findByRole("option", { name: /Escalations/ }));
    calls.getHealthSettings.mockResolvedValue({
      ...HEALTH_SETTINGS_FIXTURE,
      offline_after_s: 1200,
      default_alert_queue_id: QUEUE_FIXTURE.id,
    });

    await user.click(screen.getByRole("button", { name: "Save health settings" }));

    await waitFor(() => expect(calls.updateHealthSettings).toHaveBeenCalledTimes(1));
    expect(calls.updateHealthSettings).toHaveBeenCalledWith({
      flap_window_s: 3600,
      flap_threshold: 4,
      flap_cooldown_s: 3600,
      unavailable_clear_after_s: 86400,
      alert_retention_days: 400,
      escalate_after_s: 3600,
      offline_after_s: 1200,
      approval_ttl_s: 3600,
      default_alert_queue_id: "queue-7",
      default_ticket_category_id: null,
      monitoring_deep_link_template: null,
      auto_resolve_alert_tickets: true,
    });
    expect(await screen.findByRole("status")).toHaveTextContent(/Saved\./);
    await waitFor(() => expect(calls.getHealthSettings.mock.calls.length).toBeGreaterThan(1));
  });

  it("a failed save is unmistakable: the server's bound error is shown and success is not claimed", async () => {
    const user = userEvent.setup();
    renderSettings();
    await openTab();

    const offline = await screen.findByLabelText<HTMLInputElement>("Mark a device offline after");
    await user.clear(offline);
    await user.type(offline, "20");
    calls.updateHealthSettings.mockRejectedValue(
      new ApiError(400, JSON.stringify({ detail: "offline_after_s must be between 300 and 86400" })),
    );

    await user.click(screen.getByRole("button", { name: "Save health settings" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("offline_after_s must be between 300 and 86400");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("refuses to save a blank numeric field rather than sending it as zero", async () => {
    const user = userEvent.setup();
    renderSettings();
    await openTab();

    const offline = await screen.findByLabelText<HTMLInputElement>("Mark a device offline after");
    await user.clear(offline);

    expect(screen.getByRole("button", { name: "Save health settings" })).toBeDisabled();
    expect(screen.getByText(/Not a whole number: offline_after_s/)).toBeInTheDocument();
    expect(calls.updateHealthSettings).not.toHaveBeenCalled();
  });

  it("renders no secret and offers no field that could carry one", async () => {
    renderSettings();
    await openTab();
    await screen.findByLabelText("Mark a device offline after");
    expect(document.body.textContent ?? "").not.toContain(PLANTED_SECRET);
    expect(document.querySelectorAll('input[type="password"]')).toHaveLength(0);
  });

  it("disables the form, with the reason, for a health.read holder without health.admin", async () => {
    installDefaults(["health.read"]);
    renderSettings();
    await openTab();
    expect(await screen.findByLabelText("Mark a device offline after")).toBeDisabled();
    expect(screen.getByLabelText(/Auto-resolve alert tickets/)).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save health settings" })).toBeDisabled();
    expect(screen.getByText(/requires the health\.admin permission/i)).toBeInTheDocument();
  });
});

/**
 * The two id fields that decide whether an alert can become a ticket are
 * `Alerting`; the deep-link template is `Monitoring`. Both `h3` under the
 * panel's "Endpoint health" `h2`.
 */
describe("Settings — Health tab, grouped", () => {
  it("groups the alerting ids and the monitoring link by what they are for", async () => {
    renderSettings();
    await openTab();

    const alerting = (await screen.findByRole("heading", { name: "Alerting", level: 3 })).closest(
      "section",
    ) as HTMLElement;
    expect(within(alerting).getByLabelText(/Default alert queue/)).toBeInTheDocument();
    expect(within(alerting).getByLabelText(/Default ticket category/)).toBeInTheDocument();

    const monitoring = (
      await screen.findByRole("heading", { name: "Monitoring", level: 3 })
    ).closest("section") as HTMLElement;
    expect(within(monitoring).getByLabelText("Monitoring deep-link template")).toBeInTheDocument();
  });

  // Regrouping must not turn a disabled control into a hidden one.
  it("still renders the grouped fields, disabled, for a health.read holder", async () => {
    installDefaults(["health.read"]);
    renderSettings();
    await openTab();

    const alerting = (await screen.findByRole("heading", { name: "Alerting", level: 3 })).closest(
      "section",
    ) as HTMLElement;
    expect(within(alerting).getByLabelText(/Default alert queue/)).toBeDisabled();
    // Review finding 3: the ticket-category picker had no test pinning its
    // read-only guard — `disabled={false}` here left the whole settings
    // suite green.
    expect(within(alerting).getByLabelText(/Default ticket category/)).toBeDisabled();
  });

  it("groups the eight bounded integers under Timing and thresholds", async () => {
    renderSettings();
    await openTab();

    const timing = (
      await screen.findByRole("heading", { name: "Timing and thresholds", level: 3 })
    ).closest("section") as HTMLElement;
    for (const label of [
      "Mark a device offline after",
      "Escalate an unanswered alert after",
      "Give up on an unanswerable check after",
      "Look for repeat alerts over",
      "Treat a check as repeating after",
      "Keep automatic actions paused for",
      "Approvals expire after",
      "Delete closed alerts after",
    ]) {
      expect(within(timing).getByLabelText(label)).toBeInTheDocument();
    }
  });

  it("keeps a numeric field's help wired to it after the move to Field's help slot", async () => {
    renderSettings();
    await openTab();

    const offline = await screen.findByLabelText("Mark a device offline after");
    expect(offline).toHaveAccessibleDescription(
      "How long a device can go quiet before we raise an alert.",
    );
  });

  /**
   * THE GUARD DERIVES ITS OWN SUBJECT, RATHER THAN NAMING ONE. This is the
   * exact blind spot that cost this branch a defect: the mapping table
   * behind this cycle's work was built by an inventory that enumerated
   * fields by hand and missed the eight `NUMERIC_FIELDS` renders, and
   * `HealthTab` shipped grouped 3-of-11 as a result. A hardcoded list here
   * would have the same blind spot. So this queries every labelled control
   * the tab actually rendered and asserts each one sits inside a
   * `section.field-group` — it goes red on the next field nobody grouped.
   */
  it("keeps every labelled control of the tab inside a group, with one documented exception", async () => {
    renderSettings();
    await openTab();

    const panel = screen.getByRole("tabpanel");
    // "Auto-resolve alert tickets when the alert recovers" is a bare
    // checkbox `<label>`, not a `Field` inside a `FieldGroup` — the one
    // control `HealthTab` intentionally leaves outside every group. Named
    // here, by reference to the element itself, so a NEXT ungrouped control
    // still fails below instead of being silently absorbed into this
    // allowlist.
    const autoResolve = await screen.findByLabelText(/Auto-resolve alert tickets/);
    expect(autoResolve.closest("section.field-group")).toBeNull();

    const controls = within(panel).getAllByLabelText(/.+/);
    const checked = controls.filter((control) => control !== autoResolve);
    // More than just the documented exception, or the loop below proves nothing.
    expect(checked.length).toBeGreaterThan(0);
    for (const control of checked) {
      expect(
        control.closest("section.field-group"),
        `${describeControl(control)} is not inside any field-group`,
      ).not.toBeNull();
    }

    // NAMED-CONTROL COVERAGE ISN'T EVERY CONTROL. `getAllByLabelText` derives
    // its subject from ACCESSIBLE NAMES, so a control with none — a `<span>`
    // caption next to an input, not a `<label>` bound to it — would be
    // invisible to the loop above, not merely unasserted. `Field` (and the
    // raw `.field`/`.field-wrap` shape a couple of other tabs use for a
    // caption-only control) is what every control on this tab renders
    // through regardless of whether it carries a name, so this checks it
    // directly. The auto-resolve checkbox needs no exclusion here: its
    // wrapping `<label>` carries no `.field`/`.field-wrap` class (it is a
    // bare `flex items-center gap-2` label), so it is not among the
    // elements this loop finds in the first place.
    const wrapped = Array.from(panel.querySelectorAll(".field, .field-wrap"));
    expect(wrapped.length).toBeGreaterThan(0);
    for (const el of wrapped) {
      expect(
        el.closest("section.field-group"),
        `${describeControl(el)} is not inside any field-group`,
      ).not.toBeNull();
    }
  });
});

/**
 * The duration control (`components/duration.tsx`): seconds unchanged on the
 * wire, a number beside a unit on screen. `docs/design/settings-language.md`,
 * "Durations stop being seconds".
 */
describe("Settings — Health tab, duration control", () => {
  it("displays the largest whole unit that divides the stored seconds", async () => {
    renderSettings();
    await openTab();

    // 900s -> 15 minutes (the fixture's own value).
    expect(
      await screen.findByLabelText<HTMLInputElement>("Mark a device offline after"),
    ).toHaveValue(15);
    expect(
      screen.getByLabelText<HTMLSelectElement>("Mark a device offline after unit"),
    ).toHaveValue("minutes");

    // 86400s -> 1 day: no larger unit divides a day evenly, but a day itself does.
    expect(
      screen.getByLabelText<HTMLInputElement>("Give up on an unanswerable check after"),
    ).toHaveValue(1);
    expect(
      screen.getByLabelText<HTMLSelectElement>("Give up on an unanswerable check after unit"),
    ).toHaveValue("days");

    // 3600s -> 1 hour.
    expect(
      screen.getByLabelText<HTMLInputElement>("Escalate an unanswered alert after"),
    ).toHaveValue(1);
    expect(
      screen.getByLabelText<HTMLSelectElement>("Escalate an unanswered alert after unit"),
    ).toHaveValue("hours");
  });

  it("leaves flap threshold and alert retention as plain numbers, never wrapped in a unit picker", async () => {
    renderSettings();
    await openTab();

    // flap_threshold is a COUNT ("times"), not a duration -- the brief is
    // explicit that it must stay a plain number input.
    expect(
      await screen.findByLabelText<HTMLInputElement>("Treat a check as repeating after"),
    ).toHaveValue(4);
    expect(screen.queryByLabelText("Treat a check as repeating after unit")).not.toBeInTheDocument();

    // alert_retention_days is already stored in DAYS on the wire (see
    // `backend/app/modules/health/sweep.py`'s `timedelta(days=...)`), not
    // seconds -- routing it through the seconds-based duration control would
    // silently multiply it by up to 86400. It stays a plain number too.
    expect(screen.getByLabelText<HTMLInputElement>("Delete closed alerts after")).toHaveValue(400);
    expect(screen.queryByLabelText("Delete closed alerts after unit")).not.toBeInTheDocument();
  });

  it("switching the unit redisplays the same stored value without marking the form dirty", async () => {
    const user = userEvent.setup();
    renderSettings();
    await openTab();

    const unit = await screen.findByLabelText<HTMLSelectElement>("Mark a device offline after unit");
    expect(unit).toHaveValue("minutes");
    await user.selectOptions(unit, "seconds");

    // 900 seconds, now shown in seconds.
    expect(screen.getByLabelText<HTMLInputElement>("Mark a device offline after")).toHaveValue(900);
    // A pure unit switch redisplays the value; it is not an edit, so Save
    // must not light up over it.
    expect(screen.getByRole("button", { name: "Save health settings" })).toBeDisabled();
    expect(calls.updateHealthSettings).not.toHaveBeenCalled();
  });

  it("converts a value typed after a unit switch to seconds on save", async () => {
    const user = userEvent.setup();
    renderSettings();
    await openTab();

    // Flap window loads as "1 hour" (3600s). Switch to minutes...
    const unit = await screen.findByLabelText<HTMLSelectElement>("Look for repeat alerts over unit");
    await user.selectOptions(unit, "minutes");
    const window = screen.getByLabelText<HTMLInputElement>("Look for repeat alerts over");
    // ...redisplayed as the same 3600 seconds, now in minutes.
    expect(window).toHaveValue(60);

    // ...then set to 90 minutes, which is 5400 seconds.
    await user.clear(window);
    await user.type(window, "90");

    await user.click(screen.getByRole("button", { name: "Save health settings" }));
    await waitFor(() => expect(calls.updateHealthSettings).toHaveBeenCalledTimes(1));
    expect(calls.updateHealthSettings).toHaveBeenCalledWith(
      expect.objectContaining({ flap_window_s: 5400 }),
    );
  });

  it("does not clamp an out-of-bounds value itself -- the true seconds reach the server for it to reject", async () => {
    const user = userEvent.setup();
    renderSettings();
    await openTab();

    // offline_after_s's floor is 300 seconds (`SETTING_BOUNDS`). Switch to
    // seconds and type a value under it.
    const unit = await screen.findByLabelText<HTMLSelectElement>("Mark a device offline after unit");
    await user.selectOptions(unit, "seconds");
    const offline = screen.getByLabelText<HTMLInputElement>("Mark a device offline after");
    await user.clear(offline);
    await user.type(offline, "100");

    // Bounds are an affordance, not a validation (`types.ts`'s own comment
    // on `SETTING_BOUNDS`) -- nothing about the duration control disables
    // Save for an out-of-bounds value.
    expect(screen.getByRole("button", { name: "Save health settings" })).not.toBeDisabled();

    calls.updateHealthSettings.mockRejectedValue(
      new ApiError(400, JSON.stringify({ detail: "offline_after_s must be between 300 and 86400" })),
    );
    await user.click(screen.getByRole("button", { name: "Save health settings" }));

    await waitFor(() => expect(calls.updateHealthSettings).toHaveBeenCalledTimes(1));
    // The typed value reached the server exactly as typed -- not clamped, not
    // rounded up to the bound -- so it is the SERVER's bound check that
    // catches it, and the control never pretended otherwise.
    expect(calls.updateHealthSettings).toHaveBeenCalledWith(
      expect.objectContaining({ offline_after_s: 100 }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "offline_after_s must be between 300 and 86400",
    );
  });
});

/**
 * `default_alert_queue_id` / `default_ticket_category_id` used to be raw
 * text boxes — five auditor-flagged fields across the console, two of them
 * here. They are `EntityPicker`s now, over the org's own queues and
 * categories, with the same rule this cycle applies everywhere else: a
 * value the picker cannot resolve is kept, not discarded, because a deleted
 * queue is a configuration to correct, not a field to silently blank.
 */
describe("Settings — Health tab, queue and category pickers", () => {
  const UNRESOLVABLE_QUEUE_ID = "b0000000-0000-0000-0000-00000000dead";

  it("chooses a queue by name, and keeps an id it cannot resolve", async () => {
    calls.getHealthSettings.mockResolvedValue({
      ...HEALTH_SETTINGS_FIXTURE,
      default_alert_queue_id: UNRESOLVABLE_QUEUE_ID,
    });
    calls.listTicketQueues.mockResolvedValue([QUEUE_FIXTURE]);
    renderSettings();
    await openTab();

    // A SETTINGS PAGE MUST NOT DISCARD A VALUE IT CANNOT RENDER: a deleted
    // queue is a configuration to correct, not a field to silently blank.
    expect(await screen.findByText(/b0000000/)).toBeInTheDocument();
    expect(screen.getByText(/no longer exists/i)).toBeInTheDocument();

    // The picker itself never falls back to the raw id: `EntityPicker`'s own
    // comment on why not is `components/picker.tsx`'s — an id no longer in
    // the list would render as text while still being submitted, and a value
    // nobody can see is a value nobody chose. It shows empty instead, with
    // the orphan note (above) carrying the id.
    expect(
      screen.getByLabelText<HTMLInputElement>("Default alert queue"),
    ).toHaveValue("");
  });

  it("chooses a queue by name and PUTs its id", async () => {
    const user = userEvent.setup();
    calls.listTicketQueues.mockResolvedValue([QUEUE_FIXTURE]);
    renderSettings();
    await openTab();

    await user.click(await screen.findByLabelText("Default alert queue"));
    await user.click(await screen.findByRole("option", { name: /Escalations/ }));
    expect(
      screen.getByLabelText<HTMLInputElement>("Default alert queue"),
    ).toHaveValue("Escalations");

    await user.click(screen.getByRole("button", { name: "Save health settings" }));
    await waitFor(() => expect(calls.updateHealthSettings).toHaveBeenCalledTimes(1));
    expect(calls.updateHealthSettings).toHaveBeenCalledWith(
      expect.objectContaining({ default_alert_queue_id: QUEUE_FIXTURE.id }),
    );
  });

  it("offers an explicit 'No default' option rather than leaving blank to mean it", async () => {
    const user = userEvent.setup();
    calls.getHealthSettings.mockResolvedValue({
      ...HEALTH_SETTINGS_FIXTURE,
      default_alert_queue_id: QUEUE_FIXTURE.id,
    });
    calls.listTicketQueues.mockResolvedValue([QUEUE_FIXTURE]);
    renderSettings();
    await openTab();

    await user.click(await screen.findByLabelText("Default alert queue"));
    await user.click(await screen.findByRole("option", { name: "No default queue" }));

    await user.click(screen.getByRole("button", { name: "Save health settings" }));
    await waitFor(() => expect(calls.updateHealthSettings).toHaveBeenCalledTimes(1));
    expect(calls.updateHealthSettings).toHaveBeenCalledWith(
      expect.objectContaining({ default_alert_queue_id: null }),
    );
  });

  it("saving a different field does not discard an id the picker could not resolve", async () => {
    const user = userEvent.setup();
    calls.getHealthSettings.mockResolvedValue({
      ...HEALTH_SETTINGS_FIXTURE,
      default_alert_queue_id: UNRESOLVABLE_QUEUE_ID,
    });
    calls.listTicketQueues.mockResolvedValue([QUEUE_FIXTURE]);
    renderSettings();
    await openTab();

    await screen.findByText(/no longer exists/i);

    // Loaded showing "15" under "minutes" (900 seconds); "20" in that unit
    // is 1200 seconds on the wire.
    const offline = await screen.findByLabelText<HTMLInputElement>("Mark a device offline after");
    await user.clear(offline);
    await user.type(offline, "20");

    await user.click(screen.getByRole("button", { name: "Save health settings" }));
    await waitFor(() => expect(calls.updateHealthSettings).toHaveBeenCalledTimes(1));
    expect(calls.updateHealthSettings).toHaveBeenCalledWith(
      expect.objectContaining({
        offline_after_s: 1200,
        default_alert_queue_id: UNRESOLVABLE_QUEUE_ID,
      }),
    );
  });

  it("degrades the alert-queue field to a raw id box when queues 403, without blocking the tab", async () => {
    calls.listTicketQueues.mockRejectedValue(new ApiError(403, "forbidden"));
    renderSettings();
    await openTab();

    const raw = await screen.findByLabelText<HTMLInputElement>("Default alert queue id");
    expect(raw).toBeInTheDocument();
    // The permission/outage distinction is the point of this test, not
    // incidental to it — a 403 must read as a permission problem, never as
    // "the request failed" (review finding 1).
    expect(raw).toHaveAccessibleDescription(/insufficient permissions/i);
    expect(screen.queryByLabelText("Default alert queue")).not.toBeInTheDocument();
    // Editing the eight numeric fields must still work — the 403 is on a
    // cross-module read, not on health.read/health.admin.
    expect(await screen.findByLabelText("Mark a device offline after")).not.toBeDisabled();
  });

  it("degrades the ticket-category field to a raw id box when categories 403, without blocking the tab", async () => {
    calls.listTicketCategories.mockRejectedValue(new ApiError(403, "forbidden"));
    renderSettings();
    await openTab();

    const raw = await screen.findByLabelText<HTMLInputElement>("Default ticket category id");
    expect(raw).toBeInTheDocument();
    expect(raw).toHaveAccessibleDescription(/insufficient permissions/i);
    expect(screen.queryByLabelText("Default ticket category")).not.toBeInTheDocument();
  });

  it("chooses a ticket category by name", async () => {
    const user = userEvent.setup();
    calls.listTicketCategories.mockResolvedValue([CATEGORY_FIXTURE]);
    renderSettings();
    await openTab();

    await user.click(await screen.findByLabelText("Default ticket category"));
    await user.click(await screen.findByRole("option", { name: /Incident/ }));

    await user.click(screen.getByRole("button", { name: "Save health settings" }));
    await waitFor(() => expect(calls.updateHealthSettings).toHaveBeenCalledTimes(1));
    expect(calls.updateHealthSettings).toHaveBeenCalledWith(
      expect.objectContaining({ default_ticket_category_id: CATEGORY_FIXTURE.id }),
    );
  });

  // Review finding 1: only a 403 used to degrade. A 500, a timeout, a
  // dropped connection is not "forbidden" — the old code let those fall
  // through to the picker, whose only option was the synthetic "No default
  // queue", with the real (existing) queue silently declared gone.
  it("degrades the alert-queue field to a raw id box on a server error too, and does not declare the stored queue gone", async () => {
    calls.getHealthSettings.mockResolvedValue({
      ...HEALTH_SETTINGS_FIXTURE,
      default_alert_queue_id: QUEUE_FIXTURE.id,
    });
    calls.listTicketQueues.mockRejectedValue(new ApiError(500, "internal error"));
    renderSettings();
    await openTab();

    const raw = await screen.findByLabelText<HTMLInputElement>("Default alert queue id");
    expect(raw).toHaveValue(QUEUE_FIXTURE.id);
    // A server error is not a permission problem — the help text must say
    // so distinctly from the 403 case above (review finding 1).
    expect(raw).toHaveAccessibleDescription(/the request failed/i);
    expect(raw).not.toHaveAccessibleDescription(/insufficient permissions/i);
    expect(screen.queryByLabelText("Default alert queue")).not.toBeInTheDocument();
    // The picker never got a chance to run out of ways to say the queue is
    // still there, so it must never claim otherwise.
    expect(screen.queryByText(/no longer exists/i)).not.toBeInTheDocument();
    // Editing the eight numeric fields must still work.
    expect(await screen.findByLabelText("Mark a device offline after")).not.toBeDisabled();
  });

  it("degrades the ticket-category field to a raw id box on a server error too, and does not declare the stored category gone", async () => {
    calls.getHealthSettings.mockResolvedValue({
      ...HEALTH_SETTINGS_FIXTURE,
      default_ticket_category_id: CATEGORY_FIXTURE.id,
    });
    calls.listTicketCategories.mockRejectedValue(new ApiError(504, "gateway timeout"));
    renderSettings();
    await openTab();

    const raw = await screen.findByLabelText<HTMLInputElement>("Default ticket category id");
    expect(raw).toHaveValue(CATEGORY_FIXTURE.id);
    expect(raw).toHaveAccessibleDescription(/the request failed/i);
    expect(raw).not.toHaveAccessibleDescription(/insufficient permissions/i);
    expect(screen.queryByLabelText("Default ticket category")).not.toBeInTheDocument();
    expect(screen.queryByText(/no longer exists/i)).not.toBeInTheDocument();
  });

  // Review finding 3: `queues.error != null` ALSO degrades on a failed
  // REFETCH that has a good list cached — a transient 500 on the background
  // refetch that fires on remount tore down a working picker over nothing,
  // even though `queues.data` still held the last good list. Probe: load
  // with a resolving list, remount with the mock now rejecting.
  it("keeps the alert-queue picker showing a cached list after a failed refetch, not a raw id box", async () => {
    const user = userEvent.setup();
    calls.getHealthSettings.mockResolvedValue({
      ...HEALTH_SETTINGS_FIXTURE,
      default_alert_queue_id: QUEUE_FIXTURE.id,
    });
    // First fetch (Health's initial mount) succeeds; every later call fails.
    calls.listTicketQueues.mockResolvedValueOnce([QUEUE_FIXTURE]);
    renderSettings();
    await openTab();

    expect(
      await screen.findByLabelText<HTMLInputElement>("Default alert queue"),
    ).toHaveValue("Escalations");

    calls.listTicketQueues.mockRejectedValue(new ApiError(500, "internal error"));

    // Unmount Health, remount it — `SettingsPage` renders only the active
    // tab, so this is a genuine remount, and TanStack Query refetches a
    // stale query on mount by default. `Notifications` also calls
    // `useTicketQueues` (a distinct, uncached key: `includeInactive: false`),
    // so this exercises the real path rather than a shortcut through it.
    await user.click(await screen.findByRole("tab", { name: "Notifications" }));
    await openTab();

    // The remount's background refetch must have actually run and failed —
    // not just have been skipped — before this counts as proof.
    await waitFor(() => expect(calls.listTicketQueues).toHaveBeenCalledTimes(3));

    expect(
      await screen.findByLabelText<HTMLInputElement>("Default alert queue"),
    ).toHaveValue("Escalations");
    expect(screen.queryByLabelText("Default alert queue id")).not.toBeInTheDocument();
  });

  // Review finding 2: `emptyHint` is unreachable at this call site because
  // the synthetic "No default queue"/"No default category" entry means the
  // picker's own `options.length` is never 0. Probed on a zero-queue org
  // (the harness's own default fixture): the hint never showed.
  it("tells the operator where queues come from when the org has none, not just inside an unreachable dropdown", async () => {
    calls.listTicketQueues.mockResolvedValue([]);
    renderSettings();
    await openTab();

    await screen.findByLabelText("Default alert queue");
    expect(screen.getByText(/No ticket queues exist yet\./)).toBeInTheDocument();
    // Where they come from is a link, not a menu path to follow by hand.
    expect(screen.getAllByRole("link", { name: "Create one in the service desk settings" })[0]).toHaveAttribute(
      "href",
      "/service-desk/admin",
    );
  });

  it("tells the operator where categories come from when the org has none", async () => {
    calls.listTicketCategories.mockResolvedValue([]);
    renderSettings();
    await openTab();

    await screen.findByLabelText("Default ticket category");
    expect(screen.getByText(/No ticket categories exist yet\./)).toBeInTheDocument();
    // Where they come from is a link, not a menu path to follow by hand.
    expect(screen.getAllByRole("link", { name: "Create one in the service desk settings" })[0]).toHaveAttribute(
      "href",
      "/service-desk/admin",
    );
  });

  /**
   * Review finding 2. Task 1 built `.field-wrap` on the argument that a
   * control and its hint must be ONE grid child, so the cell is the measure
   * -- and `EntityPicker`'s own hint paragraphs shipped as bare `<p
   * className="form-hint">` SIBLINGS instead, a separate grid item apiece.
   * This pins the fix as a DOM fact (one wrapper, one cell), not by eyeballing
   * a screenshot, so it cannot silently regress to two items again.
   */
  it("keeps the alert-queue picker and its orphan note in one grid cell", async () => {
    const UNRESOLVABLE = "b0000000-0000-0000-0000-00000000dead";
    calls.getHealthSettings.mockResolvedValue({
      ...HEALTH_SETTINGS_FIXTURE,
      default_alert_queue_id: UNRESOLVABLE,
    });
    calls.listTicketQueues.mockResolvedValue([QUEUE_FIXTURE]);
    renderSettings();
    await openTab();

    const input = await screen.findByLabelText("Default alert queue");
    const pickerRoot = input.closest(".picker") as HTMLElement;
    const cell = pickerRoot.parentElement as HTMLElement;
    expect(cell.parentElement).toHaveClass("form-grid");

    const orphanNote = await screen.findByText(/no longer exists/i);
    // Same grid cell: a sibling of the picker root inside the wrapper, not a
    // second direct child of `.form-grid`.
    expect(orphanNote.parentElement).toBe(cell);
  });

  it("keeps the ticket-category picker and its orphan note in one grid cell", async () => {
    const UNRESOLVABLE = "c0000000-0000-0000-0000-00000000dead";
    calls.getHealthSettings.mockResolvedValue({
      ...HEALTH_SETTINGS_FIXTURE,
      default_ticket_category_id: UNRESOLVABLE,
    });
    calls.listTicketCategories.mockResolvedValue([CATEGORY_FIXTURE]);
    renderSettings();
    await openTab();

    const input = await screen.findByLabelText("Default ticket category");
    const pickerRoot = input.closest(".picker") as HTMLElement;
    const cell = pickerRoot.parentElement as HTMLElement;
    expect(cell.parentElement).toHaveClass("form-grid");

    const orphanNote = await screen.findByText(/no longer exists/i);
    expect(orphanNote.parentElement).toBe(cell);
  });

  /**
   * Review finding 3. `EntityPicker` never emitted `aria-describedby` before
   * this (`grep -n "describedby\|describedBy" src/components/picker.tsx`
   * returned nothing across all 39 call sites), so a screen-reader user on
   * an empty combobox never learned that an unresolvable id was still in the
   * form and still being submitted. Now wired through the optional
   * `describedBy` prop `picker.tsx` gained for exactly this.
   */
  it("wires the alert-queue orphan note as the picker's accessible description", async () => {
    const UNRESOLVABLE = "b0000000-0000-0000-0000-00000000dead";
    calls.getHealthSettings.mockResolvedValue({
      ...HEALTH_SETTINGS_FIXTURE,
      default_alert_queue_id: UNRESOLVABLE,
    });
    calls.listTicketQueues.mockResolvedValue([QUEUE_FIXTURE]);
    renderSettings();
    await openTab();

    const input = await screen.findByLabelText("Default alert queue");
    await screen.findByText(/no longer exists/i);
    expect(input).toHaveAccessibleDescription(/no longer exists/i);
  });

  it("wires the ticket-category orphan note as the picker's accessible description", async () => {
    const UNRESOLVABLE = "c0000000-0000-0000-0000-00000000dead";
    calls.getHealthSettings.mockResolvedValue({
      ...HEALTH_SETTINGS_FIXTURE,
      default_ticket_category_id: UNRESOLVABLE,
    });
    calls.listTicketCategories.mockResolvedValue([CATEGORY_FIXTURE]);
    renderSettings();
    await openTab();

    const input = await screen.findByLabelText("Default ticket category");
    await screen.findByText(/no longer exists/i);
    expect(input).toHaveAccessibleDescription(/no longer exists/i);
  });

  /**
   * Review finding 2, the empty-list branch: the "no queues exist yet" hint
   * must share the picker's cell too, not only the orphan note.
   */
  it("keeps the alert-queue picker and its empty-org hint in one grid cell", async () => {
    calls.listTicketQueues.mockResolvedValue([]);
    renderSettings();
    await openTab();

    const input = await screen.findByLabelText("Default alert queue");
    const pickerRoot = input.closest(".picker") as HTMLElement;
    const cell = pickerRoot.parentElement as HTMLElement;
    expect(cell.parentElement).toHaveClass("form-grid");

    const hint = screen.getByText(/No ticket queues exist yet/);
    expect(hint.parentElement).toBe(cell);
  });

  // Review finding 4: `listTicketQueues()`/`listTicketCategories()` default
  // to `include_inactive=false`, so a queue that merely went inactive (not
  // deleted) was absent from the options, declared "no longer exists", and
  // could never be re-selected. Simulates the server's own filtering: the
  // inactive fixture is served only when `include_inactive` is actually
  // requested.
  it("does not declare a stored queue gone merely because it went inactive", async () => {
    const user = userEvent.setup();
    const inactiveQueue = { ...QUEUE_FIXTURE, active: false };
    calls.getHealthSettings.mockResolvedValue({
      ...HEALTH_SETTINGS_FIXTURE,
      default_alert_queue_id: QUEUE_FIXTURE.id,
    });
    calls.listTicketQueues.mockImplementation((includeInactive?: boolean) =>
      Promise.resolve(includeInactive ? [inactiveQueue] : []),
    );
    renderSettings();
    await openTab();

    const picker = await screen.findByLabelText<HTMLInputElement>("Default alert queue");
    expect(picker).toHaveValue("Escalations");
    expect(screen.queryByText(/no longer exists/i)).not.toBeInTheDocument();

    await user.click(picker);
    expect(await screen.findByText(/inactive/i)).toBeInTheDocument();
  });

  it("does not declare a stored ticket category gone merely because it went inactive", async () => {
    const inactiveCategory = { ...CATEGORY_FIXTURE, active: false };
    calls.getHealthSettings.mockResolvedValue({
      ...HEALTH_SETTINGS_FIXTURE,
      default_ticket_category_id: CATEGORY_FIXTURE.id,
    });
    calls.listTicketCategories.mockImplementation((includeInactive?: boolean) =>
      Promise.resolve(includeInactive ? [inactiveCategory] : []),
    );
    renderSettings();
    await openTab();

    expect(
      await screen.findByLabelText<HTMLInputElement>("Default ticket category"),
    ).toHaveValue("Incident");
    expect(screen.queryByText(/no longer exists/i)).not.toBeInTheDocument();
  });

  // Review finding 2 (a real cache-collision, not a hypothetical one):
  // `HealthTab` fetches queues with `includeInactive: true`, `NotificationsTab`
  // with the default `false`. `../../../App.tsx`'s module-level query client
  // keeps an unmounted tab's cache entry alive for the default `gcTime` (five
  // minutes) — well past `SettingsPage` switching tabs — so without
  // `includeInactive` in the query key, the two hooks would share one cache
  // entry and a Health→Notifications switch would serve Health's
  // inactive-inclusive list into Notifications' Queue scope. `renderSettings`
  // uses a fresh `QueryClient` per test, but its `gcTime` still defaults to
  // five minutes, so the same collision reproduces here.
  it("does not leak Health's inactive-inclusive queue list into Notifications' Queue scope", async () => {
    const user = userEvent.setup();
    const ghostQueue = { ...QUEUE_FIXTURE, id: "queue-ghost", name: "Ghost Queue", active: false };

    // The active-only ("false") fetch is held open deliberately: if the two
    // tabs shared a cache entry, Notifications would already be showing
    // Health's cached (inactive-inclusive) data before this ever resolves —
    // proving the leak does not depend on which fetch wins a race.
    let resolveActiveOnly: (queues: (typeof QUEUE_FIXTURE)[]) => void = () => {};
    const activeOnlyList = new Promise<(typeof QUEUE_FIXTURE)[]>((resolve) => {
      resolveActiveOnly = resolve;
    });
    calls.listTicketQueues.mockImplementation((includeInactive?: boolean) =>
      includeInactive ? Promise.resolve([QUEUE_FIXTURE, ghostQueue]) : activeOnlyList,
    );

    renderSettings();
    await openTab();
    // Health's inactive-inclusive fetch must have landed (and populated its
    // own cache entry) before switching tabs.
    await screen.findByLabelText("Default alert queue");
    expect(calls.listTicketQueues).toHaveBeenCalledWith(true);

    await user.click(await screen.findByRole("tab", { name: "Notifications" }));

    // A genuinely separate, active-only fetch was made for Notifications —
    // not served from Health's cache entry by coincidence.
    await waitFor(() => expect(calls.listTicketQueues).toHaveBeenCalledWith(false));

    const routing = (await screen.findByRole("heading", { name: "Routing", level: 3 })).closest(
      "section",
    ) as HTMLElement;
    await user.click(within(routing).getByLabelText("Queue scope"));
    // The active-only fetch has not resolved yet. A colliding cache key would
    // already have data here — Health's inactive-inclusive list — and would
    // show "Ghost Queue" instead of "Loading…".
    expect(within(routing).getByText("Loading…")).toBeInTheDocument();
    expect(within(routing).queryByText("Ghost Queue")).not.toBeInTheDocument();

    resolveActiveOnly([QUEUE_FIXTURE]);
    await waitFor(() =>
      expect(within(routing).queryByText("Loading…")).not.toBeInTheDocument(),
    );
    expect(within(routing).queryByText("Ghost Queue")).not.toBeInTheDocument();
  });
});
