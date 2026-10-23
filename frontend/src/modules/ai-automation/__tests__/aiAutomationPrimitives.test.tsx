/**
 * The shared error primitive, as act mode uses it (cycle 4).
 *
 * THIS MODULE'S FAILURES ARE THE ONES THAT MATTER MOST TO GET ON SCREEN. Every
 * refusal here means a job did NOT dispatch, and the previous rendering could
 * not promise that was said out loud: `explainFailure` was built out of
 * `error.message`, `ApiError` is `new ApiError(status, await response.text())`,
 * and a bodyless refusal therefore carried `""` — which the component tested
 * with `failure !== ""` before rendering anything. A confirm that did not run
 * looked exactly like one that did.
 *
 * Each test below was falsified against the pre-cycle implementation before
 * being kept.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient, type Me } from "../../../lib/api";
import { AiActionPage } from "../ActionsPage";
import { AutonomyDialPage } from "../AutonomyDialPage";
import { ConfirmDialog, explainFailure } from "../ConfirmDialog";
import { ceilingLabel, type AiActionOut, type AiActionPreviewOut } from "../types";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "a@b.c",
  display_name: "Alex Morgan",
  permissions: ["ai.act.propose", "ai.act.confirm", "ai.admin"],
};

const ACTION_ID = "11111111-1111-1111-1111-111111111111";

/** One proposal waiting for a person, in the LIST shape — which carries no
 *  `name`, which is the whole reason the panel reads one from the preview. */
const WAITING: AiActionOut = {
  id: ACTION_ID,
  session_id: "44444444-4444-4444-4444-444444444444",
  persona: "technician",
  approved_action_key: "patch.approve.ring",
  job_type: "patch.approve",
  target_device_id: "33333333-3333-3333-3333-333333333333",
  risk_class: "medium",
  destructive: false,
  effective_autonomy: "require_approval",
  tainted: false,
  state: "previewed",
  expires_at: new Date(Date.now() + 600_000).toISOString(),
  confirmed_by: null,
  rejected_reason: null,
  job_id: null,
  created_at: new Date(Date.now() - 3_600_000).toISOString(),
};

function preview(overrides: Partial<AiActionPreviewOut> = {}): AiActionPreviewOut {
  return {
    ai_action_id: ACTION_ID,
    approved_action_id: "22222222-2222-2222-2222-222222222222",
    approved_action_key: "collect-inventory",
    name: "Collect inventory",
    description: "Refresh the hardware and software inventory.",
    job_type: "inventory.collect",
    rendered_params: {},
    redacted_param_paths: [],
    script_name: null,
    script_version: null,
    target_device_id: "33333333-3333-3333-3333-333333333333",
    target_hostname: "cfo-laptop-7",
    risk_class: "low",
    destructive: false,
    effective_autonomy: "require_approval",
    tainted: false,
    fuse_verdict: null,
    expires_at: new Date(Date.now() + 600_000).toISOString(),
    preview_digest: "a".repeat(64),
    state: "previewed",
    rationale: "the inventory is 40 days stale",
    ...overrides,
  };
}

function stubApi(overrides: Partial<ApiClient> = {}): ApiClient {
  const api = {
    me: vi.fn().mockResolvedValue(ME),
    getAiAction: vi.fn().mockResolvedValue(preview()),
    confirmAiAction: vi.fn(),
    rejectAiAction: vi.fn(),
    listAiCeilings: vi.fn().mockResolvedValue([]),
    setAiCeiling: vi.fn(),
    // The dial page also lists the actions waiting for approval (v3's second
    // panel on that screen); an empty queue keeps the assertions below about
    // the ceilings and nothing else.
    listAiActions: vi.fn().mockResolvedValue([]),
    ...overrides,
  } as unknown as ApiClient;
  mockUseApi.mockReturnValue(api);
  return api;
}

function renderWithProviders(ui: ReactElement, path = "/") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockUseApi.mockReset();
});

// --------------------------------------------------------------------------
// The sentence itself
// --------------------------------------------------------------------------

