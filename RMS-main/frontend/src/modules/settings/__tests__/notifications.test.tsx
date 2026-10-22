// Notifications tab — channels and rules.

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../lib/api";
import { EVENT_TYPE_LABELS, RECIPIENT_KIND_LABELS } from "../../notification/labels";
import { EVENT_TYPES } from "../../notification/types";
import {
  CHANNEL_FIXTURE,
  PLANTED_SECRET,
  WEBHOOK_CHANNEL_FIXTURE,
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
  await user.click(await screen.findByRole("tab", { name: "Notifications" }));
}

/**
 * Click one chip inside a named `ChipsField` group — `Kind` and `Recipients`
 * are both fixed, small enumerations now (docs/superpowers/specs/
 * 2026-09-10-settings-ui-rework-design.md §4), so this replaces
 * `user.selectOptions` for both the same way `budget.test.tsx` already
 * clicks into "Fiscal year starts in".
 */
async function clickChip(
  user: ReturnType<typeof userEvent.setup>,
  groupName: string,
  chipName: string,
  scope: HTMLElement = document.body,
): Promise<void> {
  const group = within(scope).getByRole("group", { name: groupName });
  await user.click(within(group).getByRole("button", { name: chipName }));
}

describe("Settings — Notifications tab", () => {
  // Browser review at 390x844: the NAME cell's `<input>` measured 24px wide
  // against 125-147px of channel names, so three channels rendered as three
  // empty boxes (measured with Playwright against `?__fixtures`, not
  // reproducible in jsdom, which has no layout). `Th`'s `min-width` is what
  // fixes it in a real viewport; this is the regression guard that a later
  // edit cannot drop the floor without going red here, even though it cannot
  // itself prove the overflow is gone.
  it("gives the channel table's Name column a width floor, so a narrow viewport cannot collapse it to empty boxes", async () => {
    renderSettings();
    await openTab();

    const nameHeader = await screen.findByRole("columnheader", { name: "Name" });
    expect(nameHeader.style.minWidth).toBe("200px");

    const tokenHeader = screen.getByRole("columnheader", { name: "New token" });
    expect(tokenHeader.style.minWidth).toBe("280px");
  });

  it("loads the current channels and rules into editable controls", async () => {
    renderSettings();
    await openTab();
    expect(
      await screen.findByLabelText<HTMLInputElement>("Name for Operations mailbox"),
    ).toHaveValue("Operations mailbox");
    expect(screen.getByLabelText("Active for Operations mailbox")).toBeChecked();
    expect(screen.getByText("stored in vault")).toBeInTheDocument();
    // The rules table's own cell — "Ticket assigned" (the display name for
    // `ticket.assigned`) is also an option in the "add a rule" event-type
    // select, so the row is located by its table cell.
    const eventCell = screen
      .getAllByText("Ticket assigned")
      .find((element) => element.tagName === "TD");
    expect(eventCell, "the existing rule must be listed").toBeDefined();
  });

  // The direction doc (docs/design/settings-language.md, "Notifications")
  // names this leak explicitly: `rule.event_type` printed raw, in `mono`, so
  // it read as a database column twice over. Same fix for `recipient_kind`.
  it("shows the rules table's display name for the event type and recipients, never the wire value, and drops the mono styling", async () => {
    renderSettings();
    await openTab();

    const eventCell = screen
      .getAllByText(EVENT_TYPE_LABELS["ticket.assigned"])
      .find((element) => element.tagName === "TD") as HTMLElement;
    expect(eventCell).toBeInTheDocument();
    expect(eventCell).not.toHaveClass("mono");
    // The raw wire value must not appear anywhere in that cell's text.
    expect(eventCell.textContent).not.toContain("ticket.assigned");

    const recipientCell = screen
      .getAllByText(RECIPIENT_KIND_LABELS.queue_members)
      .find((element) => element.tagName === "TD") as HTMLElement;
    expect(recipientCell).toBeInTheDocument();
    expect(recipientCell.textContent).not.toContain("queue_members");
  });

  // Design review, casing drift: `channel.kind` printed raw in the KIND
  // column (`webhook`/`email`, lowercase) while the create form's own chips,
  // two inches below in the same tab, spell the identical values `Webhook`/
  // `Email`. `CHANNEL_FIXTURE.kind` is `"email"`, so this pins the column
  // against the Title Case the chips already use, not against the wire token.
  it("prints the channel's kind in the same Title Case as the create form's chips, not the raw wire token", async () => {
    renderSettings();
    await openTab();

    const nameCell = await screen.findByLabelText<HTMLInputElement>("Name for Operations mailbox");
    const row = nameCell.closest("tr") as HTMLElement;
    expect(within(row).getByText("Email")).toBeInTheDocument();
    expect(within(row).queryByText("email")).not.toBeInTheDocument();
  });

  // Fallback runtime proof of exhaustiveness (the strongest form -- adding a
  // hypothetical event type to `EVENT_TYPES` without a matching entry in
  // `EVENT_TYPE_LABELS` fails `tsc --noEmit` with TS2741 -- was verified by
  // hand and is not re-asserted here, since a persisted test cannot break the
  // build without breaking CI). This instead pins, at runtime, that every
  // event type this client knows about actually renders as ITS OWN label
  // rather than silently falling back to the wire value -- the regression the
  // exhaustive typing exists to prevent.
  it("gives every known event type a display label distinct from its own wire value", () => {
    for (const type of EVENT_TYPES) {
      const label = EVENT_TYPE_LABELS[type];
      expect(label, `${type} has no label`).toBeTruthy();
      expect(label, `${type} rendered as itself`).not.toBe(type);
    }
  });

  // Glossary, `docs/design/settings-language.md`: "relay" -> "mail server",
  // everywhere. The Email tab already made this move (`email.test.tsx`);
  // this pins the same word on this tab's own subtitle.
  it("calls the SMTP destination a mail server, not a relay", async () => {
    renderSettings();
    await openTab();

    expect(screen.getByText(/webhook, or mail server/i)).toBeInTheDocument();
    expect(screen.queryByText(/mail relay/i)).not.toBeInTheDocument();
  });

  it("puts 'optional' in the description, not in the label, for both credential fields", async () => {
    const user = userEvent.setup();
    renderSettings();
    await openTab();

    // Webhook is the default kind: the bearer token field is already showing.
    const bearer = await screen.findByLabelText<HTMLInputElement>("Bearer token");
    expect(bearer).toHaveAccessibleDescription("Optional");

    await clickChip(user, "Kind", "Email");
    const smtpPassword = await screen.findByLabelText<HTMLInputElement>("Password");
    expect(smtpPassword).toHaveAccessibleDescription("Optional");
  });

  it("PATCHes an edited channel, shows success, and refetches", async () => {
    const user = userEvent.setup();
    renderSettings();
    await openTab();

    const name = await screen.findByLabelText<HTMLInputElement>("Name for Operations mailbox");
    await user.clear(name);
    await user.type(name, "Ops mailbox");
    calls.listNotificationChannels.mockResolvedValue([
      {
        id: "c1",
        kind: "email",
        name: "Ops mailbox",
        config: {},
        active: true,
        has_credential: true,
        created_at: "2026-08-01T00:00:00Z",
      },
    ]);

    const row = name.closest("tr");
    await user.click(within(row as HTMLElement).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(calls.updateNotificationChannel).toHaveBeenCalledTimes(1));
    expect(calls.updateNotificationChannel).toHaveBeenCalledWith("c1", {
      name: "Ops mailbox",
      active: true,
      // Blank secret means "keep the stored one" — OMITTED, never sent as "".
      secret: undefined,
    });
    expect(await screen.findByRole("status")).toHaveTextContent("Saved.");
    await waitFor(() =>
      expect(calls.listNotificationChannels.mock.calls.length).toBeGreaterThan(1),
    );
  });

  it("sends a replacement credential only when one was typed", async () => {
    const user = userEvent.setup();
    calls.listNotificationChannels.mockResolvedValue([WEBHOOK_CHANNEL_FIXTURE]);
    renderSettings();
    await openTab();

    const secret = await screen.findByLabelText<HTMLInputElement>("New token for Ops chat");
    await user.type(secret, "new-token");
    const row = secret.closest("tr");
    await user.click(within(row as HTMLElement).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(calls.updateNotificationChannel).toHaveBeenCalledTimes(1));
    expect(calls.updateNotificationChannel).toHaveBeenCalledWith("c2", {
      name: "Ops chat",
      active: true,
      secret: "new-token",
    });
  });

  it("a failed save is unmistakable: the server's error is shown and success is not claimed", async () => {
    const user = userEvent.setup();
    renderSettings();
    await openTab();

    const name = await screen.findByLabelText<HTMLInputElement>("Name for Operations mailbox");
    await user.type(name, " (renamed)");
    calls.updateNotificationChannel.mockRejectedValue(
      new ApiError(422, JSON.stringify({ detail: "name must be unique in this organization" })),
    );

    const row = name.closest("tr");
    await user.click(within(row as HTMLElement).getByRole("button", { name: "Save" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("name must be unique in this organization");
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

  it("never renders a credential: the box is write-only and empty, and config is never echoed", async () => {
    calls.listNotificationChannels.mockResolvedValue([CHANNEL_FIXTURE, WEBHOOK_CHANNEL_FIXTURE]);
    renderSettings();
    await openTab();
    const secret = await screen.findByLabelText<HTMLInputElement>("New token for Ops chat");
    // Invariant 3. The fixture's channel `config` carries a planted token,
    // which is what a server bug (or a config key the backend's own
    // `_reject_secret_keys` missed) would look like.
    expect(document.body.textContent ?? "").not.toContain(PLANTED_SECRET);
    expect(secret.value).toBe("");
    expect(secret.type).toBe("password");
    expect(secret.placeholder).toBe("Leave blank to keep the current one");
    // Existence, never the value.
    expect(screen.getAllByText("stored in vault")).toHaveLength(2);
  });

  // docs/design/settings-language.md, "Notifications": column `Replace
  // credential` -> `New token`; button `Test send` -> `Send a test`.
  it("renames the credential column to 'New token' and the test button to 'Send a test'", async () => {
    calls.listNotificationChannels.mockResolvedValue([WEBHOOK_CHANNEL_FIXTURE]);
    renderSettings();
    await openTab();

    expect(
      await screen.findByRole("columnheader", { name: "New token" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Replace credential" })).not.toBeInTheDocument();

    const secret = screen.getByLabelText<HTMLInputElement>("New token for Ops chat");
    const row = secret.closest("tr") as HTMLElement;
    expect(within(row).getByRole("button", { name: "Send a test" })).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: "Test send" })).not.toBeInTheDocument();
  });

  // Defect 2 (lang-notifications-reconcile report): the column header and
  // placeholder were renamed to "New token" / "Leave blank to keep the
  // current one" (docs/design/settings-language.md, "Notifications"), but the
  // row editor's `aria-label` was left reading "Replace credential for
  // <channel>" -- a screen-reader user heard a name a sighted user no longer
  // saw anywhere on screen. The accessible name must track the visible
  // column, while still disambiguating the row by channel name the way
  // "Name for <channel>" and "Active for <channel>" already do.
  it("names the New token field's accessible name after the visible column, not the old 'Replace credential' wording", async () => {
    calls.listNotificationChannels.mockResolvedValue([WEBHOOK_CHANNEL_FIXTURE]);
    renderSettings();
    await openTab();

    expect(
      await screen.findByLabelText<HTMLInputElement>("New token for Ops chat"),
    ).toHaveAttribute("placeholder", "Leave blank to keep the current one");
    expect(screen.queryByLabelText("Replace credential for Ops chat")).not.toBeInTheDocument();
  });

  it("sends an email channel's sign-in to the channel page instead of a one-box token", async () => {
    // A mail sign-in is a username and password, or three Microsoft 365
    // values; a single box here stored a bare string the sender could not use.
    renderSettings();
    await openTab();

    await screen.findByLabelText("Name for Operations mailbox");
    expect(screen.queryByLabelText("New token for Operations mailbox")).not.toBeInTheDocument();
    expect(screen.getByText(/Change the mail sign-in under Notifications/)).toBeInTheDocument();
  });

  it("disables every write, with the reason, for a reader who lacks notification.admin", async () => {
    installDefaults(["notification.read"]);
    calls.listNotificationChannels.mockResolvedValue([CHANNEL_FIXTURE, WEBHOOK_CHANNEL_FIXTURE]);
    renderSettings();
    await openTab();

    expect(await screen.findByLabelText("Name for Operations mailbox")).toBeDisabled();
    expect(screen.getByLabelText("New token for Ops chat")).toBeDisabled();
    expect(screen.getByLabelText("Active for Operations mailbox")).toBeDisabled();
    // `Kind` and `Recipients` are chip groups now — each chip is disabled
    // individually (`Chips` has no group-level disabled attribute of its
    // own to assert on), the same shape `budget.test.tsx` and
    // `email.test.tsx` check for their own chip fields.
    for (const button of within(screen.getByRole("group", { name: "Kind" })).getAllByRole("button")) {
      expect(button).toBeDisabled();
    }
    for (const button of within(screen.getByRole("group", { name: "Recipients" })).getAllByRole(
      "button",
    )) {
      expect(button).toBeDisabled();
    }
    expect(screen.getByRole("button", { name: "Create channel" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Create rule" })).toBeDisabled();
    expect(screen.getAllByText(/requires the notification\.admin permission/i).length).toBeGreaterThan(
      0,
    );
  });

  it("creates a rule through the rules endpoint with the routing that was chosen", async () => {
    const user = userEvent.setup();
    renderSettings();
    await openTab();

    await user.selectOptions(await screen.findByLabelText("Event type"), "ticket.resolved");
    await user.selectOptions(screen.getByLabelText("Channel"), "c1");
    await clickChip(user, "Recipients", RECIPIENT_KIND_LABELS.assignee);
    await user.click(screen.getByRole("button", { name: "Create rule" }));

    await waitFor(() => expect(calls.createNotificationRule).toHaveBeenCalledTimes(1));
    expect(calls.createNotificationRule).toHaveBeenCalledWith({
      event_type: "ticket.resolved",
      channel_id: "c1",
      recipient_kind: "assignee",
      queue_id: null,
    });
    expect(await screen.findByRole("status")).toHaveTextContent("Rule created.");
  });

  // The other half of the same leak: `EVENT_TYPES` entries and recipient
  // kinds used as OPTION TEXT (`:558`, `:589` in the direction doc), so the
  // operator picked `queue_members` from a menu. Event type is still a
  // `<select>` (it is server-fed in the general case -- `RuleCreate.
  // event_type` is validated against the server's own registry, not this
  // client's `EVENT_TYPES` mirror), so that half still pins `<option>`
  // text against `value`. Recipients moved to chips (`ChipsField`, §4 of
  // the design doc): a chip's own visible text IS what gets sent, via
  // `recipientOptions[at]`, so what this half pins instead is that the wire
  // token itself is never what is printed on the button.
  it("offers plain-language options in the event-type select and recipients chips while still sending wire values", async () => {
    renderSettings();
    await openTab();

    const eventSelect = await screen.findByLabelText<HTMLSelectElement>("Event type");
    const resolvedOption = within(eventSelect).getByRole("option", {
      name: "Ticket resolved",
    }) as HTMLOptionElement;
    expect(resolvedOption.value).toBe("ticket.resolved");
    expect(within(eventSelect).queryByRole("option", { name: "ticket.resolved" })).not.toBeInTheDocument();

    const recipientsGroup = screen.getByRole("group", { name: "Recipients" });
    expect(
      within(recipientsGroup).getByRole("button", { name: "Everyone in the queue" }),
    ).toBeInTheDocument();
    expect(
      within(recipientsGroup).queryByRole("button", { name: "queue_members" }),
    ).not.toBeInTheDocument();
  });

  it("scopes a rule to a queue chosen by name, through the picker that replaced the queue-scope select", async () => {
    const user = userEvent.setup();
    calls.listTicketQueues.mockResolvedValue([QUEUE_FIXTURE]);
    renderSettings();
    await openTab();

    await user.selectOptions(await screen.findByLabelText("Event type"), "ticket.resolved");
    await user.selectOptions(screen.getByLabelText("Channel"), "c1");
    await user.click(screen.getByLabelText("Queue scope"));
    await user.click(await screen.findByRole("option", { name: /Escalations/ }));
    await user.click(screen.getByRole("button", { name: "Create rule" }));

    await waitFor(() => expect(calls.createNotificationRule).toHaveBeenCalledTimes(1));
    expect(calls.createNotificationRule).toHaveBeenCalledWith(
      expect.objectContaining({ queue_id: QUEUE_FIXTURE.id }),
    );
  });

  it("leaves the degraded raw queue-id box alone when queues cannot be listed", async () => {
    calls.listTicketQueues.mockRejectedValue(new ApiError(403, "forbidden"));
    renderSettings();
    await openTab();

    // Review finding 7: the parenthetical used to be the whole accessible
    // NAME ("Queue id (blank for any queue)"). `HealthTab.tsx:313-319` and
    // `:359-365` gave their structurally identical degrade branches the
    // full `label` + `help` treatment; this box now matches.
    const box = await screen.findByLabelText("Queue id");
    expect(box).toBeInTheDocument();
    expect(box).toHaveAccessibleDescription(/blank for any queue/i);
    expect(screen.queryByLabelText("Queue scope")).not.toBeInTheDocument();
  });

  // Review finding 3: `queues.error != null` ALSO degraded on a failed
  // REFETCH that had a good list cached — a transient 500 on the background
  // refetch that fires on remount tore down a working picker over nothing,
  // even though `queues.data` still held the last good list. Same fix,
  // same probe as `HealthTab.tsx` (see `health.test.tsx`): load with a
  // resolving list, remount with the mock now rejecting.
  it("keeps the Queue scope picker showing a cached list after a failed refetch, not a raw id box", async () => {
    const user = userEvent.setup();
    // First fetch (Notifications' initial mount) succeeds; every later call
    // fails.
    calls.listTicketQueues.mockResolvedValueOnce([QUEUE_FIXTURE]);
    renderSettings();
    await openTab();

    await user.click(await screen.findByLabelText("Queue scope"));
    expect(await screen.findByRole("option", { name: /Escalations/ })).toBeInTheDocument();

    calls.listTicketQueues.mockRejectedValue(new ApiError(500, "internal error"));

    // Unmount Notifications, remount it — `SettingsPage` renders only the
    // active tab, so this is a genuine remount, and TanStack Query refetches
    // a stale query on mount by default. `HealthTab` also calls
    // `useTicketQueues` (a distinct, uncached key: `includeInactive: true`),
    // so this exercises the real path rather than a shortcut through it.
    // A `RegExp`, not the exact string "Health": the nav now appends "Set
    // up" / "Not set up" to this tab's accessible name (Task 7), and
    // `getByRole`'s `name` matches a string by whole-string equality
    // regardless of `exact` (text-query-only) -- so an exact string here
    // would miss the tab as soon as its own settings query answers
    // `configured`.
    await user.click(await screen.findByRole("tab", { name: /^Health/ }));
    await openTab();

    // The remount's background refetch must have actually run and failed —
    // not just have been skipped — before this counts as proof.
    await waitFor(() => expect(calls.listTicketQueues).toHaveBeenCalledTimes(3));

    await user.click(await screen.findByLabelText("Queue scope"));
    expect(await screen.findByRole("option", { name: /Escalations/ })).toBeInTheDocument();
    expect(screen.queryByLabelText("Queue id")).not.toBeInTheDocument();
  });

  // Review finding 2: `emptyHint` is unreachable at this call site because
  // the synthetic "Any queue" entry means the picker's own `options.length`
  // is never 0. Probed on a zero-queue org (the harness's own default
  // fixture): the hint never showed.
  it("tells the operator where queues come from under Queue scope when the org has none", async () => {
    calls.listTicketQueues.mockResolvedValue([]);
    renderSettings();
    await openTab();

    await screen.findByLabelText("Queue scope");
    expect(screen.getByText(/No ticket queues exist yet\./)).toBeInTheDocument();
    // Where they come from is a link, not a menu path to follow by hand.
    expect(screen.getAllByRole("link", { name: "Create one in the service desk settings" })[0]).toHaveAttribute(
      "href",
      "/service-desk/admin",
    );
  });

  /**
   * Review finding 2. `Queue scope` and its "no queues exist yet" hint used
   * to be separate direct children of `.form-grid` — two grid items — which
   * is what broke the Routing group's 2×2 (`git show 7ecccea` had a plain
   * `Field`+`<select>` there, not a spanning picker with an orphaned
   * sibling). Wrapped in one cell, `Recipients` and `Queue scope` sit
   * side by side again, and this pins the DOM fact rather than a
   * screenshot.
   */
  it("keeps the Queue scope picker and its empty-org hint in one grid cell", async () => {
    calls.listTicketQueues.mockResolvedValue([]);
    renderSettings();
    await openTab();

    const input = await screen.findByLabelText("Queue scope");
    const pickerRoot = input.closest(".picker") as HTMLElement;
    const cell = pickerRoot.parentElement as HTMLElement;
    expect(cell.parentElement).toHaveClass("form-grid");

    const hint = screen.getByText(/No ticket queues exist yet/);
    expect(hint.parentElement).toBe(cell);

    // The tidy 2×2 the wrap restores: `Recipients` immediately precedes the
    // wrapper as a grid sibling, so the two land in the same row.
    const recipients = screen.getByLabelText("Recipients");
    expect(recipients.closest(".field")?.nextElementSibling).toBe(cell);
  });
});

/**
 * Both forms on this tab are grouped by purpose: what a channel IS
 * (`Channel`), and where an event GOES (`Routing`). Each group is an `h3`
 * under its own panel's `h2` — "Add a channel" and "Notification rules" —
 * so the outline never jumps a level.
 */
describe("Settings — Notifications, grouped", () => {
  it("groups the channel form and the rule form by what they are for", async () => {
    const user = userEvent.setup();
    renderSettings();
    await openTab();

    const channel = (await screen.findByRole("heading", { name: "Channel", level: 3 })).closest(
      "section",
    ) as HTMLElement;
    for (const field of ["Kind", "Name", "URL", "Bearer token"]) {
      expect(within(channel).getByLabelText(field)).toBeInTheDocument();
    }

    // The mail-relay half of the same group appears only for `email`, and it
    // belongs to the same group when it does.
    await clickChip(user, "Kind", "Email", channel);
    for (const field of ["SMTP host", "SMTP port", "From address", "Sign-in", "Password"]) {
      expect(within(channel).getByLabelText(field)).toBeInTheDocument();
    }

    const routing = (await screen.findByRole("heading", { name: "Routing", level: 3 })).closest(
      "section",
    ) as HTMLElement;
    for (const field of ["Event type", "Channel", "Recipients", "Queue scope"]) {
      expect(within(routing).getByLabelText(field)).toBeInTheDocument();
    }
  });

  // Regrouping must not turn a disabled control into a hidden one.
  it("still renders the grouped fields, disabled, for a reader without notification.admin", async () => {
    installDefaults(["notification.read"]);
    renderSettings();
    await openTab();

    const channel = (await screen.findByRole("heading", { name: "Channel", level: 3 })).closest(
      "section",
    ) as HTMLElement;
    expect(within(channel).getByLabelText("Name")).toBeDisabled();
    expect(
      screen.getAllByText(/requires the notification\.admin permission/i).length,
    ).toBeGreaterThan(0);

    // Review finding 3: the "Queue scope" picker had no test pinning its
    // read-only guard — `disabled={false}` here left the whole settings
    // suite green.
    const routing = (await screen.findByRole("heading", { name: "Routing", level: 3 })).closest(
      "section",
    ) as HTMLElement;
    expect(within(routing).getByLabelText("Queue scope")).toBeDisabled();
  });

  /**
   * THE GUARD DERIVES ITS OWN SUBJECT, RATHER THAN NAMING ONE. The test
   * above pins WHICH group the channel form's own fields belong to; a
   * hardcoded list like that can never notice a field nobody enumerated —
   * which is exactly how `HealthTab` shipped grouped 3-of-11 elsewhere in
   * this cycle. So this queries every labelled control the tab actually
   * rendered and asserts each one sits inside a `section.field-group`, with
   * one documented exception below.
   *
   * BOTH `Kind` ARMS ARE SUBJECT TO THE GUARD, NOT JUST THE DEFAULT ONE.
   * `newKind` starts as `"webhook"`, so a guard that only ever ran once
   * would render — and check — the webhook arm and never the five-field,
   * `email` arm at all: a control that ships ungrouped in that branch alone
   * would pass silently forever. So the loop below selects each kind in
   * turn and re-runs the same two checks against whatever is on screen,
   * which is also why the checks live inside the loop rather than after it.
   */
  it("keeps every labelled control of the tab inside a group, in both Kind arms, except the channel table's own row editors", async () => {
    const user = userEvent.setup();
    renderSettings();
    await openTab();

    // Wait for the existing-channels table to have loaded: its row editors
    // are this tab's one documented exception, and the guard needs them to
    // exist so it excludes them by name rather than by accident.
    await screen.findByLabelText(`Name for ${CHANNEL_FIXTURE.name}`);

    // The existing-channels table edits each row through its own
    // `aria-label` ("Name for <channel>", "Active for <channel>", "New
    // token for <channel>") — a table cell, not a `Field` inside a
    // `FieldGroup`. It is a different UI pattern (a data table, editable in
    // place) from the "grouped by what they are for" form fields, and it has
    // its own dedicated coverage above ("PATCHes an edited channel", "sends
    // a replacement credential only when one was typed", "disables every
    // write..."). Named here explicitly, by exact label, so a stray field
    // added anywhere else on this tab still fails below.
    const rowEditorExceptions = new Set([
      `Name for ${CHANNEL_FIXTURE.name}`,
      `Active for ${CHANNEL_FIXTURE.name}`,
      `New token for ${CHANNEL_FIXTURE.name}`,
    ]);

    const KIND_CHIP_LABEL: Record<"webhook" | "email", string> = {
      webhook: "Webhook",
      email: "Email",
    };
    for (const kind of ["webhook", "email"] as const) {
      await clickChip(user, "Kind", KIND_CHIP_LABEL[kind]);
      const panel = screen.getByRole("tabpanel");

      const controls = within(panel).getAllByLabelText(/.+/);
      const checked = controls.filter(
        (control) => !rowEditorExceptions.has(control.getAttribute("aria-label") ?? ""),
      );
      // More than just the documented exceptions excluded, or the loop below proves nothing.
      expect(checked.length).toBeGreaterThan(0);
      for (const control of checked) {
        expect(
          control.closest("section.field-group"),
          `[kind=${kind}] ${describeControl(control)} is not inside any field-group`,
        ).not.toBeNull();
      }

      // A control that carries no accessible name at all — a `<span>`
      // caption next to an input, not a `<label>` — is invisible to
      // `getAllByLabelText` above; it is not merely unasserted, it is
      // unseen. `Field` (and the raw `.field`/`.field-wrap` shape a few
      // tabs use for a caption-only control) is what every grouped or
      // ungrouped control renders through, named or not, so this checks it
      // directly rather than through the accessible-name query above.
      const wrapped = Array.from(panel.querySelectorAll(".field, .field-wrap"));
      expect(wrapped.length).toBeGreaterThan(0);
      for (const el of wrapped) {
        expect(
          el.closest("section.field-group"),
          `[kind=${kind}] ${describeControl(el)} is not inside any field-group`,
        ).not.toBeNull();
      }
    }
  });
});
