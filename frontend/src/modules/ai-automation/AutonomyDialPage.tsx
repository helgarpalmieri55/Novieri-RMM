// The autonomy dial (§4, §12), gated `ai.admin` server-side.
//
// **THE CONTROL OFFERS `off | suggest | require_approval` AND NOTHING ELSE.**
// Not caution: `ai_autonomy_ceiling`'s CHECK has no `autonomous` value at all
// (§0.4), so a request carrying one is a 400 and a stored row is impossible.
// Widening it is a migration a reviewer sees, and this control not offering it
// keeps the console honest about what the database can hold.
//
// **A MISSING ROW IS `off`, AND THE GRID SAYS SO WITHOUT INVENTING ONE.** The
// server returns only STORED ceilings — a materialized default would read as
// configuration somebody made — so every unset cell renders `off` here, which is
// the deny-by-default the resolver applies.
//
// The `end_user` row is rendered READ-ONLY at `off`. §0.9 gives that persona no
// act at any dial position and a database CHECK enforces it, so an editable
// control there would offer a change the server refuses.
//
// ---------------------------------------------------------------------------
// **THE DIAL USED TO WRITE ON `onChange`, AND THAT WAS THE AUDIT'S BLOCKER.**
// Auditor B, /assistant/autonomy: "A security control is mutated and persisted
// by a single dropdown change with no Save, no confirmation and no feedback of
// any kind ... I changed technician/LOW from `off` to `suggest`. Result: no
// toast, no 'Saved', no 'Unsaved changes', no live region ... I then performed
// a full page navigation back to the route and the value read back as
// `suggest` — the change had been written and persisted."
//
// Three things follow, and they are the whole of this file's structure:
//
//   1. THE CONTROL EDITS A DRAFT, NOT THE SERVER. Nothing here writes until an
//      explicit Save. "A user who bumps a dropdown with a scroll wheel while
//      reading the page permanently widens the AI's autonomy ceiling" is a
//      sentence about auto-save, and the fix is to not auto-save.
//   2. SAVING CONFIRMS, AND THE CONFIRMATION NAMES THE CELL AND THE NEW
//      CAPABILITY. `components/confirm.tsx`'s own rule: say WHAT will happen,
//      to WHICH thing. Here "which thing" is `technician / high`, and "what"
//      is the sentence from §4's own table — a person reading
//      "off → require_approval" cannot be expected to know what they just
//      bought, and the audit's [MEDIUM] on this page says exactly that.
//   3. THE OUTCOME IS SAID OUT LOUD, both ways: `role="status"` on a save that
//      the server accepted, `role="alert"` naming the cell on one it refused.
//      A ceiling that was NOT lowered must never read as one that was.
// ---------------------------------------------------------------------------
//
// THE PAGE IS v3's `assistant/autonomy` SCREEN: the head, the autonomy panel,
// and under it the actions waiting for a person. v3 draws the level as one
// row of chips; this console's ceiling is a persona-by-risk grid, because
// that is what the server stores, so the panel holds the grid and each cell of
// it is one of v3's chip rows. v3's "Model
// provider" select is not drawn: the provider is server configuration and is
// reported, not offered, on Assistant settings.