describe("explainFailure", () => {
  it("never returns an empty string for a bodyless refusal", () => {
    // The whole defect in one assertion: `""` is falsy, so the component's
    // `failure !== ""` guard passed and NOTHING rendered.
    for (const status of [400, 403, 409, 500, 503]) {
      expect(explainFailure(new ApiError(status, "")).trim()).not.toBe("");
    }
  });

  it("still says nothing ran on a bodyless 409, and still says what went wrong", () => {
    const text = explainFailure(new ApiError(409, ""));
    expect(text).toMatch(/conflicts/i);
    expect(text).toMatch(/nothing was run/i);
    expect(text).toMatch(/reload the preview/i);
    // The old rendering began with a dash, because `error.message` was "".
    expect(text.startsWith(" —")).toBe(false);
  });

  it("keeps 410 as its own sentence rather than the shared fallback", () => {
    // `describeSubmitError` has no 410 case; deferring to it here would lose
    // the one fact that matters — the preview is gone, ask again.
    expect(explainFailure(new ApiError(410, ""))).toMatch(/expired before it was confirmed/i);
  });

  it("names permission, and that nothing ran, on a bodyless 403", () => {
    const text = explainFailure(new ApiError(403, ""));
    expect(text).toMatch(/permission/i);
    expect(text).toMatch(/nothing was run/i);
  });

  it("translates a raw Pydantic 422 payload instead of printing it", () => {
    const text = explainFailure(
      new ApiError(422, JSON.stringify({ detail: [{ loc: ["body", "digest"], msg: "too short" }] })),
    );
    expect(text).toBe("Digest: too short");
    expect(text).not.toContain("{");
  });

  it("is empty only when there is no error", () => {
    expect(explainFailure(null)).toBe("");
  });
});

// --------------------------------------------------------------------------
// …and that the sentence is announced
// --------------------------------------------------------------------------

