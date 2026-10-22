import { Link } from "react-router-dom";
import { describeSubmitError } from "../../components/form";
import { Badge } from "../../components/ui";
import { useT } from "../../i18n/context";
import { ApiError } from "../../lib/api";
import { Panel, StatCard, StatsGrid } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { KIND_ICON } from "../../shell/viz";
import { useHealthAlerts } from "./api";
import { formatSeconds, summariseAlerts } from "./healthState";

// Alert volume, MTTR and the machine/human split (spec §7.4's obligation, §13).
//
// TWO RULES ARE APPLIED AND BOTH ARE VISIBLE ON THE PANEL, not buried in the
// arithmetic:
//
//   1. `unavailable_timeout`, `policy_removed` and `device_inactive` are
//      EXCLUDED from every resolution count and from MTTR. In all three the
//      alert went away because the QUESTION went away — the sensor stopped
//      answering, someone deleted the check, the machine left the fleet. They
//      are shown as their own number rather than dropped, because a report that
//      silently discards them makes a shrinking backlog look like progress.
//   2. Machine-resolved and human-closed are SEPARATE numbers. Conflating them
//      makes auto-remediation look effective in exact proportion to how often
//      people give up and close things by hand.
//
// WHAT THIS PANEL IS NOT, said on the panel itself. Spec §7.4 defines the
// machine/human split on `ticket_status_history.actor_type='system'` for an
// `origin='alert'` ticket. That history IS routed —
// `GET /service-desk/tickets/{id}/history` returns `actor_type` — so the
// obstruction is not access to it. It is that the POPULATION cannot be
// selected: `GET /service-desk/tickets` has no `origin` filter, so
// alert-origin tickets cannot be enumerated, and the history is per-ticket
// with no aggregate, so the split would be an N+1 walk over a set this API
// cannot produce. The split here is therefore made on the ALERT's own
// `cleared_reason` — "what closed the alert", not "who closed its ticket".
// The caveat is rendered rather than commented, because a number whose
// definition differs from the spec's and does not say so is worse than no
// number.
//
// **EVERY FIGURE HERE LINKS TO THE ALERTS IT WAS COUNTED OVER**, which is the
// operator's rule ("no dead tiles") and, on this panel, also a correctness
// property. Each number is a judgement — what counts as open, what counts as
// resolved, what a machine resolved rather than a person — and the only way a
// reader can check one is to see the rows behind it. The destinations use
// `AlertConsole`'s URL filters, which apply `healthState.ts`'s SAME
// predicates, so the list a figure opens contains exactly the alerts the
// figure counted. A tile whose link showed a different population would be a
// worse defect than a tile with no link at all.
//
// COLOUR IS CATEGORY: red for what is still wrong, green for what the fleet
// fixed by itself, gold for a close that is not evidence of a fix, blue for
// a measurement, neutral for the inert total. WEIGHT IS URGENCY, so "Still
// open" is the one tile carrying it — it is the only number on this panel a
// person is meant to do something about.

/** The alert list, filtered the way `AlertConsole` reads its URL. Written once
 *  here so a tile's destination and the console's parameter names cannot drift
 *  apart in two files. */
const ALERTS = "/health";
const OPEN_ALERTS = "/health?state=open";
const RESOLVED_ALERTS = "/health?state=cleared&resolution=resolved";
const MACHINE_RESOLVED_ALERTS = "/health?state=cleared&resolution=machine";
const HUMAN_CLOSED_ALERTS = "/health?state=cleared&resolution=human";
const EXCLUDED_ALERTS = "/health?state=cleared&resolution=excluded";

