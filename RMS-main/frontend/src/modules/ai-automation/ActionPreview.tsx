// THE CONFIRMATION SCREEN, WHOSE LAYOUT IS A SECURITY PROPERTY.
//
// docs/modules/ai-automation.md §6.3. **THE TWO REGIONS MAY NOT MERGE.**
//
//   * "What will happen" is rendered ONLY from the confirm payload's
//     catalogue-derived fields: name, description, key, job type, redacted
//     params, script name and version, target hostname and id, risk class,
//     destructive flag, effective autonomy, taint, and the fuse verdict.
//   * "Why it was suggested" is the model's `rationale`, in a visually distinct,
//     clearly-labelled region, rendered as TEXT and never as markup.
//
// A UI that lets the second region describe the first re-introduces the whole
// injection problem AT THE CONFIRMATION STEP: a laundered instruction rendering
// as an authoritative action description is a confirmation the human did not
// really give. The rationale is downstream of ticket bodies an internet sender
// can write; the fields above are downstream of a catalogue row an org admin
// authored through an audited, validated write path.
//
// **`rationale` IS RENDERED AS `{...}` TEXT, NOT THROUGH THE MARKDOWN
// RENDERER.** `modules/ai-admin/aiMarkdown.ts` exists for ANSWERS; using it here
// would let attacker-influenced prose emit headings, emphasis and link text
// inside the one dialog whose whole job is to be unambiguous about what runs.
// React escapes a string child, so this is the strong form and not a
// convention.
//
// DRAWN AS v3's `assistant/actions/:actionId` SCREEN: the facts strip
// (action, target, risk, reversibility) over a panel of prose. Region 1 is
// the strip plus a key/value list of everything else the catalogue says;
// region 2 is the "Why it was proposed" panel, whose title and sub-line say
// in words what the previous design said with a dashed edge: this is the
// assistant's argument, and it describes nothing above it.

