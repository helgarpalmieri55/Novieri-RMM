import type { UseQueryResult } from "@tanstack/react-query";
import { EmptyState } from "../../components/emptystate";
import { useT } from "../../i18n/context";
import { Panel, Progress, QueryState } from "../../shell/components";
import {
  DEFAULT_SEVERITIES,
  complianceBandLabel,
  compliancePctTone,
  compliancePlatformLabel,
  complianceToneLegend,
  formatPct,
} from "./patchCopy";
import { severityLabel } from "./patchState";
import type { FleetReport } from "./types";

/**
 * WHAT THE OPERATOR REJECTED, AND WHAT SURVIVED IT.
 *
 * 2026-08-27: "two tiles on the bottom that shouldn't be there and don't look
 * good and don't have the same theme of the other tiles". They are right about
 * every part of that. This was TWO `<Panel>`s, side by side at the foot of a
 * long page, each rendering a bare `<dl>` of flex rows — "linux ....... 0.0%".
 * On a one-Linux-box fleet the whole of it read "linux 0.0%" and "important
 * 100.0% / unknown 16.7%", which is four numbers of nothing occupying the last
 * screenful of the module. And they read as broken stat tiles, because a
 * label-left/number-right row at the bottom of a page whose top is four stat
 * tiles is what a stat tile looks like when its treatment has been lost.
 *
 * DELETING IT WAS A REAL OPTION AND IT IS NOT WHAT WAS CHOSEN, because the
 * numbers above the table do not say this. The four tiles count updates and
 * machines; none of them answers "Linux is at 71% while Windows is at 94%",
 * which is the question that decides which platform gets the next maintenance
 * window. `GET /patch/report` computes both splits from the same query the
 * exposure counts come from, so the data costs nothing extra either.
 *
 * SO IT IS ONE PANEL, IN THE CONSOLE'S OWN LANGUAGE. A percentage in this
 * product is a `Progress` bar — that is what the device table's Patch column
 * has always used — and a bar cannot be mistaken for a stat tile. Two labelled
 * groups inside one panel rather than two panels: the split by platform and
 * the split by severity are two readings of one measurement, and a section
 * heading is not owed a card each.
 *
 * THE EMPTY STATE IS THE ONE THE PREVIOUS AUDIT ASKED FOR, kept verbatim in
 * substance. AUDITOR A, [MEDIUM]: "Two chart panels use a device/monitor icon
 * and identical copy for their empty state ... repeating one icon and one
 * sentence across three semantically different panels means the empty states
 * carry no information at all. Neither card explains WHY there is no data —
 * here, because no patch scan has ever completed." `QueryState`'s empty branch
 * takes a glyph and one sentence and its glyph DEFAULTS to `devices`, the
 * monitor the audit saw in a percentage chart; `EmptyState` is the component
 * that carries a title, the cause and the way out. So `QueryState` still owns
 * loading and failure (its `role="alert"` is the announced read-path error)
 * and `empty={false}` says "the empty case is mine". Collapsing two panels to
 * one collapses two empty states to one — which is the fix the audit wanted in
 * the first place, since the two it was complaining about said the same thing.
 *
 * NO SCAN BUTTON IN THE CARD, deliberately, and unchanged. The way out is
 * named in words instead: a second dispatch control whose only feedback is the
 * `role="status"` beside the header's Scan button is A65 in miniature — a
 * fleet-wide scan queued from mid-page with nothing on screen saying it
 * happened is exactly the click-again loop that produced the dashboard's
 * duplicate `patch.scan` jobs.
 */
