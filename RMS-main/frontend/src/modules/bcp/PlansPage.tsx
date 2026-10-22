import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Button, Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDateTime } from "../../lib/datetime";
import { usePlanStatus, usePlans, useUserTimezone } from "./api";
import { outcomeKind, outcomeLabel, planStateLabel } from "./bcpState";
import { BcpReadiness } from "./BcpReadiness";
import { PlanEditor } from "./PlanEditor";
import {
  needsReview,
  planReviewStandings,
  reviewStandingKind,
  reviewStandingLabel,
} from "./reviewState";
import type { PlanOut, PlanStatusOut } from "./types";
import {
  PageHead,
  Panel,
  QueryState,
  StatCard,
  StatsGrid,
  StatusPill,
} from "../../shell/components";
import { Icon } from "../../shell/icons";

/**
 * The query-string keys this list is addressable by, so each tile above it can
 * open the rows behind its own number instead of dropping the reader into the
 * whole list. `state` is the plan's own wire vocabulary
 * (`bcp/service.py#_VALID_PLAN_STATES` — "active" | "archived"); `review` is
 * this page's own standing, computed by `planReviewStandings` and by nothing
 * else, so the filter and the KPI cannot disagree.
 */
const STATE_PARAM = "state";
const REVIEW_PARAM = "review";
const OVERDUE_VIEW = "overdue";

/** "RTO 30 min · RPO 15 min", with an unset objective drawn as absent. */
function objectives(plan: PlanOut): string {
  const rto =
    plan.rto_minutes != null
      ? translate("bcp.plans.rto", { minutes: plan.rto_minutes })
      : translate("bcp.plans.rtoNone");
  const rpo =
    plan.rpo_minutes != null
      ? translate("bcp.plans.rpo", { minutes: plan.rpo_minutes })
      : translate("bcp.plans.rpoNone");
  return `${rto} · ${rpo}`;
}

/**
 * v3's "Last test" cell: the outcome as its pill, "Untested" as the warning.
 * The status row is where the last outcome lives; until it has answered the
 * cell says so rather than guessing. The design dates the pill ("Tested 12
 * Aug"); `PlanStatusOut` carries no test date, so the pill is undated rather
 * than stamped with the review date.
 */
function LastTest({ status }: { status: PlanStatusOut | undefined }) {
  const t = useT();
  if (!status) return <span className="cell-sub">{t("bcp.plans.na")}</span>;
  if (status.last_test_outcome == null) {
    return <StatusPill value="untested" kind="warn" label={t("bcp.plans.untested")} />;
  }
  return (
    <StatusPill
      value={status.last_test_outcome}
      kind={outcomeKind(status.last_test_outcome)}
      label={outcomeLabel(status.last_test_outcome)}
    />
  );
}