describe("ConfirmDialog", () => {
  it("announces a bodyless 409 and still offers the reload, rather than rendering nothing", async () => {
    const api = stubApi({ confirmAiAction: vi.fn().mockRejectedValue(new ApiError(409, "")) });
    const onReload = vi.fn();
    renderWithProviders(
      <ConfirmDialog preview={preview()} canConfirm onReload={onReload} />,
    );

    await userEvent.click(screen.getByRole("button", { name: /confirm and run/i }));
    await waitFor(() => expect(api.confirmAiAction).toHaveBeenCalled());

    const alert = await screen.findByRole("alert");
    expect(alert.textContent?.trim()).not.toBe("");
    expect(alert).toHaveTextContent(/nothing was run/i);
    await userEvent.click(screen.getByRole("button", { name: /show the current preview/i }));
    expect(onReload).toHaveBeenCalled();
  });

  it("draws the decision pair in the page head, where v3 draws it", async () => {
    // The pair used to sit under the rationale panel — roughly 1,100px down at
    // 390px, so the two controls the screen exists for were below the fold.
    // The design file's own line for this route is
    // `head(..., [btn('Reject','x'), btn('Approve','check',true)], ...)`.
    stubApi({});
    const { container } = renderWithProviders(
      <ConfirmDialog preview={preview()} canConfirm onReload={vi.fn()} />,
    );

    const head = container.querySelector(".ph .ph-act");
    expect(head, "the head must carry an actions slot").not.toBeNull();
    expect(head).toContainElement(screen.getByRole("button", { name: /confirm and run/i }));
    expect(head).toContainElement(screen.getByRole("button", { name: /^decline$/i }));
  });

  it("keeps the head to the two controls the design draws, and asks why after Decline", async () => {
    // The reason picker used to be a third control IN the actions slot, and
    // the cluster then measured wider than the room beside the head block, so
    // `.ph-row` wrapped the whole decision onto the canvas below -- a bare
    // caption and a dropdown in no container at all. The design file draws
    // two: `head(..., [btn('Reject','x'), btn('Approve','check',true)])`.
    stubApi({});
    const { container } = renderWithProviders(
      <ConfirmDialog preview={preview()} canConfirm onReload={vi.fn()} />,
    );

    const head = container.querySelector(".ph .ph-act");
    expect(head?.querySelectorAll("button")).toHaveLength(2);
    // Nothing to answer until the decline is actually started.
    expect(screen.queryByRole("group", { name: "Decline reason" })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /^decline$/i }));
    expect(await screen.findByRole("group", { name: "Decline reason" })).toBeInTheDocument();
    // The head is still two controls; the question is asked below it.
    expect(head?.querySelectorAll("button")).toHaveLength(2);
  });

  it("gives the decline reason a visible label rather than an invisible one", async () => {
    // It carried `aria-label="Reason for declining"` and nothing on screen, so
    // a sighted operator saw an unexplained "not needed" dropdown with nothing
    // saying what it set. The accessible name is now the same words that are
    // printed above the control, and the options are sentence case rather than
    // the wire enum with its underscores swapped for spaces.
    stubApi({});
    renderWithProviders(<ConfirmDialog preview={preview()} canConfirm onReload={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /^decline$/i }));

    const group = await screen.findByRole("group", { name: "Decline reason" });
    expect(screen.getByText("Decline reason")).toBeInTheDocument();
    expect(within(group).getByRole("button", { name: "Not needed" })).toBeInTheDocument();
    expect(within(group).getByRole("button", { name: "Needs a change request" })).toBeInTheDocument();
    expect(within(group).queryByText(/needs change request/)).not.toBeInTheDocument();
    // Every one of the six the wire accepts is offered; none was dropped by
    // giving them labels.
    expect(within(group).getAllByRole("button")).toHaveLength(6);
  });

  it("sends the reason the operator picked, and nothing until they send it", async () => {
    const api = stubApi({ rejectAiAction: vi.fn().mockResolvedValue({}) });
    renderWithProviders(<ConfirmDialog preview={preview()} canConfirm onReload={vi.fn()} />);

    await userEvent.click(screen.getByRole("button", { name: /^decline$/i }));
    const group = await screen.findByRole("group", { name: "Decline reason" });
    await userEvent.click(within(group).getByRole("button", { name: "Wrong device" }));
    // Pressing Decline and picking a reason has written nothing.
    expect(api.rejectAiAction).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: /send the decline/i }));
    await waitFor(() =>
      expect(api.rejectAiAction).toHaveBeenCalledWith(ACTION_ID, { reason: "wrong_device" }),
    );
  });

  it("announces a failed DECLINE too — the reject path had the same silence", async () => {
    const api = stubApi({ rejectAiAction: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    renderWithProviders(<ConfirmDialog preview={preview()} canConfirm onReload={vi.fn()} />);

    await userEvent.click(screen.getByRole("button", { name: /^decline$/i }));
    await userEvent.click(await screen.findByRole("button", { name: /send the decline/i }));
    await waitFor(() => expect(api.rejectAiAction).toHaveBeenCalled());

    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable/i);
  });
});