import { useState, type ReactElement } from "react";
import { Link } from "react-router-dom";
import { ConfirmDialog } from "../../components/confirm";
import { describeSubmitError } from "../../components/form";
import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { lazyLabels } from "../../i18n/lazy";
import { formatDateTime } from "../../lib/datetime";
import { Btn, KIND_ICON, PageHead, Panel, QueryState, StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { Chips, KV } from "../../shell/viz";
import { useProfileZone } from "../ai-admin/api";
import { useAiActionNames, useAiActions, useAiCeilings, useSetAiCeiling } from "./api";
import { AutomationPrerequisites } from "./AutomationPrerequisites";
import {
  AI_CEILING_LEVELS,
  AI_PERSONAS,
  AI_RISK_CLASSES,
  ceilingLabel,
  personaLabel,
  riskClassLabel,
  type AiCeilingLevel,
  type AiPersona,
  type AiRiskClass,
} from "./types";
import { useEverywherePermissions } from "../../lib/mePermissions";

/**
 * The ordered vocabulary, weakest first — `app/core/ai/autonomy.py::LEVELS`
 * minus the `autonomous` the dial cannot store. Index order is what makes
 * "raising" a computable fact rather than a guess, and raising is the direction
 * that carries the risk.
 */
const RANK: Record<AiCeilingLevel, number> = { off: 0, suggest: 1, require_approval: 2 };

/**
 * What each position actually permits — docs/modules/ai-automation.md §4's own
 * table, restated where the control is.
 *
 * Audit [MEDIUM] on this page: "Cells offer `off`, `suggest`,
 * `require_approval` with no legend, tooltip or description of the difference
 * ... The practical difference between `suggest` and `require_approval` — when
 * both still require human confirmation — is genuinely unclear, and a user
 * setting a security ceiling should not have to guess." The difference is that
 * one of them mints a row and the other does not, so that is what these say.
 */
const LEVEL_MEANING = lazyLabels({
  off: "aiAutomation.meaning.off",
  suggest: "aiAutomation.meaning.suggest",
  require_approval: "aiAutomation.meaning.require_approval",
} as const);

interface PendingChange {
  persona: AiPersona;
  risk: AiRiskClass;
  from: AiCeilingLevel;
  to: AiCeilingLevel;
}

function cellKey(persona: AiPersona, risk: AiRiskClass): string {
  return `${persona}:${risk}`;
}

/**
 * A stored level, read defensively.
 *
 * An unknown string falls back to `off` rather than rendering a `<select>` with
 * no matching option (which shows the FIRST option and would misreport the
 * stored value as something the operator never set). `ck_ai_autonomy_ceiling_level`
 * makes this unreachable from the shipped server; the fallback is the safe
 * direction if that ever stops being true.
 */
function levelOf(value: string | undefined): AiCeilingLevel {
  return (AI_CEILING_LEVELS as readonly string[]).includes(value ?? "")
    ? (value as AiCeilingLevel)
    : "off";
}

/**
 * One pending edit as a sentence a person can check.
 *
 * IN WORDS, NOT WIRE TOKENS. This string is read in three places — the unsaved
 * line under the grid, the confirmation's subject and its body — and it is the
 * last thing between an operator and a widened ceiling, so it says
 * "Technician / high risk: Off → Needs approval" rather than restating the
 * enum. The identifiers are still on the page: each level's own token sits in
 * the legend and each persona's under its name.
 */
function describeChange(change: PendingChange): string {
  return translate("aiAutomation.dial.describe", {
    persona: personaLabel(change.persona),
    risk: riskWord(change.risk),
    from: ceilingLabel(change.from),
    to: ceilingLabel(change.to),
  });
}

/** A risk class in lower case, for the middle of a sentence ("high"). */
function riskWord(risk: string): string {
  return riskClassLabel(risk).toLocaleLowerCase();
}

export function AutonomyDialPage(): ReactElement {
  const t = useT();
  const ceilings = useAiCeilings();
  const setCeiling = useSetAiCeiling();
  // The dial is configuration every client shares (`PUT /ai/autonomy` is in
  // GLOBAL_WRITES): `ai.admin` on EVERY client, or it is read-only (phase 3
  // design §2).
  const canSet = useEverywherePermissions().has("ai.admin");

  /** Edits not yet sent. Keyed `persona:risk`; absent means "as stored". */
  const [draft, setDraft] = useState<Readonly<Record<string, AiCeilingLevel>>>({});
  const [confirming, setConfirming] = useState(false);
  const [saved, setSaved] = useState(false);

  const stored = new Map<string, string>();
  for (const row of ceilings.data ?? []) {
    stored.set(`${row.persona}:${row.risk_class}`, row.level);
  }

  function storedLevel(persona: AiPersona, risk: AiRiskClass): AiCeilingLevel {
    return levelOf(stored.get(cellKey(persona, risk)));
  }

  function shownLevel(persona: AiPersona, risk: AiRiskClass): AiCeilingLevel {
    return draft[cellKey(persona, risk)] ?? storedLevel(persona, risk);
  }

  const changes: PendingChange[] = [];
  for (const persona of AI_PERSONAS) {
    if (persona === "end_user") continue;
    for (const risk of AI_RISK_CLASSES) {
      const from = storedLevel(persona, risk);
      const to = shownLevel(persona, risk);
      if (from !== to) changes.push({ persona, risk, from, to });
    }
  }
  const raising = changes.filter((change) => RANK[change.to] > RANK[change.from]);

  function edit(persona: AiPersona, risk: AiRiskClass, level: AiCeilingLevel) {
    // A "Saved." from the previous write must not sit above an unsent edit —
    // the rule `settings/SaveBar.tsx` already holds. The FAILURE is left
    // standing on purpose: "technician / high was not changed" stays true.
    setSaved(false);
    setDraft((current) => ({ ...current, [cellKey(persona, risk)]: level }));
  }

  function discard() {
    setSaved(false);
    setDraft({});
  }

  /**
   * One request per changed cell — the API has no bulk route (`PUT
   * /ai/ceilings` takes a single `(persona, risk_class, level)`), and inventing
   * a batch here would mean a partial failure with nothing to point at.
   *
   * IT STOPS AT THE FIRST REFUSAL and leaves the rest in the draft, so the grid
   * keeps showing exactly what has not been applied. Each cell that DID land is
   * dropped from the draft as it lands, so it stops counting as unsaved.
   */
  async function applyChanges(pending: readonly PendingChange[]): Promise<void> {
    setConfirming(false);
    setSaved(false);
    for (const change of pending) {
      try {
        await setCeiling.mutateAsync({
          persona: change.persona,
          risk_class: change.risk,
          level: change.to,
        });
      } catch {
        // Deliberately swallowed HERE and rendered BELOW: `setCeiling.error`
        // holds the refusal and `setCeiling.variables` names the cell, which is
        // more than a rethrow into an unhandled rejection would give anyone.
        return;
      }
      setDraft((current) => {
        const next = { ...current };
        delete next[cellKey(change.persona, change.risk)];
        return next;
      });
    }
    setSaved(true);
  }

  return (
    <>
      <PageHead
        title={t("aiAutomation.dial.title")}
        description={t("aiAutomation.dial.description")}
      />

      <AutomationPrerequisites />

      <Panel
        title={t("aiAutomation.dial.panelTitle")}
        subtitle={t("aiAutomation.dial.panelSubtitle")}
        bare
      >
        <div style={{ padding: "14px 18px 4px" }}>
          <p className="note">
            {t("aiAutomation.dial.emptyNote")}
          </p>

          {/* THE LEGEND, audit [MEDIUM]: the header copy was "excellent on policy
              but says nothing about these three values". */}
          <div style={{ margin: "12px 0" }}>
            {/* THE TERM IS THE WORDS, THE TOKEN IS BESIDE IT. The legend used
                to restate `off` / `suggest` / `require_approval`, so the
                underscore form was the entire vocabulary the page offered:
                the chips said it, the legend said it again and the persona
                column said it a third time. */}
            <KV
              items={AI_CEILING_LEVELS.map(
                (level) =>
                  [
                    <>
                      {/* Token UNDER the label, not beside it: `.kv`'s term
                          column is `max-content`, so "Needs approval
                          require_approval" on one line makes the widest term
                          200px and squeezes every definition into a ribbon at
                          390px. Stacked, the column is as wide as the token
                          alone, which is what it was before the label existed. */}
                      <div>{ceilingLabel(level)}</div>
                      <div className="note mono">{level}</div>
                    </>,
                    LEVEL_MEANING[level],
                  ] as const,
              )}
            />
          </div>
          <p className="note">
            {/* Audit [LOW]: "The administrator is setting a policy over a
                classification they cannot see." It is not set here and cannot be —
                it is a column on each approved action — so the page says where it
                comes from rather than implying this screen defines it. */}
            {t("aiAutomation.dial.classNote")}
          </p>
        </div>

        {/* The failing cell is named from `setCeiling.variables` rather than left
            to be guessed — nine cells on a page, one alert, and "which one" is
            the first question. */}
        {setCeiling.isError && (
          <p role="alert" className="notice red" style={{ margin: "10px 18px 12px" }}>
            <Icon name={KIND_ICON.crit} size={16} />
            <span>
              {[
                setCeiling.variables
                  ? t("aiAutomation.dial.notChanged", {
                      persona: personaLabel(setCeiling.variables.persona),
                      risk: riskWord(setCeiling.variables.risk_class),
                    })
                  : "",
                describeSubmitError(setCeiling.error),
                changes.length > 0 ? t("aiAutomation.dial.gridStill") : "",
              ]
                .filter((part) => part !== "")
                .join(" ")}
            </span>
          </p>
        )}

        <QueryState
          loading={ceilings.isLoading}
          error={ceilings.error}
          empty={false}
          emptyLabel=""
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("aiAutomation.dial.persona")}</Th>
                {AI_RISK_CLASSES.map((risk) => (
                  <Th key={risk}>{t("aiAutomation.dial.riskColumn", { risk: riskWord(risk) })}</Th>
                ))}
              </tr>
            </thead>
            <tbody>
              {AI_PERSONAS.map((persona: AiPersona) => (
                <tr key={persona}>
                  <Td lead={<Icon name={persona === "end_user" ? "user" : "automation"} size={18} />}>
                    <div className="cell-title">{personaLabel(persona)}</div>
                    {/* The identifier stays, under the name rather than as it:
                        `end_user` is what the API stores and what an operator
                        reading the docs will search for, and it is not the
                        name of a row on a screen. */}
                    <div className="cell-sub mono">{persona}</div>
                    {/* Said ONCE on the row rather than three times across its
                        cells, and said on the page rather than in a `title`
                        nobody on a keyboard can open. */}
                    {persona === "end_user" ? (
                      <div className="cell-sub">
                        {t("aiAutomation.dial.endUserNote")}
                      </div>
                    ) : null}
                  </Td>
                  {AI_RISK_CLASSES.map((risk: AiRiskClass) => (
                    <Td key={risk}>
                      {persona === "end_user" ? (
                        /* THE FIXED CELL IS LABELLED LIKE EVERY OTHER CELL.
                           It read `off (fixed)` — the wire token, lower case,
                           in the one column where the two rows below it say
                           "Off", "Suggest only" and "Needs approval". The
                           value is the same value, so it is written the same
                           way; a pill rather than a chip because there is
                           nothing here to press, which is the whole point of
                           the row. The reason it cannot be pressed was a
                           `title` tooltip, which a keyboard or a screen reader
                           never reaches — it is a line on the page now. */
                        <div data-testid={`dial-${persona}-${risk}`}>
                          <StatusPill value="off" kind="mut" label={ceilingLabel("off")} />
                          <div className="form-hint" style={{ marginTop: 4 }}>
                            {t("aiAutomation.dial.fixed")}
                          </div>
                        </div>
                      ) : (
                        <>
                          {/* CHIPS, WHICH IS WHAT v3 DRAWS FOR THIS CONTROL:
                              `field('Level',chips(['Answer only','Propose
                              actions','Run low-risk actions'],1))` on
                              `P['assistant/autonomy']`. It was a native
                              `<select>`, and a native select paints the
                              browser's own widget through everything the kit
                              puts on the box — so six of these were the one
                              part of the page wearing the UA's chrome. It also
                              CLIPPED: at 390px the box was narrower than
                              `require_approval`, which a select truncates
                              without an ellipsis, so the admin / low cell read
                              "require_app" and the stored ceiling was
                              unreadable on a handset. Chips wrap instead of
                              clipping, and every position is on screen at once
                              rather than behind a popup. */}
                          <div data-testid={`dial-${persona}-${risk}`}>
                            <Chips
                              items={AI_CEILING_LEVELS.map(ceilingLabel)}
                              active={AI_CEILING_LEVELS.indexOf(shownLevel(persona, risk))}
                              disabled={!canSet}
                              onSelect={(at) => {
                                const level = AI_CEILING_LEVELS[at];
                                if (level !== undefined) edit(persona, risk, level);
                              }}
                              label={t("aiAutomation.dial.cellLabel", { persona: personaLabel(persona), risk: riskWord(risk) })}
                            />
                          </div>
                          {draft[cellKey(persona, risk)] != null &&
                          shownLevel(persona, risk) !== storedLevel(persona, risk) ? (
                            <div className="form-hint" style={{ marginTop: 4 }}>
                              {t("aiAutomation.dial.unsaved", { level: ceilingLabel(storedLevel(persona, risk)) })}
                            </div>
                          ) : null}
                        </>
                      )}
                    </Td>
                  ))}
                </tr>
              ))}
            </tbody>
          </Table>
        </QueryState>

        {canSet ? (
          <div className="form-actions" style={{ margin: 0, padding: "14px 18px 16px", borderTop: "1px solid var(--line)" }}>
            <Btn
              label={setCeiling.isPending ? t("aiAutomation.dial.saving") : t("aiAutomation.dial.save")}
              icon="check"
              variant="primary"
              disabled={changes.length === 0 || setCeiling.isPending}
              onClick={() => setConfirming(true)}
            />
            {changes.length > 0 ? (
              <>
                <Btn label={t("aiAutomation.dial.discard")} onClick={discard} />
                {/* NOT `role="alert"`: this is the state of the form, announced as
                    the operator arrives at it, not an interruption. */}
                <span role="status" className="form-hint">
                  {t("aiAutomation.dial.unsavedChanges", {
                    count: changes.length,
                    changes: changes.map(describeChange).join(changes.length === 1 ? "" : "; "),
                  })}
                </span>
              </>
            ) : null}
            {saved && changes.length === 0 && !setCeiling.isError ? (
              <span role="status" className="form-saved">
                <Icon name={KIND_ICON.ok} size={13} />
                {t("aiAutomation.dial.saved")}
              </span>
            ) : null}
          </div>
        ) : (
          <p className="note" style={{ margin: 0, padding: "14px 18px 16px" }}>
            {t("aiAutomation.dial.everyClientOnly")}
          </p>
        )}
      </Panel>

      <WaitingActionsPanel />

      <ConfirmDialog
        open={confirming && changes.length > 0}
        title={raising.length > 0 ? t("aiAutomation.dial.raiseTitle") : t("aiAutomation.dial.changeTitle")}
        subject={changes.map(describeChange).join("; ")}
        body={`${changes
          .map((change) =>
            t("aiAutomation.dial.bodyLine", {
              persona: personaLabel(change.persona),
              risk: riskWord(change.risk),
              meaning: LEVEL_MEANING[change.to],
            }),
          )
          .join(" ")} ${t("aiAutomation.dial.bodyTail")}`}
        confirmLabel={raising.length > 0 ? t("aiAutomation.dial.raiseConfirm") : t("aiAutomation.dial.changeConfirm")}
        // A raise WIDENS what an AI may prepare against the fleet. Lowering it
        // again undoes the setting, but not anything that ran while it was
        // raised — which is what `destructive` is for here.
        destructive={raising.length > 0}
        onConfirm={() => void applyChanges(changes)}
        onCancel={() => setConfirming(false)}
      />
    </>
  );
}