export function CompliancePanels({
  report,
  canScan,
}: {
  report: UseQueryResult<FleetReport>;
  canScan: boolean;
}) {
  const t = useT();
  const byOsFamily = report.data?.compliant_pct_by_os_family ?? {};
  const bySeverity = report.data?.compliant_pct_by_severity ?? {};
  const nothingMeasured =
    Object.keys(byOsFamily).length === 0 && Object.keys(bySeverity).length === 0;

  return (
    <Panel
      title={t("patch.compliance.title")}
      subtitle={t("patch.compliance.subtitle")}
    >
      <QueryState loading={report.isLoading} error={report.error} empty={false} emptyLabel="">
        {nothingMeasured ? (
          <EmptyState
            icon="chart"
            title={t("patch.compliance.emptyTitle")}
            body={canScan ? t("patch.compliance.emptyScan") : t("patch.compliance.emptyNoScan")}
          />
        ) : (
          <>
            {/* THE LEGEND IS ABOVE THE BARS, NOT UNDER THEM. A reader who has
                already taken the gold bar beside "critical" for the severity's
                own colour has made the mistake the sentence exists to prevent;
                a caption below would arrive after it. See
                `complianceToneLegend` for the review that asked for it. */}
            <p className="note">{complianceToneLegend()}</p>
            {/* TWO BANDS STACKED, NOT TWO COLUMNS SIDE BY SIDE. Review,
                2026-09-08 [MINOR, composition]: "the 'By platform' column ends
                three rows short of 'By severity', leaving roughly 120px of
                empty panel under it at 1440px. The two columns of one panel do
                not share a baseline or a bottom edge." They cannot: one split
                has three rows and the other five, and no amount of alignment
                makes a three-row column end where a five-row column ends. Any
                fix that keeps the columns is padding one of them out.

                So the columns go. Each split is now a full-width band whose
                bars sit in one row of equal cells, and the two bands share a
                left edge, a right edge and a rhythm. The panel ends where its
                last band ends, there is no void under anything, and the block
                is shorter than the one it replaces at the foot of a long page.

                WHAT THIS COSTS AND WHY IT IS WORTH IT: bars stacked on a shared
                left edge are the easiest length comparison there is, and a row
                of equal-width cells is a hair harder. It is the small-multiple
                trade, and three or five bars of a common scale still answer
                "which is worst" at a glance -- while a column of bars 1100px
                wide, which is what one full-width stack would give, answers it
                worse than either. */}
            <div className="mt-3" style={{ display: "grid", gap: "18px" }}>
              <ComplianceGroup
                heading={t("patch.compliance.byPlatform")}
                hint={t("patch.compliance.byPlatformHint")}
                entries={byOsFamily}
                /* The tab strip's own names, so one screen does not spell one
                   platform two ways. */
                labelFor={compliancePlatformLabel}
                /* Every platform is held to the same target: a fleet is meant to
                   be current on all three, so any of them drifting is a fact
                   somebody should act on. */
                toned={() => true}
              />
              <ComplianceGroup
                heading={t("patch.compliance.bySeverity")}
                hint={t("patch.compliance.bySeverityHint")}
                entries={bySeverity}
                /* The severity pill's own names, for the same reason. */
                labelFor={severityLabel}
                /* AND THIS SPLIT'S MEANING IS THE OTHER WAY UP, which the first
                   version got wrong: it painted "low 55.0%" red. Nobody is
                   chasing 100% on low-severity updates, so an alarm colour there
                   reports a problem that does not exist — and DESIGN.md's
                   Rationed Hue Rule is that a colour must be carrying
                   information. The severities that ARE a decision are the two
                   the console's own filter defaults to, so that constant decides
                   which rows get a tone and the rest take the neutral bar. */
                toned={(severity) =>
                  (DEFAULT_SEVERITIES as readonly string[]).includes(severity)
                }
              />
            </div>
          </>
        )}
      </QueryState>
    </Panel>
  );
}

/**
 * One split, as a list of bars.
 *
 * A `<dl>`, still — each row genuinely is a term and its value, and that is
 * what a screen reader should hear. What changed is the value: a bare "0.0%"
 * on the right-hand edge of a flex row carries no comparison, and comparison
 * is the entire reason to split a percentage by anything. Three bars of
 * different lengths answer "which is worst" before the numbers are read.
 *
 * THE BAR IS NOT THE ONLY CHANNEL. `Progress` renders a real `role="progressbar"`
 * with `aria-valuenow` and `aria-valuetext`, and the number is printed beside
 * it in tabular figures — so the tone, the length and the digits all say the
 * same thing, and none of them is load-bearing alone (WCAG 2.2 SC 1.4.1).
 */