export function HealthReportPanel() {
  const t = useT();
  // The console's own list, unfiltered: `summariseAlerts` needs cleared rows,
  // and the default filter shows them.
  const alerts = useHealthAlerts({});
  const forbidden = alerts.error instanceof ApiError && alerts.error.status === 403;

  if (forbidden) {
    return (
      <Panel title={t("health.report.title")}>
        <p role="alert" className="note">
          {t("health.report.forbidden")}
        </p>
      </Panel>
    );
  }

  // THE WORST SWALLOWED FAILURE IN THIS MODULE. `summariseAlerts(alerts.data ??
  // [])` over a failed fetch yields zeroes, and the panel renders them as
  // FINDINGS: "Alerts in view 0", "Still open 0", "0 excluded". Every number a
  // manager reads off this panel is good news, and every one of them is an
  // artefact of a request that never returned. A report that cannot say where
  // its numbers came from must not print numbers.
  if (alerts.error) {
    return (
      <Panel title={t("health.report.title")}>
        <div role="alert" className="notice red">
          <Icon name={KIND_ICON.crit} size={17} />
          <div>{t("health.report.loadFailed", { detail: describeSubmitError(alerts.error) })}</div>
        </div>
      </Panel>
    );
  }

  const summary = summariseAlerts(alerts.data ?? []);
  const autoRate =
    summary.resolved > 0 ? Math.round((summary.machineResolved / summary.resolved) * 100) : null;

  return (
    <Panel
      title={t("health.report.title")}
      subtitle={t("health.report.subtitle")}
    >
      <StatsGrid columns={3}>
        <StatCard
          label={t("health.report.inView")}
          value={String(summary.total)}
          icon="bell"
          to={ALERTS}
          tone="neutral"
          zeroLabel={t("health.report.inViewZero")}
        />
        <StatCard
          label={t("health.report.stillOpen")}
          value={String(summary.open)}
          icon="warning"
          to={OPEN_ALERTS}
          tone="red"
          urgent
          // ACKNOWLEDGED AND MUTED ARE OPEN. The destination is the console's
          // "Open (any state)" filter for that reason: a link to `active`
          // alone would show fewer rows than this number claims.
          zeroLabel={t("health.report.stillOpenZero")}
          foot={t("health.report.stillOpenFoot")}
        />
        <StatCard
          label={t("health.report.mttr")}
          value={
            summary.meanTimeToResolveS === null
              ? "—"
              : formatSeconds(Math.round(summary.meanTimeToResolveS))
          }
          icon="clock"
          to={RESOLVED_ALERTS}
          tone="blue"
          foot={t("health.report.mttrFoot")}
        />
        <StatCard
          label={t("health.report.machine")}
          value={String(summary.machineResolved)}
          icon="automation"
          to={MACHINE_RESOLVED_ALERTS}
          tone="green"
          zeroLabel={t("health.report.machineZero")}
          foot={t("health.report.machineFoot")}
        />
        <StatCard
          label={t("health.report.human")}
          value={String(summary.humanClosed)}
          icon="user"
          to={HUMAN_CLOSED_ALERTS}
          tone="gold"
          zeroLabel={t("health.report.humanZero")}
          foot={t("health.report.humanFoot")}
        />
        <StatCard
          label={t("health.report.autoRate")}
          value={autoRate === null ? "—" : `${autoRate}%`}
          icon="activity"
          to={RESOLVED_ALERTS}
          tone="green"
          foot={t("health.report.autoRateFoot")}
        />
      </StatsGrid>

      <div className="note flex flex-wrap items-center gap-2">
        {/* A NUMBER, SO IT GOES SOMEWHERE TOO. It is a badge rather than a
            seventh tile because the sentence beside it is what makes it
            readable, and that sentence does not fit in a tile. */}
        <Link to={EXCLUDED_ALERTS}>
          <Badge variant={summary.excludedFromResolution > 0 ? "warning" : "muted"}>
            {t("health.report.excluded", { count: summary.excludedFromResolution })}
          </Badge>
        </Link>
        <span>{t("health.report.excludedNote")}</span>
      </div>

      {/* KEPT: the figures above are a proxy for the measure the spec
          defines, and a page that says nothing is claiming they are the same
          number. What went is the API archaeology explaining WHY -- that is a
          docs page. */}
      <p className="note mt-3">{t("health.report.caveat")}</p>
    </Panel>
  );
}
