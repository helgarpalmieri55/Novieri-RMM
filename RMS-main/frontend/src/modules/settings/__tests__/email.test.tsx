/**
 * Settings → Email — `GET/PUT /api/v1/settings/email`, `POST .../email/test`,
 * and the Users tab banner that depends on it.
 *
 * THIS TAB IS THE ANSWER TO A REAL AFTERNOON. The operator went looking for
 * SMTP configuration inside their own product and had to be told it lives in a
 * different one's admin console. Everything below is about that tab being
 * trustworthy once they find it here instead.
 *
 * THE PASSWORD IS THE PART TO GET RIGHT, and it is a FORM problem before it is
 * a server problem. The server never returns one — so the box is always empty,
 * "leave blank to keep it" describes exactly what an omitted field does, and
 * the PUT must not carry a `password` key the operator did not type. A form
 * that sent `password: ""` for an untouched box would blank a working
 * credential the first time somebody corrected the port, and the only symptom
 * would be invitations that stop arriving.
 *
 * THE TEST SEND REPORTS WHAT HAPPENED AND NOT MORE. A relay can accept a
 * message and drop it silently; this platform's own runbook says so. A green
 * tick that only proves the relay answered is the failure mode, so the success
 * message names the limit.
 *
 * THE BANNER IS TESTED IN BOTH STATES. A banner that is always there is
 * wallpaper, and a banner that is never there is why an operator finds out
 * about unconfigured email from an invitation that never arrived.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../lib/api";
import { EmailTab } from "../EmailTab";
import {
  ALL_PERMISSIONS,
  EMAIL_SETTINGS_FIXTURE,
  EMAIL_UNCONFIGURED_FIXTURE,
  PLANTED_SECRET,
  apiClient,
  calls,
  describeControl,
  installDefaults,
  renderSettings,
} from "./harness";

// The OIDC client is not under test and builds a UserManager (with a
// silent-renew timer) at import. Only the token source and the in-flow
// credential action are needed here.
vi.mock("../../../lib/auth", () => ({
  accessToken: () => "test-token",
  startAccountAction: vi.fn(),
}));

vi.mock("../../../lib/ApiContext", () => ({ useApi: () => apiClient() }));

beforeEach(() => {
  installDefaults();
});

async function openEmail() {
  const user = userEvent.setup();
  renderSettings();
  // A `RegExp`, not the exact string "Email": the nav now appends "Set up"
  // / "Not set up" to this tab's accessible name (Task 7), and `getByRole`'s
  // `name` matches a string by whole-string equality regardless of `exact`
  // (text-query-only) -- so an exact string here would miss the tab as soon
  // as its own settings query answers `configured`.
  await user.click(await screen.findByRole("tab", { name: /^Email/ }));
  return user;
}

async function openUsers() {
  const user = userEvent.setup();
  renderSettings();
  await user.click(await screen.findByRole("tab", { name: "Users" }));
  return user;
}

describe("Settings — Email tab", () => {
  it("loads the saved relay settings", async () => {
    await openEmail();

    expect(await screen.findByLabelText<HTMLInputElement>("Mail server")).toHaveValue(
      "smtp.internal",
    );
    expect(screen.getByLabelText<HTMLInputElement>("Port")).toHaveValue("587");
    expect(screen.getByLabelText<HTMLInputElement>("From address")).toHaveValue(
      "itms@toeshee.com",
    );
    // Encryption is a chip group now, not a `<select>` — see "Settings —
    // Email tab, Encryption as chips" below for the dedicated coverage.
    expect(
      within(screen.getByRole("group", { name: "Encryption" })).getByRole("button", {
        name: "StartTLS · port 587",
      }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText<HTMLInputElement>("Username")).toHaveValue("itms");
  });

  it("says the password is set, and never renders it", async () => {
    await openEmail();

    // The saved state is reported the only way it honestly can be: that one
    // exists, not what it is.
    expect(await screen.findByText(/password: set/i)).toBeInTheDocument();
    const box = screen.getByLabelText<HTMLInputElement>("Password");
    expect(box).toHaveValue("");
    expect(box).toHaveAttribute("type", "password");
    expect(document.body.textContent).not.toContain(PLANTED_SECRET);
    expect(document.body.innerHTML).not.toContain(PLANTED_SECRET);
  });

  /**
   * Review finding 1. The sentence above used to render as a `<p className="note">`
   * after the `Authentication` `FieldGroup` closed, wired to nothing — a
   * screen-reader user on the box heard "Relay password, edit" and no
   * description at all. It belongs in the box's own `help`, which is the
   * first entry on the spec's "What must not regress" list: an untouched box
   * sends nothing, and that only reads as reassurance if it is actually
   * announced.
   */
  it("announces the saved-password state as the box's own accessible description", async () => {
    await openEmail();

    const box = await screen.findByLabelText<HTMLInputElement>("Password");
    expect(box).toHaveAccessibleDescription(/password: set\. leave the box blank to keep it\./i);
  });

  it("announces the unset-password state the same way, when there is no mail server yet", async () => {
    calls.getEmailSettings.mockResolvedValue(EMAIL_UNCONFIGURED_FIXTURE);
    await openEmail();

    const box = await screen.findByLabelText<HTMLInputElement>("Password");
    expect(box).toHaveAccessibleDescription(/password: not set\./i);
  });

  /**
   * Defect: `:282` (password set) and `:283` (password not set) used to make
   * OPPOSITE claims about which system holds the SMTP password — one said
   * "this platform never stores it", the other said "identity provider".
   * `backend/app/modules/user_admin/realm_config.py` settles which is true:
   * the password is written to the Keycloak realm's own `smtpServer` block
   * (`_SMTP = "smtpServer"`, `set_smtp`), `get_smtp` strips it on every read,
   * and `kc_client.py`'s `execute-actions-email` is Keycloak itself sending
   * the mail with it. So "identity provider" was the correct system in both
   * states, and both branches must say so IDENTICALLY — not just each
   * separately passing its own regex, but both matching the very same
   * pattern, so an edit that drifts one branch's claim away from the other's
   * fails here rather than passing because nobody compared them.
   */
  const PASSWORD_CUSTODY_CLAIM =
    /identity provider.*stores it and uses it to send mail.*this platform never stores it/is;

  it("makes the identical claim about who holds the password — set branch", async () => {
    await openEmail();

    const box = await screen.findByLabelText<HTMLInputElement>("Password");
    expect(box).toHaveAccessibleDescription(PASSWORD_CUSTODY_CLAIM);
  });

  it("makes the identical claim about who holds the password — not-set branch", async () => {
    calls.getEmailSettings.mockResolvedValue(EMAIL_UNCONFIGURED_FIXTURE);
    await openEmail();

    const box = await screen.findByLabelText<HTMLInputElement>("Password");
    expect(box).toHaveAccessibleDescription(PASSWORD_CUSTODY_CLAIM);
  });

  it("saves the edited settings WITHOUT a password the operator did not type", async () => {
    const user = await openEmail();

    const host = await screen.findByLabelText<HTMLInputElement>("Mail server");
    await user.clear(host);
    await user.type(host, "smtp.new.internal");
    // What the server holds AFTER the save. The form re-reads rather than
    // trusting what was typed, so "Saved." only appears when the server's own
    // answer matches — which is the point of re-reading.
    calls.getEmailSettings.mockResolvedValue({
      ...EMAIL_SETTINGS_FIXTURE,
      host: "smtp.new.internal",
    });
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(calls.updateEmailSettings).toHaveBeenCalledTimes(1));
    const body = calls.updateEmailSettings.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(body).toMatchObject({
      host: "smtp.new.internal",
      port: 587,
      from_address: "itms@toeshee.com",
      encryption: "starttls",
      username: "itms",
    });
    // THE ASSERTION THIS FILE EXISTS FOR. An untouched box sends NOTHING, so
    // the server keeps what it holds. `password: ""` here would clear a
    // working credential.
    expect(body).not.toHaveProperty("password");
    expect(await screen.findByRole("status")).toHaveTextContent(/Saved\./);
  });

  it("sends the password when one is actually typed", async () => {
    const user = await openEmail();

    await user.type(await screen.findByLabelText("Password"), "new-relay-secret");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(calls.updateEmailSettings).toHaveBeenCalledTimes(1));
    expect(calls.updateEmailSettings.mock.calls[0]?.[0]).toMatchObject({
      password: "new-relay-secret",
    });
  });

  it("refuses to save a password with no sign-in username", async () => {
    const user = await openEmail();

    await user.clear(await screen.findByLabelText("Username"));
    await user.type(screen.getByLabelText("Password"), "orphan-secret");

    // Saving with no username CLEARS the stored credential server-side, so a
    // typed password would be discarded by a request that answered 200.
    expect(await screen.findByText(/needs a sign-in username/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(calls.updateEmailSettings).not.toHaveBeenCalled();
  });

  it("reports a test send honestly — accepted, which is not delivered", async () => {
    const user = await openEmail();

    await user.click(await screen.findByRole("button", { name: "Send a test message" }));

    await waitFor(() => expect(calls.sendTestEmail).toHaveBeenCalledTimes(1));
    // Re-pointed (glossary sweep, `docs/design/settings-language.md`: "relay"
    // → "mail server"): the panel's own field is labelled "Mail server", so
    // the outcome copy naming a "relay" was the system's word, not the
    // operator's. The claim under test — accepted is not delivered — is
    // unchanged.
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Sent to support@toeshee.com — check that mailbox. A mail server can accept a message and still drop it.",
    );
  });

  it("proposes the operator's own inbox, tagged, and sends to what the box says", async () => {
    // The identity provider refuses a sign-in account's own address, so the
    // box offers a tagged form of it that reaches the same inbox. Whatever the
    // operator leaves in the box is what the request names.
    calls.getEmailSettings.mockResolvedValue({
      ...EMAIL_SETTINGS_FIXTURE,
      suggested_test_recipient: "helgar+itms-test@toeshee.com",
    });
    const user = await openEmail();

    const box = await screen.findByLabelText("Send the test to");
    await waitFor(() => expect(box).toHaveValue("helgar+itms-test@toeshee.com"));
    await user.clear(box);
    await user.type(box, "it-team@toeshee.com");
    await user.click(screen.getByRole("button", { name: "Send a test message" }));

    await waitFor(() =>
      expect(calls.sendTestEmail).toHaveBeenCalledWith({ recipient: "it-team@toeshee.com" }),
    );
  });

  it("a failed test send shows the server's own message and claims nothing", async () => {
    calls.sendTestEmail.mockRejectedValue(
      new ApiError(502, JSON.stringify({ detail: "The identity provider refused that (HTTP 500)." })),
    );
    const user = await openEmail();

    await user.click(await screen.findByRole("button", { name: "Send a test message" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/HTTP 500/);
    expect(screen.queryByText(/^Sent/)).not.toBeInTheDocument();
  });

  /**
   * Defect: the disabled-Save hint named a "relay host" — a field the form
   * has not had since the language pass relabelled it "Mail server"
   * (`docs/design/settings-language.md` "Email": "Relay host" → "Mail
   * server"). A validation message naming a control the operator cannot find
   * on the panel is worse than no message. Reached by leaving the mail
   * server blank, which also makes the form incomplete without touching the
   * password/username pairing covered elsewhere.
   */
  it("names the field that actually exists when the form can't be saved", async () => {
    const user = await openEmail();

    await user.clear(await screen.findByLabelText<HTMLInputElement>("Mail server"));

    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(screen.getByText(/enter a mail server, a from address and a valid port/i)).toBeInTheDocument();
    expect(screen.queryByText(/relay host/i)).not.toBeInTheDocument();
  });

  it("says plainly when no relay is configured at all", async () => {
    calls.getEmailSettings.mockResolvedValue(EMAIL_UNCONFIGURED_FIXTURE);
    await openEmail();

    expect(await screen.findByText(/email is not set up/i)).toBeInTheDocument();
    // Nothing to test until something is saved: a button that can only fail is
    // worse than one that is not offered yet.
    expect(screen.getByRole("button", { name: "Send a test message" })).toBeDisabled();
  });

  it("is not offered at all without orgs.manage, and its read never fires", async () => {
    installDefaults(ALL_PERMISSIONS.filter((code) => code !== "orgs.manage"));
    renderSettings();

    // The tab is not offered at all: its read is `orgs.manage` too, so
    // rendering it would show a form that can only 403.
    await screen.findByRole("tab", { name: "Users" });
    expect(screen.queryByRole("tab", { name: "Email" })).not.toBeInTheDocument();
    expect(calls.getEmailSettings).not.toHaveBeenCalled();
  });
});

describe("Settings — the Users tab warns while email is unconfigured", () => {
  it("shows the banner when no relay is configured", async () => {
    calls.getEmailSettings.mockResolvedValue(EMAIL_UNCONFIGURED_FIXTURE);
    await openUsers();

    const banner = await screen.findByRole("alert", { name: /email/i });
    expect(
      within(banner).getByText(/Email isn't set up, so invitations can't be sent/i),
    ).toBeInTheDocument();
    // A link to the tab that fixes it, not a direction to go and find it.
    expect(within(banner).getByRole("link", { name: "Set up email" })).toHaveAttribute(
      "href",
      "/settings?tab=email",
    );
  });

  it("does NOT show it once a relay is configured", async () => {
    await openUsers();

    // The directory has rendered, so the email query has had its chance.
    await screen.findByText("casey@example.com");
    expect(screen.queryByText(/invitations can't be sent/i)).not.toBeInTheDocument();
  });
});

/**
 * THE FIELDS ARE GROUPED BY WHAT THEY ARE FOR, not merely laid out. A relay's
 * eight boxes in one undifferentiated grid is the "db table with some style"
 * the console was rebuilt to stop being: nothing on screen says the port
 * belongs to the host rather than to the sender.
 *
 * The heading is the assertion, and its LEVEL is part of it. A titled `Panel`
 * renders the `h2`; a group heading is the `h3` under it. `h4` here, or a
 * styled `div`, is the `heading-order` defect (axe, `moderate`) that the
 * harmony gate — which fails on `serious` only — would not catch.
 */
describe("Settings — Email tab, grouped", () => {
  /**
   * THIS PINS WHICH GROUP, NOT COMPLETENESS. It is the union of what used to
   * be two tests — a one-field-per-group spot check and an eight-field
   * enumeration — folded together because the second strictly subsumed the
   * first (every field the spot check named also appears in its group's
   * full list below). Renamed off "keeps every field... inside one of the
   * three groups": that name claimed a completeness the guard below is the
   * only thing that actually delivers, since this list is hand-written and
   * cannot notice a ninth field nobody enumerated — which is exactly how
   * `HealthTab` shipped grouped 3-of-11 elsewhere in this cycle.
   */
  it("puts each of the relay's known fields in the group it belongs to", async () => {
    await openEmail();

    for (const [group, fields] of [
      ["Connection", ["Mail server", "Port", "Encryption"]],
      ["Sender identity", ["From address", "Sender name", "Reply-to address"]],
      ["Authentication", ["Username", "Password"]],
    ] as const) {
      const heading = await screen.findByRole("heading", { name: group, level: 3 });
      const section = heading.closest("section") as HTMLElement;
      for (const field of fields) {
        expect(within(section).getByLabelText(field)).toBeInTheDocument();
      }
    }
  });

  /**
   * THE GUARD DERIVES ITS OWN SUBJECT, RATHER THAN NAMING ONE. The test
   * above pins WHICH group each of eight known fields belongs to; a
   * hardcoded list like that can never notice a NINTH field nobody
   * enumerated — which is exactly how `HealthTab` shipped grouped 3-of-11:
   * the inventory that built this mapping enumerated fields by hand and
   * missed the eight `NUMERIC_FIELDS` renders. So this queries every
   * labelled control the tab actually rendered (`getAllByLabelText(/.+/)` —
   * a `Field`'s wrapping `<label>`, or an explicit `aria-label` such as
   * `Chips`' `role="group"`) and asserts each one sits inside a
   * `section.field-group`. It goes red on the next field nobody grouped,
   * not on one of the eight above going missing.
   *
   * NAMED-CONTROL COVERAGE ISN'T EVERY CONTROL. `getAllByLabelText` derives
   * its subject from ACCESSIBLE NAMES, so a control with none — a `<span>`
   * caption next to an input, not a `<label>` bound to it — is invisible to
   * the loop above, not merely unasserted. `Field` (and the raw
   * `.field`/`.field-wrap` shape a couple of other tabs use for a
   * caption-only control) is what every grouped-or-not control renders
   * through regardless of whether it carries a name, so the second loop
   * checks that directly.
   */
  it("keeps every labelled control of the tab inside a group", async () => {
    await openEmail();
    // A real floor, not just "the array wasn't empty": `openEmail()` awaits
    // only the tab click, not the render past it, so a genuine load race
    // would otherwise query whatever happened to be on screen at that
    // instant. Anchoring on a known field first means the guard below sees
    // the fully-loaded form or fails here, honestly, instead of passing
    // quietly over a partial one.
    await screen.findByLabelText("Mail server");

    const panel = screen.getByRole("tabpanel");
    const controls = within(panel).getAllByLabelText(/.+/);
    // A guard that found nothing would trivially "pass" empty-handed.
    expect(controls.length).toBeGreaterThan(0);
    for (const control of controls) {
      expect(
        control.closest("section.field-group"),
        `${describeControl(control)} is not inside any field-group`,
      ).not.toBeNull();
    }

    const wrapped = Array.from(panel.querySelectorAll(".field, .field-wrap"));
    expect(wrapped.length).toBeGreaterThan(0);
    for (const el of wrapped) {
      expect(
        el.closest("section.field-group"),
        `${describeControl(el)} is not inside any field-group`,
      ).not.toBeNull();
    }
  });

  /**
   * THE NON-EDITABLE RENDER IS GROUPED BY CONSTRUCTION TOO — it is the same
   * `ChipsField` in the same `form-grid`/`FieldGroup`, just `disabled`, not
   * a second markup on the other side of a branch (that branch is gone: see
   * `ChipsField`). But `canManage` is still a prop, not a live control the
   * guard can flip mid-test the way Notifications flips `Kind`, so the only
   * way to reach the disabled render is a second, direct render (the same
   * one used below for "the read-only branch is the same chip group,
   * genuinely disabled"). Cheap enough to run the same two checks against it.
   */
  it("keeps every labelled control of the non-editable branch inside a group too", async () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <EmailTab canManage={false} />
      </QueryClientProvider>,
    );

    // The real floor: confirms the disabled render actually happened before
    // trusting an empty result below.
    const encryption = await screen.findByRole("group", { name: "Encryption" });
    expect(within(encryption).getAllByRole("button")[0]).toBeDisabled();

    const controls = screen.getAllByLabelText(/.+/);
    expect(controls.length).toBeGreaterThan(0);
    for (const control of controls) {
      expect(
        control.closest("section.field-group"),
        `${describeControl(control)} is not inside any field-group`,
      ).not.toBeNull();
    }

    const wrapped = Array.from(document.querySelectorAll(".field, .field-wrap"));
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
 * ENCRYPTION IS A CHOICE OF THREE, RENDERED AS A CHOICE. `<select>` is the
 * browser's own widget painted through the kit's box; the design draws a
 * short, fixed enumeration as `Chips`, now through the shared `ChipsField`
 * (`viz.tsx`) that closed this call site, `BudgetTab`'s and
 * `OrganizationTab`'s into one. `Chips` carries a real `disabled` state as
 * of this cycle, so the read-only branch is the SAME chip group, disabled,
 * rather than a parallel `<select disabled>` standing in for a control that
 * used to have no inert state of its own.
 */
describe("Settings — Email tab, Encryption as chips", () => {
  it("offers the three TLS choices as a group, not a menu", async () => {
    await openEmail();

    const group = await screen.findByRole("group", { name: "Encryption" });
    for (const label of [
      "StartTLS · port 587",
      "SSL/TLS · port 465",
      "None · plain connection",
    ]) {
      expect(within(group).getByRole("button", { name: label })).toBeInTheDocument();
    }
    expect(
      within(group).getByRole("button", { name: "StartTLS · port 587" }),
    ).toHaveAttribute("aria-pressed", "true");
    // The select is gone in the editable case, not merely hidden.
    expect(document.querySelector("select")).toBeNull();
  });

  it("switching the chip stages the new value, saved on submit", async () => {
    const user = await openEmail();

    const group = await screen.findByRole("group", { name: "Encryption" });
    await user.click(within(group).getByRole("button", { name: "None · plain connection" }));
    expect(
      within(group).getByRole("button", { name: "None · plain connection" }),
    ).toHaveAttribute("aria-pressed", "true");

    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls.updateEmailSettings).toHaveBeenCalledTimes(1));
    expect(calls.updateEmailSettings.mock.calls[0]?.[0]).toMatchObject({ encryption: "none" });
  });

  it("the read-only branch is the same chip group, genuinely disabled — not a parallel <select>", async () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <EmailTab canManage={false} />
      </QueryClientProvider>,
    );

    // The select is gone. There is exactly one Encryption control now, not
    // two markups behind a branch — `select` used to be reachable through
    // this same render; it no longer exists at all.
    expect(document.querySelector("select")).toBeNull();

    const group = await screen.findByRole("group", { name: "Encryption" });
    // Every chip disabled — not merely styled grey. A real `disabled`
    // attribute is what drops each one from the tab order and gets it
    // announced as unavailable; `shell/viz.test.tsx` pins the keyboard half
    // of this directly against `Chips` itself.
    for (const button of within(group).getAllByRole("button")) {
      expect(button).toBeDisabled();
    }
    // NOT asserting which chip is pressed. Email's read and write are the
    // SAME permission (`EMAIL_MANAGE`, both `orgs.manage`), so
    // `canManage={false}` is the only way to reach this branch — and it is
    // also what disables `useEmailSettings`'s query (`api.ts`'s
    // `enabled: canManage`). The group never sees the server's
    // `EMAIL_SETTINGS_FIXTURE.encryption`; it shows `encryptionOf(undefined)`'s
    // seed default, "starttls", regardless of what the fixture says.
    // Asserting a pressed chip would pin the seed default, not the loaded
    // setting — proved by the fixture's `encryption` being `"starttls"` too,
    // which made the old `<select>` assertion pass for the wrong reason.
    // Unlike `budget.test.tsx`'s `canRead`/`canManage` split, there is no
    // permission combination on this tab that loads real data into a
    // disabled control, so there is no genuine pressed state to assert here.
  });
});

/**
 * Sign-in: a password, or a Microsoft 365 application. The realm's sign-in
 * service fetches the Microsoft token itself, so the client secret follows
 * the password's rule exactly: never shown, and sent only when typed.
 * Design: docs/superpowers/specs/2026-09-11-mail-oauth-design.md §3.
 */
const MICROSOFT_FIXTURE = {
  ...EMAIL_SETTINGS_FIXTURE,
  host: "smtp.office365.com",
  username: "itms@toeshee.com",
  auth_type: "token",
  microsoft_client_id: "11111111-2222-3333-4444-555555555555",
  microsoft_sign_in_url:
    "https://login.microsoftonline.com/contoso.onmicrosoft.com/oauth2/v2.0/token",
  microsoft_scope: "https://outlook.office365.com/.default",
};

async function chooseMicrosoft(user: ReturnType<typeof userEvent.setup>) {
  const group = await screen.findByRole("group", { name: "Sign-in" });
  await user.click(within(group).getByRole("button", { name: "Microsoft 365" }));
}

describe("Settings — Email tab, signing in with Microsoft 365", () => {
  it("offers Microsoft 365 and swaps the password box for the application's details", async () => {
    const user = await openEmail();

    await chooseMicrosoft(user);

    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Directory (tenant) ID")).toBeInTheDocument();
    expect(screen.getByLabelText("Application (client) ID")).toBeInTheDocument();
    expect(screen.getByLabelText<HTMLInputElement>("Client secret")).toHaveValue("");
  });

  it("says the saved password will not be kept, before the save", async () => {
    const user = await openEmail();

    await chooseMicrosoft(user);

    expect(
      screen.getByText(/replace the saved mail server password with a Microsoft 365 sign-in/i),
    ).toBeInTheDocument();
  });

  it("builds the token address from the directory ID and sends no password", async () => {
    const user = await openEmail();
    await chooseMicrosoft(user);

    await user.type(screen.getByLabelText("Directory (tenant) ID"), "contoso.onmicrosoft.com");
    await user.type(screen.getByLabelText("Application (client) ID"), "app-id");
    await user.type(screen.getByLabelText("Client secret"), "typed-client-secret");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(calls.updateEmailSettings).toHaveBeenCalledTimes(1));
    const body = calls.updateEmailSettings.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(body).toMatchObject({
      auth_type: "token",
      token_url: "https://login.microsoftonline.com/contoso.onmicrosoft.com/oauth2/v2.0/token",
      token_scope: "https://outlook.office365.com/.default",
      token_client_id: "app-id",
      token_client_secret: "typed-client-secret",
    });
    expect(body).not.toHaveProperty("password");
  });

  it("will not save a first Microsoft sign-in without a client secret", async () => {
    const user = await openEmail();
    await chooseMicrosoft(user);

    await user.type(screen.getByLabelText("Directory (tenant) ID"), "contoso.onmicrosoft.com");
    await user.type(screen.getByLabelText("Application (client) ID"), "app-id");

    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("loads a saved Microsoft sign-in and omits the untouched client secret", async () => {
    calls.getEmailSettings.mockResolvedValue(MICROSOFT_FIXTURE);
    const user = await openEmail();

    expect(await screen.findByLabelText("Directory (tenant) ID")).toHaveValue(
      "contoso.onmicrosoft.com",
    );
    expect(screen.getByLabelText("Client secret")).toHaveAccessibleDescription(
      /client secret: set\. leave the box blank to keep it\./i,
    );
    // No warning: there is no password to lose.
    expect(screen.queryByText(/password is not kept/i)).not.toBeInTheDocument();

    const name = screen.getByLabelText("Sender name");
    await user.clear(name);
    await user.type(name, "IT desk");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(calls.updateEmailSettings).toHaveBeenCalledTimes(1));
    const body = calls.updateEmailSettings.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(body).toMatchObject({
      auth_type: "token",
      token_client_id: MICROSOFT_FIXTURE.microsoft_client_id,
      token_url: MICROSOFT_FIXTURE.microsoft_sign_in_url,
    });
    // The omission is the feature, exactly as for the password.
    expect(body).not.toHaveProperty("token_client_secret");
  });

  it("switching back to a password sends the password mode and no client secret", async () => {
    calls.getEmailSettings.mockResolvedValue(MICROSOFT_FIXTURE);
    const user = await openEmail();

    const group = await screen.findByRole("group", { name: "Sign-in" });
    await user.click(within(group).getByRole("button", { name: "Password" }));
    // The Microsoft sign-in had no password, so none is claimed.
    expect(screen.getByLabelText("Password")).toHaveAccessibleDescription(/password: not set/i);
    await user.type(screen.getByLabelText("Password"), "back-to-password");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(calls.updateEmailSettings).toHaveBeenCalledTimes(1));
    const body = calls.updateEmailSettings.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(body).toMatchObject({ auth_type: "password", password: "back-to-password" });
    expect(body).not.toHaveProperty("token_client_secret");
  });

  it("uses the operator's words, not the protocol's", async () => {
    const user = await openEmail();
    await chooseMicrosoft(user);

    const panel = screen.getByRole("tabpanel");
    expect(panel.textContent).not.toMatch(/oauth|xoauth2|client.credentials/i);
  });
});