import type { ReactElement } from "react";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { KIND_ICON, Panel, StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { Facts, KV } from "../../shell/viz";
import { jobTypeLabel } from "../jobs/jobTypes";
import { autonomyLabel, riskClassLabel, type AiActionPreviewOut } from "./types";

/** Test hooks. Named constants so a test asserts on the SAME string the
 * component renders, and a renamed region fails loudly instead of silently
 * making an assertion pass over nothing. */
export const ACTION_REGION_TESTID = "ai-action-what-will-happen";
export const RATIONALE_REGION_TESTID = "ai-action-why-suggested";

function remaining(expiresAt: string, now: number): string {
  const seconds = Math.floor((Date.parse(expiresAt) - now) / 1000);
  if (Number.isNaN(seconds)) return translate("aiAutomation.preview.unknown");
  if (seconds <= 0) return translate("aiAutomation.preview.expired");
  const minutes = Math.floor(seconds / 60);
  return minutes > 0 ? `${minutes}m ${seconds % 60}s` : `${seconds}s`;
}

export function ActionPreview({
  preview,
  now = Date.now(),
}: {
  preview: AiActionPreviewOut;
  now?: number;
}): ReactElement {
  const t = useT();
  const details: (readonly [string, ReactElement | string])[] = [
    [t("aiAutomation.preview.catalogueKey"), <span className="mono">{preview.approved_action_key}</span>],
    [t("aiAutomation.preview.description"), preview.description],
    [
      t("aiAutomation.preview.autonomy"),
      <>
        {autonomyLabel(preview.effective_autonomy)}{" "}
        <span className="note mono">{preview.effective_autonomy}</span>
      </>,
    ],
  ];
  if (preview.script_name !== null) {
    details.push([
      t("aiAutomation.preview.script"),
      <span className="mono">{`${preview.script_name} v${preview.script_version ?? "?"}`}</span>,
    ]);
  }
  if (preview.redacted_param_paths.length > 0) {
    details.push([t("aiAutomation.preview.redacted"), <span className="mono">{preview.redacted_param_paths.join(", ")}</span>]);
  }
  if (preview.fuse_verdict !== null) {
    details.push([t("aiAutomation.preview.rateLimit"), preview.fuse_verdict]);
  }

  // §6.3 REQUIRES THE RESOLVED PARAMETERS IN FULL. IT DOES NOT REQUIRE JSON.
  // `JSON.stringify(rendered_params)` in one mono cell put the whole object on
  // a line that `word-break: break-all` then split mid-identifier -- at 390px
  // `ring` wrapped as "rin / g" -- so the one row a person has to read
  // carefully was the least readable thing on the screen. One row per
  // parameter carries the same bytes: the key is the term, the value is the
  // definition, and neither is truncated. A value that is not a scalar (an
  // object or a list) is still serialised, because showing it in full is the
  // requirement and there is no shape to lay out generically.
  //
  // THEY ARE ROWS OF THE SAME LADDER, NOT A SECOND ONE. `.kv` is
  // `grid-template-columns: max-content 1fr`, so each `<dl>` measures its own
  // term column: two lists under one panel title put their values at x=385 and
  // x=350, a 35px step in the middle of one panel with both label columns
  // starting at the same x. One `<dl>` has one value column by construction.
  // The `Parameters` caption survives as a term of its own, which is also what
  // it is — the row that says the rows under it are the resolved parameters.
  const params = Object.entries(preview.rendered_params);
  const paramItems: (readonly [ReactElement, ReactElement])[] = params.map(([key, value]) => [
    <span className="mono">{key}</span>,
    <span className="mono" style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
      {typeof value === "string" ? value : JSON.stringify(value)}
    </span>,
  ]);

  return (
    <>
      {/* REGION 1 — catalogue-derived facts only. Every value below comes from
          `preview.<field>` and none of them is `preview.rationale`. */}
      <section data-testid={ACTION_REGION_TESTID} aria-label={t("aiAutomation.preview.whatWillHappen")}>
        <Panel
          title={t("aiAutomation.preview.whatWillHappen")}
          subtitle={t("aiAutomation.preview.fromCatalogue")}
        >
          <Facts
            items={[
              { k: t("aiAutomation.preview.action"), v: preview.name },
              {
                k: t("aiAutomation.preview.whatRuns"),
                v: (
                  <>
                    {jobTypeLabel(preview.job_type)} <span className="note mono">{preview.job_type}</span>
                  </>
                ),
              },
              {
                k: t("aiAutomation.preview.target"),
                v: (
                  <>
                    {preview.target_hostname}{" "}
                    <span className="note mono">{preview.target_device_id}</span>
                  </>
                ),
              },
              { k: t("aiAutomation.preview.risk"), v: <StatusPill value={preview.risk_class} label={riskClassLabel(preview.risk_class)} /> },
              {
                k: t("aiAutomation.preview.destructive"),
                v: preview.destructive ? (
                  <StatusPill value="destructive" kind="crit" label={t("aiAutomation.preview.yes")} />
                ) : (
                  <StatusPill value="not-destructive" kind="ok" label={t("aiAutomation.preview.no")} />
                ),
              },
              { k: t("aiAutomation.preview.expiresIn"), v: <span className="mono">{remaining(preview.expires_at, now)}</span> },
            ]}
          />
          <div style={{ marginTop: 14 }}>
            <KV
              items={[
                ...details,
                [
                  <span className="k">{t("aiAutomation.preview.parameters")}</span>,
                  <span className="note">
                    {paramItems.length === 0
                      ? t("aiAutomation.preview.noParameters")
                      : t("aiAutomation.preview.values", { count: paramItems.length })}
                  </span>,
                ] as const,
                ...paramItems,
              ]}
            />
          </div>
          {preview.tainted && (
            <p data-testid="ai-action-taint-badge" className="notice gold" style={{ marginTop: 14 }}>
              <Icon name={KIND_ICON.warn} size={16} />
              <span>{t("aiAutomation.preview.tainted")}</span>
            </p>
          )}
        </Panel>
      </section>

      {/* REGION 2 — the model's argument, labelled as such, rendered as TEXT.
          Nothing above is composed from it. The two subtitles are what carry
          that separation now; the page description no longer restates it. */}
      <section data-testid={RATIONALE_REGION_TESTID} aria-label={t("aiAutomation.preview.whyRegion")}>
        <Panel
          title={t("aiAutomation.preview.whyTitle")}
          subtitle={t("aiAutomation.preview.whySubtitle")}
        >
          <p style={{ whiteSpace: "pre-wrap", margin: 0, color: "var(--ink-2)" }}>
            {preview.rationale || t("aiAutomation.preview.noReason")}
          </p>
        </Panel>
      </section>
    </>
  );
}