/**
 * v3's "Actions waiting for approval": the prepared actions nobody has
 * answered yet, from the same `GET /ai/actions` the actions page lists.
 *
 * NO APPROVE BUTTON ON THE ROW, where v3 draws one. Confirming an action is
 * §6.3's two-region screen — what will run, from the catalogue, beside why it
 * was suggested, from the model — and a one-region row cannot honour that
 * separation. Each row leads to that screen instead; the decision is made
 * there and only there.
 */
function WaitingActionsPanel(): ReactElement {
  const t = useT();
  const actions = useAiActions();
  const zone = useProfileZone();
  const waiting = (actions.data ?? []).filter((action) => action.state === "previewed");
  /* THE ROW IS TITLED WITH THE CATALOGUE'S SENTENCE, NOT ITS KEY. v3 draws
     this row as "Approve KB5041585 into ring “test”"; the list payload carries
     `patch.approve.ring` and `patch.approve`, which is an API key beside a
     permission code and tells a reviewer nothing about what would run. The
     name is `ActionPreviewOut.name` — org-admin-authored, catalogue-derived,
     the same field the confirmation screen makes its h1 — read through the
     detail route by `useAiActionNames`, which is where the rest of that
     header explains why the extra read is the one the "Review" link was about
     to make anyway. A name that does not arrive leaves the key as the title,
     so a row never goes untitled. */
  const names = useAiActionNames(waiting.map((action) => action.id));
  return (
    <Panel
      title={t("aiAutomation.waiting.title")}
      subtitle={t("aiAutomation.waiting.subtitle")}
      bare
      actions={
        <Link to="/assistant/actions" className="btn small">
          <span>{t("aiAutomation.waiting.allActions")}</span>
          <Icon name="chevron-right" size={14} />
        </Link>
      }
    >
      <QueryState
        loading={actions.isLoading}
        error={actions.error}
        empty={waiting.length === 0}
        emptyLabel={t("aiAutomation.waiting.empty")}
        emptyIcon="automation"
      >
        <Table>
          <thead>
            <tr>
              <Th>{t("aiAutomation.waiting.action")}</Th>
              <Th>{t("aiAutomation.waiting.risk")}</Th>
              <Th>{""}</Th>
            </tr>
          </thead>
          <tbody>
            {waiting.map((action) => (
              <tr key={action.id}>
                <Td
                  lead={<Icon name="automation" size={18} />}
                  gutter={action.destructive ? "crit" : action.tainted ? "warn" : undefined}
                >
                  <div className="cell-title">
                    <Link to={`/assistant/actions/${action.id}`}>
                      {names[action.id] ?? action.approved_action_key}
                    </Link>
                  </div>
                  {/* The identifier stays under the name rather than as it —
                      the rule the persona column already follows. Which
                      identifier depends on what the title became: the
                      catalogue key when the title is the catalogue's sentence,
                      the job type when the key IS the title, so the row never
                      prints one string twice. The persona is in words for the
                      same reason the dial above it is. */}
                  <div className="cell-sub">
                    <span className="mono">
                      {names[action.id] === undefined ? action.job_type : action.approved_action_key}
                    </span>
                    {` · ${t("aiAutomation.waiting.sub", { persona: personaLabel(action.persona), when: formatDateTime(action.created_at, zone) })}`}
                  </div>
                </Td>
                <Td>
                  <StatusPill value={action.risk_class} label={riskClassLabel(action.risk_class)} />
                </Td>
                <Td>
                  <Link to={`/assistant/actions/${action.id}`} className="btn small">
                    <Icon name="eye" size={14} />
                    <span>{t("aiAutomation.waiting.review")}</span>
                  </Link>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </QueryState>
    </Panel>
  );
}
