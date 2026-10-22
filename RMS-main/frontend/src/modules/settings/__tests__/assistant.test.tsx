// Assistant tab — `GET /ai/settings`, `GET /ai/providers`, `PUT /ai/settings`.

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../lib/api";
import { EGRESS_DISCLOSURE } from "../../ai-admin/egress";
import {
  PROVIDER_OFF_SUMMARY,
  SERVER_ADMIN_DETAIL,
  SERVER_ADMIN_HEADING,
} from "../../ai-admin/provider";
import {
  PLANTED_SECRET,
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
  await user.click(await screen.findByRole("tab", { name: "Assistant" }));
}

describe("Settings — Assistant tab", () => {
  it("loads the org's dials, and offers no control that decides whether the assistant runs", async () => {
    // **THE DECISION CHANGED** (spec §0.14, operator, 2026-08-25). This test
    // also asserted `Model provider` held "primary" and that
    // `Enable the assistant` was checked. Both controls are gone: the provider
    // and the switch are the deployment's `ITS_AI_*`. What this tab still owns
    // is cost and retention, and that half is unchanged.
    renderSettings();
    await openTab();
    expect(
      (await screen.findByLabelText<HTMLInputElement>("Keep conversations for")).value,
    ).toBe("30");
    expect(screen.getByLabelText<HTMLInputElement>("Questions per person, per minute").value).toBe("5");
    expect(
      screen.getByLabelText<HTMLInputElement>("Questions per organization, per hour").value,
    ).toBe("100");
    // `null` is "no budget" and must be a BLANK box, not a zero.
    expect(screen.getByLabelText<HTMLInputElement>("Daily usage limit").value).toBe(
      "",
    );
    expect(screen.queryByLabelText(/Enable the assistant/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Model provider")).not.toBeInTheDocument();
    // Asserted as "no checkbox at all": a renamed tickbox would slip past a
    // query that only knows the old name.
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
  });

  it("puts units and defaults in the description, not in the label", async () => {
    renderSettings();
    await openTab();
    const retention = await screen.findByRole("spinbutton", { name: "Keep conversations for" });
    expect(retention).toHaveAccessibleDescription("Days");
    const budget = screen.getByRole("spinbutton", { name: "Daily usage limit" });
    expect(budget).toHaveAccessibleDescription("Leave blank for no limit");
  });

  it("PUTs the edited dials, shows success, and refetches", async () => {
    // **THE DECISION CHANGED** (§0.14). This was "PUTs the edited settings with
    // the acknowledgement": the body carried `egress_acknowledged`, `enabled`
    // and `provider_key`, and the save was preceded by ticking the
    // acknowledgement. The server's `SettingsUpdate` is `extra="forbid"`, so
    // sending any of the three now is a 422 — which is why the body is
    // asserted as an EXACT object rather than with `objectContaining`.
    const user = userEvent.setup();
    renderSettings();
    await openTab();

    const retention = await screen.findByLabelText<HTMLInputElement>(
      "Keep conversations for",
    );
    await user.clear(retention);
    await user.type(retention, "45");

    calls.getAiSettings.mockResolvedValue({
      transcript_retention_days: 45,
      max_turns_per_user_per_minute: 5,
      max_turns_per_org_per_hour: 100,
      daily_token_budget: null,
    });
    await user.click(screen.getByRole("button", { name: "Save assistant settings" }));

    await waitFor(() => expect(calls.updateAiSettings).toHaveBeenCalledTimes(1));
    expect(calls.updateAiSettings).toHaveBeenCalledWith({
      transcript_retention_days: 45,
      max_turns_per_user_per_minute: 5,
      max_turns_per_org_per_hour: 100,
      daily_token_budget: null,
    });
    expect(await screen.findByRole("status")).toHaveTextContent(/Saved\./);
    // Refetched: the read ran once on mount and again after the write.
    await waitFor(() => expect(calls.getAiSettings.mock.calls.length).toBeGreaterThan(1));
  });

  it("a failed save is unmistakable: the server's error is shown and success is not claimed", async () => {
    const user = userEvent.setup();
    renderSettings();
    await openTab();

    const retention = await screen.findByLabelText<HTMLInputElement>(
      "Keep conversations for",
    );
    await user.clear(retention);
    await user.type(retention, "45");
    calls.updateAiSettings.mockRejectedValue(
      new ApiError(
        400,
        JSON.stringify({ detail: "transcript_retention_days must be between 1 and 365" }),
      ),
    );

    await user.click(screen.getByRole("button", { name: "Save assistant settings" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("transcript_retention_days must be between 1 and 365");
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

  it("never renders provider key material, and offers no field that could carry it", async () => {
    renderSettings();
    await openTab();
    await screen.findByLabelText("Keep conversations for");
    // The fixture's `api_key` is a server bug — key material in a response.
    // Nothing on this page may echo it.
    expect(document.body.textContent ?? "").not.toContain(PLANTED_SECRET);
    expect(document.querySelectorAll('input[type="password"]')).toHaveLength(0);
    for (const label of [/api key/i, /base url/i, /endpoint url/i, /secret/i]) {
      expect(screen.queryByLabelText(label)).not.toBeInTheDocument();
    }
    expect(
      screen.getByText(/Provider API keys and base URLs are never returned by the API/i),
    ).toBeInTheDocument();
  });

  it("saves without any acknowledgement, because there is none to give", async () => {
    // **THE DECISION CHANGED** (§0.14). This was "blocks the save, with the
    // reason, until the egress acknowledgement is given", and it asserted a
    // DISABLED Save button plus the sentence explaining it.
    //
    // **THE NEW RULE:** the acknowledgement is gone with the enable it gated,
    // so a dirty form saves on the first click. The falsifier is the one that
    // matters: if the tickbox ever comes back, this goes red on the request
    // that was never sent.
    const user = userEvent.setup();
    renderSettings();
    await openTab();
    const retention = await screen.findByLabelText<HTMLInputElement>(
      "Keep conversations for",
    );
    await user.clear(retention);
    await user.type(retention, "45");

    const save = screen.getByRole("button", { name: "Save assistant settings" });
    expect(save).toBeEnabled();
    expect(screen.queryByText(/acknowledgement/i)).not.toBeInTheDocument();
    await user.click(save);
    await waitFor(() => expect(calls.updateAiSettings).toHaveBeenCalledTimes(1));
  });

  // ------------------------------------------------------------------------
  // UX AUDIT [HIGH] on the sibling surface (/assistant/settings → "Model
  // provider"): a control that looks answerable and is not, with no
  // explanation. The control is gone (§0.14) and the dead end it created is
  // not: a deployment with no provider still has to say so, and say what to
  // do, since the assistant is unavailable to everyone on it.
  // ------------------------------------------------------------------------

  it("says the assistant is off for everyone on this server, not that it 'cannot be enabled'", async () => {
    // RE-POINTED (language pass, 2026-09-11, `docs/design/settings-language.md`
    // "Assistant"): this used to assert the org-admin-facing sentence NAMED
    // the environment variables directly — `PROVIDER_UNCONFIGURED_HINT`,
    // addressed to whoever can set `ITS_AI_PROVIDERS` on the control plane.
    // That reader is an org admin, who cannot act on it. The genuinely new
    // target is `PROVIDER_OFF_SUMMARY`: it says what changed for THEM (off,
    // for everyone, not their box to fix) and sends the variable names to a
    // disclosure a server administrator would open instead — covered by the
    // next test.
    calls.listAiProviders.mockResolvedValue([]);
    renderSettings();
    await openTab();

    // Asserted against the constant this tab and `/assistant/settings` both
    // import from `ai-admin/provider.ts`, not a retyped literal: the two
    // pages shipped text-identical local copies of this sentence once
    // already, and a hardcoded literal here would not notice this tab
    // drifting from that shared source the next time either page is edited.
    expect(await screen.findByText(PROVIDER_OFF_SUMMARY)).toBeInTheDocument();
    // The consequence sentence changed with the decision: "the assistant
    // cannot be enabled" would send a reader hunting for an enable control
    // that no longer exists.
    expect(screen.queryByText(/cannot be enabled/i)).not.toBeInTheDocument();
    // The hint names server configuration, NOT a console screen: §0.10 keeps
    // providers out of the console on purpose, so an "add one here" link would
    // point at a page that must never exist.
    expect(screen.queryByRole("link", { name: /add a provider/i })).not.toBeInTheDocument();
  });

  it("keeps the environment variable names behind a disclosure for the server administrator", async () => {
    // GENUINELY NEW COVERAGE, not a rename: the old hint put the variable
    // names in the same always-visible sentence as the org-admin-facing
    // warning. They now live behind a closed `<details>` titled "What the
    // server administrator needs to set", addressed to a different reader —
    // this proves that disclosure exists, starts closed, and opens.
    calls.listAiProviders.mockResolvedValue([]);
    renderSettings();
    await openTab();

    const summary = await screen.findByText(SERVER_ADMIN_HEADING);
    const disclosure = summary.closest("details");
    expect(disclosure).not.toHaveAttribute("open");

    // Asserted against the shared constant, same reasoning as
    // `PROVIDER_OFF_SUMMARY` above -- and the individual substring checks
    // below stay, since they pin the falsifiers the audit actually named
    // (each variable name, the restart, the "exactly one" limit) rather than
    // only the sentence's overall shape.
    const hint = screen.getByText(SERVER_ADMIN_DETAIL);
    expect(hint.closest("details")).toBe(disclosure);
    expect(hint).toHaveTextContent(/ITS_AI_PROVIDERS/);
    expect(hint).toHaveTextContent(/ITS_AI_<KEY>_KIND/);
    expect(hint).toHaveTextContent(/_BASE_URL/);
    expect(hint).toHaveTextContent(/_MODEL/);
    expect(hint).toHaveTextContent(/restart/i);
    expect(hint).toHaveTextContent(/exactly one provider/i);

    const user = userEvent.setup();
    await user.click(summary);
    expect(disclosure).toHaveAttribute("open");
  });

  it("states the configured provider as a fact, not as a choice", async () => {
    // **THE DECISION CHANGED** (§0.14). This was "keeps the chosen provider key
    // when there is one to choose", and it opened a combobox and read its
    // options. `kind · model` is still what is shown, because the key alone is
    // an operator's private naming and says nothing about where data goes —
    // but it is now text, and there is nothing to open.
    renderSettings();
    await openTab();
    expect(await screen.findByText("Primary")).toBeInTheDocument();
    expect(screen.getByText("· anthropic · claude-x")).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Model provider" })).not.toBeInTheDocument();
  });

  // Design review, 2026-09-11: "Where it goes" means two different things —
  // this tab's own provider heading AND the glossary's replacement for
  // "channel" on Notifications, one click away. Retitled "Provider", the same
  // choice `/assistant/settings` (`aiAdmin.test.tsx`) makes for its matching
  // panel, so the two screens still read the same story.
  it("headings the provider section 'Provider', freeing the phrase Notifications needs for 'channel'", async () => {
    renderSettings();
    await openTab();
    await screen.findByText("Primary");
    expect(screen.getByRole("heading", { name: "Provider", level: 3 })).toBeInTheDocument();
    expect(screen.queryByText("Where it goes")).not.toBeInTheDocument();
  });

  it("neither renders nor fetches the tab when the operator lacks ai.admin", async () => {
    installDefaults(["notification.read"]);
    renderSettings();
    await screen.findByRole("tab", { name: "Organization" });
    expect(screen.queryByRole("tab", { name: "Assistant" })).not.toBeInTheDocument();
    expect(calls.getAiSettings).not.toHaveBeenCalled();
    expect(calls.listAiProviders).not.toHaveBeenCalled();
  });
});

/**
 * GENUINELY NEW COVERAGE (language pass, 2026-09-11,
 * `docs/design/settings-language.md` "Assistant"). The eleven-bullet egress
 * account is security-critical and stays verbatim; only its WEIGHT changed —
 * a one-line summary sits above the controls, and the full list moved behind
 * a disclosure instead of sitting over them. `<details>`/`<summary>` gets
 * keyboard operability and an exposed expanded state for free, so what is
 * worth asserting is that the summary is genuinely reachable without opening
 * anything, and that the full account is genuinely behind the fold rather
 * than merely restyled in place.
 */
describe("Settings — Assistant tab, the egress account", () => {
  it("shows the one-line summary without needing to open anything", async () => {
    renderSettings();
    await openTab();

    expect(
      await screen.findByText(
        "Questions and the data needed to answer them go to your model provider. Passwords, " +
          "licence keys, recordings and other organizations' data never do.",
      ),
    ).toBeInTheDocument();
  });

  it("keeps the full, verbatim list behind a closed disclosure, and opens it on click", async () => {
    renderSettings();
    await openTab();

    const summary = await screen.findByText("Full list of what leaves and what never does");
    const disclosure = summary.closest("details");
    expect(disclosure).not.toHaveAttribute("open");

    // The full account is VERBATIM, not paraphrased -- pinned against the
    // same `EGRESS_DISCLOSURE` constant the tab renders from, the same way
    // `/assistant/settings` is, so the two screens cannot drift apart.
    for (const line of [...EGRESS_DISCLOSURE.leaves, ...EGRESS_DISCLOSURE.neverLeaves]) {
      const item = screen.getByText(line);
      expect(item.closest("details")).toBe(disclosure);
    }

    const user = userEvent.setup();
    await user.click(summary);
    expect(disclosure).toHaveAttribute("open");
  });
});

/**
 * Retention answers "how long is it kept", the ceilings answer "how much can
 * be spent". They are different questions and now say so. Both `h3`, under
 * the panel's "Assistant" `h2` and alongside the disclosure's own `h3`s.
 */
describe("Settings — Assistant tab, grouped", () => {
  it("separates retention from the rate ceilings", async () => {
    renderSettings();
    await openTab();

    const retention = (await screen.findByRole("heading", { name: "Retention", level: 3 })).closest(
      "section",
    ) as HTMLElement;
    expect(within(retention).getByLabelText("Keep conversations for")).toBeInTheDocument();

    const limits = (
      await screen.findByRole("heading", { name: "Rate limits", level: 3 })
    ).closest("section") as HTMLElement;
    for (const field of [
      "Questions per person, per minute",
      "Questions per organization, per hour",
      "Daily usage limit",
    ]) {
      expect(within(limits).getByLabelText(field)).toBeInTheDocument();
    }
    // The retention box is NOT a rate ceiling and must not be found here.
    expect(within(limits).queryByLabelText("Keep conversations for")).not.toBeInTheDocument();
  });

  /**
   * THE GUARD DERIVES ITS OWN SUBJECT, RATHER THAN NAMING ONE. The test
   * above pins WHICH group Retention's and Rate limits' own fields belong
   * to; a hardcoded list like that can never notice a field nobody
   * enumerated — which is exactly how `HealthTab` shipped grouped 3-of-11
   * elsewhere in this cycle. So this queries every labelled control the tab
   * actually rendered and asserts each one sits inside a
   * `section.field-group`. It goes red on the next field nobody grouped.
   */
  it("keeps every labelled control of the tab inside a group", async () => {
    renderSettings();
    await openTab();
    // A real floor, not just "the array wasn't empty": `openTab()` awaits
    // only the tab click, not the render past it, so a genuine load race
    // would otherwise query whatever happened to be on screen at that
    // instant. Anchoring on a known field first means the guard below sees
    // the fully-loaded form or fails here, honestly, instead of passing
    // quietly over a partial one.
    await screen.findByLabelText("Keep conversations for");

    const panel = screen.getByRole("tabpanel");
    const controls = within(panel).getAllByLabelText(/.+/);
    expect(controls.length).toBeGreaterThan(0);
    for (const control of controls) {
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
    // directly.
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
