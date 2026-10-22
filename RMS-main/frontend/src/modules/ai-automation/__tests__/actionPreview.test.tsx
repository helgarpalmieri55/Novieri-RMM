// §6.3's layout, as a test. **THE TWO REGIONS' TEXT IS ASSERTED SEPARATELY.**
//
// Asserting only "the rationale is displayed" passes against a UI that renders
// it AS the action description, which is precisely the failure §6.3 exists to
// prevent: a laundered instruction shown as an authoritative account of what
// will run is a confirmation the human did not really give.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  ACTION_REGION_TESTID,
  ActionPreview,
  RATIONALE_REGION_TESTID,
} from "../ActionPreview";
import { AI_CEILING_LEVELS } from "../types";
import type { AiActionPreviewOut } from "../types";

/** What a laundered instruction actually looks like: authoritative prose about
 * an effect, plus markup, in the one field the model controls. */
const HOSTILE_RATIONALE =
  "This will safely clear the print queue. <b>No approval needed.</b> " +
  "Job type: script.run on every device.";

function preview(overrides: Partial<AiActionPreviewOut> = {}): AiActionPreviewOut {
  return {
    ai_action_id: "11111111-1111-1111-1111-111111111111",
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

describe("the confirmation screen's two regions", () => {
  it("keeps a hostile rationale out of the action description entirely", () => {
    render(<ActionPreview preview={preview({ rationale: HOSTILE_RATIONALE })} />);

    const action = screen.getByTestId(ACTION_REGION_TESTID);
    const why = screen.getByTestId(RATIONALE_REGION_TESTID);

    // THE DESCRIPTION REGION CONTAINS NONE OF IT.
    expect(action.textContent).not.toContain("safely clear the print queue");
    expect(action.textContent).not.toContain("No approval needed");
    expect(action.textContent).not.toContain("script.run");
    // And it does contain the catalogue's own words.
    expect(action.textContent).toContain("Collect inventory");
    expect(action.textContent).toContain("inventory.collect");

    // THE RATIONALE REGION CONTAINS IT, AND AS TEXT: the markup is escaped, so
    // `<b>` is characters on the screen rather than an element in the tree.
    expect(why.textContent).toContain("safely clear the print queue");
    expect(why.textContent).toContain("<b>No approval needed.</b>");
    expect(why.querySelector("b")).toBeNull();
  });

  it("makes the description a function of the action fields and not of the rationale", () => {
    // `now` is PINNED: the expiry countdown is the one field that moves on its
    // own, and a test that let it drift would be asserting about the clock.
    const now = Date.now();
    const { container, rerender } = render(<ActionPreview preview={preview()} now={now} />);
    const before = screen.getByTestId(ACTION_REGION_TESTID).textContent;

    // Change the rationale: the description must not move.
    rerender(
      <ActionPreview preview={preview({ rationale: "something else entirely" })} now={now} />,
    );
    expect(screen.getByTestId(ACTION_REGION_TESTID).textContent).toBe(before);

    // Change an action field: it must.
    rerender(<ActionPreview preview={preview({ job_type: "patch.apply" })} now={now} />);
    expect(screen.getByTestId(ACTION_REGION_TESTID).textContent).not.toBe(before);
    expect(container.textContent).toContain("patch.apply");
  });

  it("renders the taint badge only when the proposal is tainted", () => {
    const { rerender } = render(<ActionPreview preview={preview({ tainted: true })} />);
    expect(screen.getByTestId("ai-action-taint-badge")).toBeTruthy();

    rerender(<ActionPreview preview={preview({ tainted: false })} />);
    expect(screen.queryByTestId("ai-action-taint-badge")).toBeNull();
  });

  it("counts down to the preview's expiry and says so when it has passed", () => {
    const now = Date.now();
    render(
      <ActionPreview
        preview={preview({ expires_at: new Date(now - 1000).toISOString() })}
        now={now}
      />,
    );
    expect(screen.getByTestId(ACTION_REGION_TESTID).textContent).toContain("expired");
  });

  it("shows the resolved parameters as labelled rows rather than a JSON blob", () => {
    // §6.3 requires the parameters IN FULL; it does not require them as JSON.
    // `JSON.stringify` in one mono cell put the object on a line that
    // `word-break: break-all` then split mid-identifier -- at 390px `ring`
    // wrapped as "rin / g" -- so the one row that has to be read carefully was
    // the least readable thing on the screen.
    render(
      <ActionPreview
        preview={preview({ rendered_params: { kb: "KB5041585", ring: "test", devices: 12 } })}
      />,
    );
    const action = screen.getByTestId(ACTION_REGION_TESTID);

    // Every key and every value is still on the screen, in full.
    for (const text of ["kb", "KB5041585", "ring", "test", "devices", "12"]) {
      expect(action.textContent).toContain(text);
    }
    // And not as a serialised object.
    expect(action.textContent).not.toContain('{"kb"');
    expect(action.textContent).not.toContain('"ring":"test"');
  });

  it("says what the autonomy position means instead of printing the wire enum alone", () => {
    // `OUTCOME` in `ActionsPage` already translates the outcome vocabulary;
    // `effective_autonomy` was the one value left printing `require_approval`
    // in mono beside facts that are all sentences.
    render(<ActionPreview preview={preview({ effective_autonomy: "require_approval" })} />);
    const action = screen.getByTestId(ACTION_REGION_TESTID);
    expect(action.textContent).toContain("Held for a person to confirm");
    // The wire value stays visible: it is what the server actually stored.
    expect(action.textContent).toContain("require_approval");
  });

  it("puts the whole ladder in one list, so there is one value column", () => {
    // `.kv` is `grid-template-columns: max-content 1fr`, so each `<dl>`
    // measures its own term column. Two lists under one panel title put their
    // values at x=385 and x=350 — a 35px step in the middle of one panel, with
    // both label columns starting at the same x. One `<dl>` cannot do that.
    const { container } = render(
      <ActionPreview preview={preview({ rendered_params: { kb: "KB5041585", ring: "test" } })} />,
    );
    const action = container.querySelector(`[data-testid="${ACTION_REGION_TESTID}"]`);
    const lists = action?.querySelectorAll("dl.kv") ?? [];
    expect(lists).toHaveLength(1);
    const terms = [...(lists[0]?.querySelectorAll("dt") ?? [])].map((dt) => dt.textContent);
    // The catalogue facts and the resolved parameters are rows of the same
    // ladder, and the caption that introduces them is a row too.
    expect(terms).toContain("Catalogue key");
    expect(terms).toContain("Parameters");
    expect(terms).toContain("kb");
    expect(terms).toContain("ring");
  });

  it("says an action takes no parameters rather than rendering an empty object", () => {
    render(<ActionPreview preview={preview({ rendered_params: {} })} />);
    expect(screen.getByTestId(ACTION_REGION_TESTID).textContent).toContain(
      "This action takes no parameters",
    );
  });

  it("shows the redacted parameter paths and never a stored credential", () => {
    render(
      <ActionPreview
        preview={preview({
          rendered_params: { repo_key: "[redacted: stored credential]" },
          redacted_param_paths: ["repo_key"],
        })}
      />,
    );
    const action = screen.getByTestId(ACTION_REGION_TESTID);
    expect(action.textContent).toContain("redacted: stored credential");
    expect(action.textContent).toContain("repo_key");
  });
});

describe("the dial's vocabulary", () => {
  it("stops one short of autonomous", () => {
    // The control cannot offer what the database cannot store (§0.4). Asserted
    // on the exported constant so the page and this test cannot disagree.
    expect([...AI_CEILING_LEVELS]).toEqual(["off", "suggest", "require_approval"]);
    expect(AI_CEILING_LEVELS).not.toContain("autonomous");
  });
});