function ComplianceGroup({
  heading,
  hint,
  entries,
  labelFor,
  toned,
}: {
  heading: string;
  hint: string;
  entries: Record<string, number>;
  /** The wire key as a person reads it. The two splits carry two different
   *  vocabularies -- os families and severities -- and each already has a
   *  display name elsewhere on this page; passing the function in is what
   *  stops this panel inventing a third. */
  labelFor: (key: string) => string;
  /** Whether this row's percentage is being measured against a target. A row
   *  that is not gets the neutral bar: its length still carries the
   *  comparison, and no hue claims something is wrong. */
  toned: (key: string) => boolean;
}) {
  const t = useT();
  const rows = Object.entries(entries);
  return (
    <section>
      {/* Heading and hint on one line where there is room for it: the band's
          own bars are the content, and a two-line header above three bars is
          the header outweighing them. They wrap to two lines below ~700px. */}
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "baseline", gap: "2px 10px" }}>
        <h3 className="cell-title">{heading}</h3>
        <p className="note">{hint}</p>
      </div>
      {rows.length === 0 ? (
        // MEASURED-NOTHING IS NOT ZERO. One split can be populated while the
        // other is not -- a fleet whose devices all report but whose updates
        // carry no severity has an OS split and an empty severity split -- and
        // an empty group rendering as a row of 0% bars would read as total
        // non-compliance rather than as an absent measurement.
        <p className="note mt-3">{t("patch.compliance.noSplit")}</p>
      ) : (
        <dl
          className="mt-3"
          /* ONE PITCH, AND BOTH BANDS ARE ON IT. `auto-fill`, not `auto-fit`,
             and the same template in both groups: the two bands sit in the
             same container at the same width, so they resolve to the same
             number of tracks and every cell in the panel is the same width.
             That is not a tidiness point -- with `auto-fit` the three-platform
             band collapsed its empty tracks to cells half again as wide as the
             five-severity band's, and "Linux 33.3%" drew visibly longer than
             "Important 33.3%". Two bars of one panel showing one number at two
             lengths is a worse defect than the one this layout was fixing. The
             cost is the trailing empty tracks after macOS, which is what a
             wrapping grid with three items in it looks like.

             Below ~440px of panel it resolves to a single column and every bar
             is full width, which is the phone layout and needs no media query
             this file cannot write. */
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(190px, 1fr))",
            gap: "0 22px",
          }}
        >
          {/* v3's `.prog`: the label line above the bar with the figure on
              its right in mono, the bar below. A `<dl>` still, because each
              row genuinely is a term and its value; the term carries its own
              figure so the group stays `dt` then `dd`, which is what the
              list's validity (axe `definition-list`) requires. */}
          {rows.map(([key, pct]) => {
            // `mut`, NOT `""`. An untoned `Progress` paints in the running
            // blue, because everywhere else in the console an untoned bar is
            // a job in flight -- and these rows are neither running nor held
            // to a target, so the neutral rung is what they mean.
            // `complianceBandLabel` already reads "not held to a target" for
            // anything outside the three bands.
            const tone = toned(key) ? compliancePctTone(pct) : "mut";
            const name = labelFor(key);
            return (
              <div key={key} className="prog" style={{ minWidth: 0 }}>
                <dt className="prog-l">
                  <span>{name}</span>
                  <span className="mono">{formatPct(pct)}</span>
                </dt>
                <dd>
                  {/* The band is IN THE NAME, not only in the hue: a bar
                      announced as "Critical compliance, by severity, behind
                      target" says what the red is for, which is the half of
                      the legend a screen reader would otherwise never reach. */}
                  <Progress
                    value={pct}
                    tone={tone}
                    label={t("patch.compliance.barLabel", {
                      name,
                      group: heading.toLowerCase(),
                      band: complianceBandLabel(tone),
                    })}
                  />
                </dd>
              </div>
            );
          })}
        </dl>
      )}
    </section>
  );
}