export function PlansPage() {
  const t = useT();
  const api = useApi();
  const [editing, setEditing] = useState<PlanOut | "new" | null>(null);

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const canManage = (me.data?.permissions ?? []).includes("bcp.manage");
  // Same `["me"]` query as the line above, read for the profile zone rather
  // than the permissions -- see `useUserTimezone` in ./api.
  const zone = useUserTimezone();

  const plans = usePlans();
  // `review_overdue` lives on PlanStatusOut (backend/app/modules/bcp/reporting.py
  // bcp_plan_status), not PlanOut — this joins the two by plan id so each row
  // can carry its review standing alongside the plan's own fields.
  //
  // Auditor B (HIGH): "a continuity plan that has never been reviewed, with a
  // one-day review interval, is classified as current." The join used to be
  // `overdueByPlan.get(plan.id) ? "review overdue" : "up to date"`, and
  // `Map.get` answers `undefined` for every plan while the status query is in
  // flight, and for every plan if it 403s -- so the fallback for "I do not
  // know" was a green badge. `planReviewStandings` is now the ONE expression
  // behind both the column and the KPI; they were two before, which is how a
  // plan could read "up to date" while the KPI counted it.
  const status = usePlanStatus();
  const items = plans.data ?? [];
  const standings = planReviewStandings(items, status.data);
  const statusById = useMemo(
    () => new Map((status.data ?? []).map((row) => [row.id, row])),
    [status.data],
  );

  const activeCount = items.filter((plan) => plan.state === "active").length;
  const overdueCount = items.filter((plan) => needsReview(standings.get(plan.id) ?? "unknown"))
    .length;
  // WHAT EACH FIGURE IS MADE OF, for the tile's second line. Every one of
  // these is a count over rows already in hand -- the plans list for the first
  // two, the status rows for the third -- so a tile that reads "—" because its
  // query has not answered carries no breakdown either (the `foot` props
  // below are passed only when the figure itself was measured).
  //
  // "Never reviewed" is the half of `Review overdue` a compliance reader acts
  // on first: an interval that has elapsed is a plan whose review is late, and
  // a plan with no review on record has never had one at all.
  const neverReviewed = items.filter((plan) => (standings.get(plan.id) ?? "unknown") === "never")
    .length;
  const intervalElapsed = items.filter((plan) => (standings.get(plan.id) ?? "unknown") === "overdue")
    .length;
  const plansWithFindings = (status.data ?? []).filter((row) => row.open_findings > 0).length;
  // "The plans list answered" and "there are no plans" are different facts, and
  // a tile must not print 0 for the first: an unmeasured 0 and a measured 0
  // look identical on screen and mean opposite things.
  const measured = plans.data !== undefined;

  const [params] = useSearchParams();
  const stateFilter = params.get(STATE_PARAM) ?? "";
  const reviewFilter = params.get(REVIEW_PARAM) ?? "";

  const visible = useMemo(
    () =>
      items.filter((plan) => {
        if (stateFilter !== "" && plan.state !== stateFilter) return false;
        // The KPI's own predicate, over the KPI's own map. Two expressions is
        // the defect this module already paid for once.
        if (reviewFilter === OVERDUE_VIEW && !needsReview(standings.get(plan.id) ?? "unknown"))
          return false;
        return true;
      }),
    [items, stateFilter, reviewFilter, standings],
  );

  const stateWords =
    stateFilter === "active"
      ? t("bcp.plans.wordsActive")
      : stateFilter === "archived"
        ? t("bcp.plans.wordsArchived")
        : t("bcp.plans.wordsOtherState", { state: planStateLabel(stateFilter).toLowerCase() });
  const filterWords = [
    stateFilter !== "" ? stateWords : null,
    reviewFilter === OVERDUE_VIEW ? t("bcp.plans.wordsOverdue") : null,
  ].filter((part): part is string => part !== null);
  // The design's "Passed" stat card needs an exercise outcome, which lives on
  // TestOut, not PlanStatusOut — this list carries neither, so a pass/fail
  // count for the fleet of plans is omitted rather than approximated.
  const openFindings = (status.data ?? []).reduce((total, row) => total + row.open_findings, 0);

  return (
    <div>
      <PageHead
        title={t("bcp.plans.title")}
        description={t("bcp.plans.description")}
        actions={
          canManage ? (
            <Button onClick={() => setEditing("new")}>
              <Icon name="plus" size={14} />
              {t("bcp.plans.newPlan")}
            </Button>
          ) : undefined
        }
      />

      <BcpReadiness />

      {/* Only "Review overdue" is `urgent`: a continuity plan nobody has
          reviewed is the count a compliance reader acts on today, and the
          findings are worked through a test report rather than from here. */}
      <StatsGrid columns={3}>
        <StatCard
          label={t("bcp.plans.tileActive")}
          value={measured ? String(activeCount) : "—"}
          icon="shield-check"
          to={`/bcp?${STATE_PARAM}=active`}
          tone="blue"
          zeroLabel={t("bcp.plans.noneActive")}
          foot={measured && activeCount > 0 ? t("bcp.plans.ofOnRecord", { count: items.length }) : undefined}
        />
        <StatCard
          label={t("bcp.plans.tileOverdue")}
          value={measured ? String(overdueCount) : "—"}
          icon="clock"
          to={`/bcp?${REVIEW_PARAM}=${OVERDUE_VIEW}`}
          tone="gold"
          urgent
          /* WHICH zero this is, and the two are not the same claim: with the
             status response in hand a 0 means every review is current, and
             without it a 0 means only that no plan carries a `never reviewed`
             date — the interval half was never checked. */
          zeroLabel={
            status.data ? t("bcp.plans.allCurrent") : t("bcp.plans.notChecked")
          }
          foot={
            measured && overdueCount > 0
              ? `${t("bcp.plans.neverReviewed", { count: neverReviewed })}, ${t("bcp.plans.pastInterval", { count: intervalElapsed })}`
              : undefined
          }
        />
        <StatCard
          label={t("bcp.plans.tileFindings")}
          // The count is a sum over the STATUS rows, so it is "—" until they
          // arrive: `(status.data ?? []).reduce(...)` reads 0 for a response
          // that never came, which is the reassuring version of "unknown".
          value={status.data ? String(openFindings) : "—"}
          icon="alert-triangle"
          /* The period report is the only surface that lists findings across
             plans (`GET /bcp/report/period` — "Open findings by severity");
             a plan row does not carry them. */
          to="/bcp/report"
          tone="orange"
          zeroLabel={t("bcp.plans.noneOpen")}
          foot={
            status.data && openFindings > 0
              ? t("bcp.plans.acrossPlans", { count: plansWithFindings })
              : undefined
          }
        />
      </StatsGrid>

      {filterWords.length > 0 ? (
        <p className="note mb-3">
          {t("bcp.plans.showing", { shown: visible.length, total: items.length, words: filterWords.join(", ") })}{" "}
          <Link to="/bcp">{t("bcp.plans.showAll")}</Link>
        </p>
      ) : null}

      <Panel bare>
        <QueryState
          loading={plans.isLoading}
          error={plans.error}
          empty={visible.length === 0}
          emptyLabel={
            items.length === 0
              ? t("bcp.plans.emptyNone")
              : t("bcp.plans.emptyFiltered", { words: filterWords.join(", ") })
          }
          emptyIcon="shield-check"
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("bcp.plans.colName")}</Th>
                <Th>{t("bcp.plans.colState")}</Th>
                <Th>{t("bcp.plans.colReview")}</Th>
                <Th>{t("bcp.plans.colLastTest")}</Th>
                <Th>{t("bcp.plans.colFindings")}</Th>
                <Th>{t("bcp.plans.colCreated")}</Th>
                <Th>{""}</Th>
              </tr>
            </thead>
            <tbody>
              {visible.map((plan) => {
                const standing = standings.get(plan.id) ?? "unknown";
                const row = statusById.get(plan.id);
                return (
                  <tr key={plan.id}>
                    {/* v3's row: the shield as the lead, the plan over its
                        objectives, and the warning gutter on a plan whose
                        review is late or has never happened. */}
                    <Td
                      lead={<Icon name="shield-check" size={18} />}
                      gutter={needsReview(standing) ? "warn" : undefined}
                    >
                      {/* A FLOOR UNDER THE NAME COLUMN, and it is what makes
                          this list readable on a handset. Five nowrap cells
                          (two pills, a count, a date, a button) take their
                          intrinsic width first, so at 390px the name was left
                          about 70px and broke one word per line -- "Porto /
                          office / WAN / failover" over four, its objectives
                          over three. The wrapper is a scroll container
                          (`Table`), so a floor here scrolls the table rather
                          than the page. Same measure as `health/ProfileList`. */}
                      <div className="cell-title" style={{ minWidth: "18ch" }}>
                        <Link to={`/bcp/plans/${plan.id}`}>{plan.name}</Link>
                      </div>
                      <div className="cell-sub" style={{ whiteSpace: "nowrap" }}>
                        {objectives(plan)}
                      </div>
                    </Td>
                    <Td>
                      {/* Wire vocabulary, read as words (cycle 12). */}
                      <StatusPill
                        value={plan.state}
                        kind={plan.state === "active" ? "ok" : "mut"}
                        label={planStateLabel(plan.state)}
                      />
                    </Td>
                    <Td>
                      <StatusPill
                        value={standing}
                        kind={reviewStandingKind(standing)}
                        label={reviewStandingLabel(standing)}
                      />
                    </Td>
                    <Td>
                      <LastTest status={row} />
                    </Td>
                    <Td>
                      <span className="mono">{row ? row.open_findings : t("bcp.plans.na")}</span>
                    </Td>
                    <Td>
                      <span className="mono" style={{ whiteSpace: "nowrap" }}>
                        {formatDateTime(plan.created_at, zone)}
                      </span>
                    </Td>
                    <Td>
                      {/* A CHECKLIST, NOT A PLAY BUTTON. This control opens
                          the plan, where its tests are listed and where a new
                          one is scheduled; nothing here runs a drill. The play
                          glyph promised an execution the link does not
                          perform -- v3's row action in this position IS "Run
                          test", which is what made the wrong glyph plausible.
                          The label already says where it goes. */}
                      <Link className="btn small" to={`/bcp/plans/${plan.id}`}>
                        <Icon name="list-check" size={14} />
                        {t("bcp.plans.tests")}
                      </Link>
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        </QueryState>
      </Panel>

      <PlanEditor plan={editing} onClose={() => setEditing(null)} />
    </div>
  );
}
