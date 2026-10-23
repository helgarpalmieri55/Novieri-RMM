import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { describeSubmitError } from "../../components/form";
import { EmptyState, NoValue } from "../../components/emptystate";
import { Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { formatDateTime, toInstant } from "../../lib/datetime";
import {
  outcomeKind,
  outcomeLabel,
  remediationKind,
  remediationLabel,
  severityKind,
  severityLabel,
  stepResultKind,
  stepResultLabel,
  testTypeLabel,
} from "./bcpState";
import { usePeriodReport, useTestReport, useUserTimezone } from "./api";
import { PageHead, Panel, StatsGrid, StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { Facts, Tile } from "../../shell/viz";

// Per-test SOC 2 evidence report (backend/app/modules/bcp/api.py's
// GET /bcp/tests/{id}/report) and the org-wide period rollup
// (GET /bcp/report/period). Fetching either writes a `bcp.report_exported`
// audit event server-side — this page is the explicit "export" action, never
// auto-fetched from a passive list/detail view (see reporting.py's
// module docstring).
export function BcpReport() {
  const { testId } = useParams<{ testId: string }>();
  if (testId) return <TestReportView testId={testId} />;
  return <PeriodReportView />;
}

/** yes / no / n/a, as words: a recovery objective that was not measured is not a miss. */
function met(value: boolean | null): string {
  if (value === null) return translate("bcp.report.na");
  return value ? translate("bcp.report.yes") : translate("bcp.report.no");
}

function TestReportView({ testId }: { testId: string }) {
  const t = useT();
  const report = useTestReport(testId);
  const zone = useUserTimezone();
  const test = report.data?.test ?? null;

  return (
    <div>
      <PageHead
        title={t("bcp.report.testTitle")}
        description={t("bcp.report.testDescription")}
        actions={
          <Link className="btn" to={`/bcp/tests/${testId}`}>
            <Icon name="chevron-left" size={14} />
            {t("bcp.report.back")}
          </Link>
        }
      />

      {report.isError && (
        <p role="alert" className="form-error mb-2">
          {t("bcp.report.loadFailed", { detail: describeSubmitError(report.error) })}
        </p>
      )}

      {test && (
        <>
          <h2 className="sr-only">{t("bcp.report.summary")}</h2>
          <Facts
            items={[
              { k: t("bcp.report.plan"), v: report.data?.plan?.name ?? t("bcp.report.na") },
              { k: t("bcp.report.type"), v: testTypeLabel(test.type) },
              {
                k: t("bcp.report.outcome"),
                v: (
                  <StatusPill
                    value={test.outcome}
                    kind={outcomeKind(test.outcome)}
                    label={outcomeLabel(test.outcome)}
                  />
                ),
              },
              { k: t("bcp.report.executed"), v: <span>{formatDateTime(test.executed_at, zone)}</span> },
              { k: t("bcp.report.rtoMet"), v: met(test.rto_met) },
              { k: t("bcp.report.rpoMet"), v: met(test.rpo_met) },
            ]}
          />
        </>
      )}

      <Panel title={t("bcp.report.steps")} bare>
        <Table>
          <thead>
            <tr>
              <Th>{t("bcp.report.colStep")}</Th>
              <Th>{t("bcp.report.colDescription")}</Th>
              <Th>{t("bcp.report.colResult")}</Th>
              <Th>{t("bcp.report.colEvidenceRef")}</Th>
            </tr>
          </thead>
          <tbody>
            {report.data?.steps.map((step) => (
              <tr key={step.id}>
                <Td>
                  <span className="mono">{String(step.ordinal).padStart(2, "0")}</span>
                </Td>
                <Td>{step.description}</Td>
                <Td>
                  <StatusPill
                    value={step.result}
                    kind={stepResultKind(step.result)}
                    label={stepResultLabel(step.result)}
                  />
                </Td>
                <Td>
                  {step.evidence_ref ? (
                    <span className="mono">{step.evidence_ref}</span>
                  ) : (
                    <NoValue label={t("bcp.report.noEvidenceRef")} />
                  )}
                </Td>
              </tr>
            ))}
            {report.data && report.data.steps.length === 0 && (
              <tr>
                <Td className="cell-sub" colSpan={4}>
                  {t("bcp.report.noSteps")}
                </Td>
              </tr>
            )}
          </tbody>
        </Table>
      </Panel>

      <Panel title={t("bcp.report.findings")} bare>
        <Table>
          <thead>
            <tr>
              <Th>{t("bcp.report.colDescription")}</Th>
              <Th>{t("bcp.report.colSeverity")}</Th>
              <Th>{t("bcp.report.colRemediation")}</Th>
            </tr>
          </thead>
          <tbody>
            {report.data?.findings.map((finding) => (
              <tr key={finding.id}>
                <Td lead={<Icon name="flag" size={18} />} gutter={severityKind(finding.severity)}>
                  {finding.description}
                </Td>
                <Td>
                  <StatusPill
                    value={finding.severity}
                    kind={severityKind(finding.severity)}
                    label={severityLabel(finding.severity)}
                  />
                </Td>
                <Td>
                  <StatusPill
                    value={finding.remediation_state}
                    kind={remediationKind(finding.remediation_state)}
                    label={remediationLabel(finding.remediation_state)}
                  />
                </Td>
              </tr>
            ))}
            {report.data && report.data.findings.length === 0 && (
              <tr>
                <Td className="cell-sub" colSpan={3}>
                  {t("bcp.report.noFindings")}
                </Td>
              </tr>
            )}
          </tbody>
        </Table>
      </Panel>
    </div>
  );
}

function PeriodReportView() {
  const t = useT();
  const zone = useUserTimezone();
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  // THE PERIOD IS THE EVIDENCE. Both fields are `datetime-local`, so they hand
  // back a wall clock with no zone; sent unqualified they moved this report's
  // boundaries by the reader's offset, silently including or excluding tests
  // at each end of a SOC 2 period. `toInstant` returns "" for an empty field,
  // so `usePeriodReport` stays disabled until both ends are filled in.
  const report = usePeriodReport(
    toInstant(from, zone) || undefined,
    toInstant(to, zone) || undefined,
  );

  const outcomes = Object.entries(report.data?.outcome_counts ?? {});
  const severities = Object.entries(report.data?.open_findings_by_severity ?? {});

  return (
    <div>
      <PageHead
        title={t("bcp.report.periodTitle")}
        description={t("bcp.report.periodDescription")}
        actions={
          <Link className="btn" to="/bcp">
            <Icon name="shield-check" size={14} />
            {t("bcp.report.allPlans")}
          </Link>
        }
      />

      <Panel title={t("bcp.report.period")}>
        {/* THE TWO ENDS OF ONE RANGE SIT ON ONE ROW, AND THE CONTROL KEEPS
            ITS OWN MEASURE.

            THE HISTORY, because it is why `full` was here. `.form-grid` used
            to be two columns at EVERY width, so on a 390px handset each column
            was about 150px -- and a `datetime-local` needs roughly 190px to
            draw `mm/dd/yyyy, --:-- --` AND the calendar-picker button the
            browser puts at its right edge. The button was clipped away
            entirely, which on this page is the whole function: nothing runs
            until both ends are set, so the only way in was typing the raw
            mask. `full` gave each field a row of its own, and that fixed it.

            THE KIT'S 640px FOLD HAS SINCE LANDED (`components.css`, the rule
            immediately after `.form-grid`): the grid is ALREADY one column on
            a handset. `full` was therefore doing nothing at 390 and
            everything at 1440, where it stacked a two-field range down the
            left of a 1140px card and left ~1100px of panel empty beside it
            (design review I7). Every sibling filter surface pairs its
            controls inline -- /audit's two inputs, /security's search and
            select and range chips -- so the two ends now share a row above
            the fold and stack below it, which is the grid's own behaviour
            and not a restatement of the breakpoint.

            `maxWidth` STAYS AND IS NOT THE FINDING. A date is a fixed measure;
            stretching the input to half a desktop card would be a second
            defect, not a fix for the first. What the review counted as empty
            was the SECOND COLUMN, and the second column now holds "To". */}
        {/* EACH END, READ BACK IN THE CONSOLE'S OWN FORMAT. The mask a native
            datetime input draws is the USER AGENT'S locale and no CSS
            reorders it, so both of these read `09/22/2026` to a US-locale
            browser on a console whose every rendered date says `22 Sept 2026`
            and whose /settings says day/month/year. The control stays -- it
            is the one datetime widget every keyboard, screen reader and
            locale already agrees on -- and says which moment it holds beside
            itself, the way `agent-push/PushAuthorizationsPage` already does
            (design review M1). On THIS page it earns its place twice over:
            the two boundaries are the evidence, and a period read a month
            wrong silently includes or excludes tests at each end.

            THE ECHO SITS UNDER THE GROUP, not inside either `<Field>`:
            `Field` wraps its control in the `<label>`, so text placed inside
            one becomes part of the field's NAME rather than its description.
            The full accounting is at `agent-push/PushAuthorizationsPage`. */}
        <div className="form-grid">
          <Field label={t("bcp.report.from")}>
            <input
              type="datetime-local"
              className={inputClass}
              style={{ maxWidth: "18rem" }}
              value={from}
              onChange={(e) => setFrom(e.target.value)}
            />
          </Field>
          <Field label={t("bcp.report.to")}>
            <input
              type="datetime-local"
              className={inputClass}
              style={{ maxWidth: "18rem" }}
              value={to}
              onChange={(e) => setTo(e.target.value)}
            />
          </Field>
        </div>
        {from !== "" || to !== "" ? (
          <p className="note" style={{ marginTop: 14 }}>
            {from !== "" && to !== ""
              ? t("bcp.report.periodBoth", {
                  from: formatDateTime(toInstant(from, zone), zone),
                  to: formatDateTime(toInstant(to, zone), zone),
                })
              : from !== ""
                ? t("bcp.report.periodFromOnly", { from: formatDateTime(toInstant(from, zone), zone) })
                : t("bcp.report.periodToOnly", { to: formatDateTime(toInstant(to, zone), zone) })}
          </p>
        ) : null}
        {/* THE HELPER LINE TAKES THE PANEL'S GAP. `.form-grid > .field` zeroes
            the field's bottom margin (the grid's own 14px gap does that job
            between rows), and `<p>` is reset to no margin, so this sentence
            sat ~4px under the control it explains where sibling helper text
            uses the standard gap. One grid gap below the fields is that gap. */}
        <p className="note" style={{ marginTop: 14 }}>
          {t("bcp.report.timesNote", { zone: zone ?? Intl.DateTimeFormat().resolvedOptions().timeZone })}
        </p>
      </Panel>

      {report.isError && (
        <p role="alert" className="form-error mb-2">
          {describeSubmitError(report.error)}
        </p>
      )}

      {/* GATED ON THE INPUTS, NOT ON THE QUERY. `!report.data` is also true
          while both ends are set and the run is in flight, and for that
          instant the page said no period was chosen when one had been. */}
      {!(from && to) && (
        <Panel>
          <EmptyState
            icon="calendar-event"
            title={t("bcp.report.noPeriodTitle")}
            body={t("bcp.report.noPeriodBody")}
          />
        </Panel>
      )}

      {report.isFetching && <p className="note">{t("bcp.report.running")}</p>}

      {report.data && (
        <>
          {/* v3's tiles, one per measured count. A count the server did not
              send is not drawn as a zero. */}
          <h2 className="sr-only">{t("bcp.report.outcomes")}</h2>
          {outcomes.length > 0 ? (
            <StatsGrid columns={Math.min(4, outcomes.length)}>
              {outcomes.map(([outcome, count]) => (
                <Tile
                  key={outcome}
                  label={outcomeLabel(outcome)}
                  value={count}
                  unit={t("bcp.report.unitTests", { count })}
                  kind={outcomeKind(outcome)}
                />
              ))}
            </StatsGrid>
          ) : (
            <Panel>
              <EmptyState
                icon="player-play"
                title={t("bcp.report.noTestTitle")}
                body={t("bcp.report.noTestBody")}
              />
            </Panel>
          )}

          <Panel title={t("bcp.report.openBySeverity")}>
            {severities.length > 0 ? (
              <StatsGrid columns={Math.min(4, severities.length)}>
                {severities.map(([severity, count]) => (
                  <Tile
                    key={severity}
                    label={severityLabel(severity)}
                    value={count}
                    unit={t("bcp.report.unitFindings", { count })}
                    kind={severityKind(severity)}
                  />
                ))}
              </StatsGrid>
            ) : (
              <p className="note">{t("bcp.report.noOpenFindings")}</p>
            )}
          </Panel>

          <Panel title={t("bcp.report.plans")}>
            <Facts
              items={[
                { k: t("bcp.report.testedThisPeriod"), v: report.data.plans_tested.length },
                { k: t("bcp.report.reviewOverdue"), v: report.data.review_overdue_plans.length },
              ]}
            />
          </Panel>
        </>
      )}
    </div>
  );
}