describe("AiActionPage", () => {
  it("announces a bodyless failure to load the preview rather than showing a blank page", async () => {
    stubApi({ getAiAction: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    renderWithProviders(
      <Routes>
        <Route path="/assistant/actions/:actionId" element={<AiActionPage />} />
      </Routes>,
      `/assistant/actions/${ACTION_ID}`,
    );

    const alert = await screen.findByRole("alert");
    expect(alert.textContent?.trim()).not.toBe("");
    expect(alert).toHaveTextContent(/unavailable/i);
  });
});

// --------------------------------------------------------------------------
// The dial, which wrote a security control on `onChange`
//
// Auditor B, [BLOCKER], /assistant/autonomy: "A security control is mutated and
// persisted by a single dropdown change with no Save, no confirmation and no
// feedback of any kind ... after a full page navigation the value read back as
// `suggest` — the change had been written and persisted."
//
// Every test below was falsified against the auto-saving version: the first
// four fail on it (it calls `setAiCeiling` from `onChange`, offers no Save
// button, no confirmation and no "Saved."), and the last two fail on a version
// that saves without confirming.
// --------------------------------------------------------------------------

const CEILING_OK = {
  persona: "technician",
  risk_class: "high",
  level: "require_approval",
  updated_by: null,
  updated_at: null,
};

/**
 * Set one cell of the dial.
 *
 * THE CONTROL IS `Chips`, NOT A `<select>`, since the ceiling grid stopped
 * wearing the browser's own widget (and stopped clipping `require_approval` to
 * "require_app" at 390px). The cell is still addressed by the same test id and
 * the level is still named by its wire token here; only the gesture changed,
 * from picking an option to pressing the chip whose label that token carries.
 */
async function choose(cell: string, level: string): Promise<void> {
  const chips = within(await screen.findByTestId(cell));
  await userEvent.click(chips.getByRole("button", { name: ceilingLabel(level) }));
}

/** The level a cell is currently showing, as its wire token. */
async function shown(cell: string): Promise<string | undefined> {
  const chips = within(await screen.findByTestId(cell));
  const pressed = chips
    .getAllByRole("button")
    .find((chip) => chip.getAttribute("aria-pressed") === "true");
  return ["off", "suggest", "require_approval"].find(
    (level) => ceilingLabel(level) === pressed?.textContent?.trim(),
  );
}

describe("AutonomyDialPage", () => {
  it("writes nothing when a cell is changed — the edit is a draft until Save", async () => {
    // THE BLOCKER, in one assertion. A scroll-wheel bump over a dropdown must
    // not widen what an AI may do to the fleet.
    const api = stubApi({ setAiCeiling: vi.fn().mockResolvedValue(CEILING_OK) });
    renderWithProviders(<AutonomyDialPage />);

    await choose("dial-technician-high", "require_approval");

    expect(api.setAiCeiling).not.toHaveBeenCalled();
    // ...and the page says the change is unsaved, naming the cell, rather than
    // leaving the operator to assume either way.
    expect(await screen.findByText(/1 unsaved change/)).toHaveTextContent(
      "Technician / high risk: Off → Needs approval",
    );
  });

  it("asks before saving, names the cell and says what the new level permits", async () => {
    const api = stubApi({ setAiCeiling: vi.fn().mockResolvedValue(CEILING_OK) });
    renderWithProviders(<AutonomyDialPage />);

    await choose("dial-technician-high", "require_approval");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));

    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Technician / high risk: Off → Needs approval");
    // The audit's [MEDIUM] as well as its [BLOCKER]: "off → require_approval"
    // means nothing to a person who has not read §4's table.
    expect(dialog).toHaveTextContent(/runs only after a person confirms it/i);
    // Raising is the direction that carries the risk, and the title says which
    // direction this is.
    expect(dialog).toHaveTextContent(/Raise the assistant's autonomy ceiling/i);
    expect(api.setAiCeiling).not.toHaveBeenCalled();
  });

  it("cancelling the confirmation writes nothing and keeps the draft", async () => {
    const api = stubApi({ setAiCeiling: vi.fn().mockResolvedValue(CEILING_OK) });
    renderWithProviders(<AutonomyDialPage />);

    await choose("dial-technician-high", "require_approval");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await userEvent.click(await screen.findByRole("button", { name: "Cancel" }));

    expect(api.setAiCeiling).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    // The edit is not thrown away by cancelling the CONFIRMATION — that is what
    // "Discard changes" is for.
    expect(await shown("dial-technician-high")).toBe("require_approval");
  });

  it("writes every changed cell once confirmed, and says the save landed", async () => {
    const setAiCeiling = vi.fn().mockResolvedValue(CEILING_OK);
    stubApi({ setAiCeiling });
    renderWithProviders(<AutonomyDialPage />);

    await choose("dial-technician-high", "require_approval");
    await choose("dial-admin-low", "suggest");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await userEvent.click(await screen.findByRole("button", { name: "Raise the ceiling" }));

    await waitFor(() => expect(setAiCeiling).toHaveBeenCalledTimes(2));
    expect(setAiCeiling).toHaveBeenCalledWith({
      persona: "technician",
      risk_class: "high",
      level: "require_approval",
    });
    expect(setAiCeiling).toHaveBeenCalledWith({
      persona: "admin",
      risk_class: "low",
      level: "suggest",
    });
    // "no toast, no 'Saved', no live region" was half the finding.
    expect(await screen.findByText(/^Saved\./)).toBeInTheDocument();
    expect(screen.queryByText(/unsaved change/)).not.toBeInTheDocument();
  });

  it("discarding restores the stored value and leaves nothing to save", async () => {
    const api = stubApi({ setAiCeiling: vi.fn().mockResolvedValue(CEILING_OK) });
    renderWithProviders(<AutonomyDialPage />);

    await choose("dial-technician-high", "require_approval");
    await userEvent.click(await screen.findByRole("button", { name: "Discard changes" }));

    expect(await shown("dial-technician-high")).toBe("off");
    expect(screen.queryByText(/unsaved change/)).not.toBeInTheDocument();
    expect(api.setAiCeiling).not.toHaveBeenCalled();
  });

  it("says a ceiling was NOT changed when the write is refused, and names the cell", async () => {
    // On a page whose only job is to say what the ceilings are, a refused
    // lowering must never read as a successful one.
    const api = stubApi({ setAiCeiling: vi.fn().mockRejectedValue(new ApiError(403, "")) });
    renderWithProviders(<AutonomyDialPage />);

    await choose("dial-technician-high", "require_approval");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await userEvent.click(await screen.findByRole("button", { name: "Raise the ceiling" }));
    await waitFor(() => expect(api.setAiCeiling).toHaveBeenCalled());

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/technician/i);
    expect(alert).toHaveTextContent(/high/);
    expect(alert).toHaveTextContent(/was not changed/i);
    expect(alert).toHaveTextContent(/permission/i);
    // NO OPTIMISTIC SUCCESS, and the unapplied change is still on screen.
    expect(screen.queryByText(/^Saved\./)).not.toBeInTheDocument();
    expect(await screen.findByText(/1 unsaved change/)).toBeInTheDocument();
  });

  it("lowering a ceiling is confirmed too, but is not dressed as a widening", async () => {
    const setAiCeiling = vi.fn().mockResolvedValue({ ...CEILING_OK, level: "off" });
    stubApi({
      setAiCeiling,
      listAiCeilings: vi.fn().mockResolvedValue([
        { persona: "technician", risk_class: "high", level: "require_approval", updated_by: null, updated_at: null },
      ]),
    });
    renderWithProviders(<AutonomyDialPage />);

    await choose("dial-technician-high", "off");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));

    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent(/Change the assistant's autonomy ceiling/i);
    expect(dialog).not.toHaveTextContent(/Raise the assistant/i);
    await userEvent.click(screen.getByRole("button", { name: "Apply the change" }));

    await waitFor(() => expect(setAiCeiling).toHaveBeenCalledTimes(1));
    expect(setAiCeiling).toHaveBeenCalledWith({
      persona: "technician",
      risk_class: "high",
      level: "off",
    });
  });

  it("defines all three levels on the page, not only inside the confirmation", async () => {
    // Audit [MEDIUM]: "Cells offer off, suggest, require_approval with no
    // legend, tooltip or description of the difference." A definition that
    // only appears once the operator has already pressed Save is not a legend.
    stubApi();
    renderWithProviders(<AutonomyDialPage />);

    const legend = (await screen.findByText(/Nothing is prepared, so there is nothing to confirm/i)).closest("dl");
    expect(legend).not.toBeNull();
    expect(legend).toHaveTextContent(/will not offer actions of this risk at all/i);
    expect(legend).toHaveTextContent(/runs only after a person confirms it/i);
    // Audit [LOW]: the risk classes are set on each approved action, not here,
    // and the page must say where they come from rather than leave the
    // administrator setting policy over a classification they cannot see.
    expect(
      screen.getByText(/Risk class comes from each approved action/i),
    ).toBeInTheDocument();
  });

  it("draws each ceiling as chips, in words, with no browser widget and nothing clipped", async () => {
    // v3 draws this control as `field('Level',chips([...],1))`. It was a native
    // `<select>`: the browser's own widget painted through the kit's box, and
    // at 390px narrower than `require_approval`, which a select truncates
    // without an ellipsis — the admin / low cell read "require_app".
    stubApi();
    renderWithProviders(<AutonomyDialPage />);

    const cell = await screen.findByTestId("dial-admin-low");
    expect(within(cell).getByRole("group")).toBeInTheDocument();
    // Every position on screen at once, in words, none of them behind a popup.
    for (const level of ["off", "suggest", "require_approval"]) {
      expect(within(cell).getByRole("button", { name: ceilingLabel(level) })).toBeInTheDocument();
    }
    expect(screen.queryAllByRole("combobox")).toHaveLength(0);
    // The wire tokens are still on the page, as the legend's own terms.
    expect(screen.getByText("require_approval")).toBeInTheDocument();
  });

  it("names the personas rather than printing their identifiers as headings", async () => {
    stubApi();
    renderWithProviders(<AutonomyDialPage />);

    expect(await screen.findByText("End user")).toBeInTheDocument();
    expect(screen.getByText("Administrator")).toBeInTheDocument();
    // Kept, not replaced: `end_user` is what the API stores and what an
    // operator reading the docs will search for.
    expect(screen.getByText("end_user")).toBeInTheDocument();
  });

  it("keeps the end_user row fixed at off, with no control to change it", async () => {
    stubApi();
    renderWithProviders(<AutonomyDialPage />);
    for (const risk of ["low", "medium", "high"]) {
      const cell = await screen.findByTestId(`dial-end_user-${risk}`);
      // STRUCTURE ONLY: the cell said `off (fixed)` — the wire token in lower
      // case, in the column where every other cell says "Off", "Suggest only"
      // or "Needs approval". Same value, same words as the rest of the grid,
      // and still marked as the one that cannot be changed.
      expect(cell).toHaveTextContent(ceilingLabel("off"));
      expect(cell).toHaveTextContent(/Fixed/);
      expect(cell.tagName).not.toBe("SELECT");
      // The grid's editable cells are chip groups now, so "no control here"
      // means no chip either, not only no dropdown.
      expect(within(cell).queryAllByRole("button")).toHaveLength(0);
      expect(within(cell).queryByRole("group")).not.toBeInTheDocument();
    }
  });

  it("titles a waiting action with the catalogue's sentence, not its dotted key", async () => {
    const api = stubApi({
      listAiActions: vi.fn().mockResolvedValue([WAITING]),
      getAiAction: vi.fn().mockResolvedValue(
        preview({
          ai_action_id: WAITING.id,
          approved_action_key: WAITING.approved_action_key,
          name: "Approve KB5041585 into ring test",
          job_type: WAITING.job_type,
        }),
      ),
    });
    renderWithProviders(<AutonomyDialPage />);

    // v3 draws this row as a sentence. The list payload has none, so the name
    // is read from the detail route — the same read the row's own link makes.
    const link = await screen.findByRole("link", { name: "Approve KB5041585 into ring test" });
    expect(link).toHaveAttribute("href", `/assistant/actions/${WAITING.id}`);
    // The identifier is kept UNDER the name, not deleted, and the persona is
    // in the page's own vocabulary — read off the ROW, because "Technician" is
    // also a heading in the grid above it.
    const row = link.closest("tr");
    expect(row).not.toBeNull();
    expect(row?.textContent).toContain(WAITING.approved_action_key);
    expect(row?.textContent).toContain("· Technician · proposed");
    expect(api.getAiAction).toHaveBeenCalledWith(WAITING.id);
  });

  it("falls back to the key when the catalogue name does not load, and never goes untitled", async () => {
    stubApi({
      listAiActions: vi.fn().mockResolvedValue([WAITING]),
      getAiAction: vi.fn().mockRejectedValue(new ApiError(403, "")),
    });
    renderWithProviders(<AutonomyDialPage />);

    expect(
      await screen.findByRole("link", { name: WAITING.approved_action_key }),
    ).toBeInTheDocument();
    // With no name, the sub-line carries the job type rather than repeating
    // the key that is already the title.
    expect(screen.getByText(WAITING.job_type)).toBeInTheDocument();
  });
});
