/**
 * Whether a continuity plan's review is current — and refusing to say "yes"
 * when nothing is known.
 *
 * WHY THIS FILE (UX remediation cycle 7). Auditor B, [HIGH], /bcp: "The plan I
 * created has `Last reviewed: never · Review interval: 1 days`. The list's
 * REVIEW column nonetheless reads 'up to date', and the 'Review overdue' KPI
 * reads 0. So a continuity plan that has never been reviewed, with a one-day
 * review interval, is classified as current... 'Review overdue: 0' is the
 * metric a compliance officer trusts, and it is wrong in the one case that
 * matters most."
 *
 * WHERE THE DEFECT ACTUALLY IS, because this matters for the fix. The BACKEND
 * is right: `bcp/reporting.py#_is_review_overdue` opens with "A plan that has
 * never been reviewed (`last_reviewed_at is None`) is always overdue" and
 * returns True. The frontend threw that answer away. `PlansPage` built a
 * `Map` from the SEPARATE `/bcp/plans/status` response and rendered
 * `overdueByPlan.get(plan.id) ? "review overdue" : "up to date"` — and
 * `Map.get` returns `undefined` for a plan the status response does not carry,
 * which is every plan while that second query is still in flight, every plan
 * if it 403s or 503s, and every plan if the two responses ever disagree about
 * which plans exist. `undefined` is falsy, so the fallback for "I do not know"
 * was a green badge reading "up to date".
 *
 * THREE STATES, NOT TWO. "Never reviewed", "overdue" and "current" are
 * different facts about a plan and a compliance reader acts differently on
 * each; and "not known yet" is a fourth that must never borrow the appearance
 * of the third. A pure function so the KPI and the column cannot drift apart —
 * they were computed by two different expressions before.
 */

import { translate, type MessageKey } from "../../i18n/core";
import type { BadgeVariant } from "../jobs/jobState";
import { variantKind } from "../jobs/jobState";
import type { Kind } from "../../shell/viz";
import type { PlanOut, PlanStatusOut } from "./types";

export type ReviewStanding = "never" | "overdue" | "current" | "unknown";

/**
 * `overdue` is the backend's own `PlanStatusOut.review_overdue`, or `undefined`
 * when no status row for this plan has arrived. `undefined` means UNKNOWN and
 * is never treated as "fine".
 */
export function reviewStanding(
  lastReviewedAt: string | null,
  overdue: boolean | undefined,
): ReviewStanding {
  // Read off the plan itself, so it holds even when the status query has not
  // answered: a plan with no review on record is never "up to date", and it is
  // a distinct fact from "the interval has elapsed".
  if (lastReviewedAt == null) return "never";
  if (overdue == null) return "unknown";
  return overdue ? "overdue" : "current";
}

/**
 * SENTENCE CASE, like every other pill in the row. The state and last-test
 * columns render humanised wire values ("Active", "Archived", "Pass",
 * "Untested"), and this column passed explicit lowercase labels -- three
 * adjacent columns in two capitalisations. `humaniseEnum` is not what
 * produces these: the standing is this module's own vocabulary, not the
 * server's, and its wording ("never reviewed" rather than "never") is the
 * point of the table.
 */
const LABELS: Record<ReviewStanding, MessageKey> = {
  never: "bcp.standing.never",
  overdue: "bcp.standing.overdue",
  current: "bcp.standing.current",
  unknown: "bcp.standing.unknown",
};

const VARIANTS: Record<ReviewStanding, BadgeVariant> = {
  never: "destructive",
  overdue: "destructive",
  current: "success",
  // NOT "success" and not "destructive": inventing either would be asserting
  // something about a plan whose status has not been read.
  unknown: "muted",
};

export function reviewStandingLabel(standing: ReviewStanding): string {
  return translate(LABELS[standing]);
}

export function reviewStandingVariant(standing: ReviewStanding): BadgeVariant {
  return VARIANTS[standing];
}

/** The standing as a v3 kind, for the pill and the row gutter. */
export function reviewStandingKind(standing: ReviewStanding): Kind {
  return variantKind(VARIANTS[standing]);
}

/** Counted by the "Review overdue" KPI. Never-reviewed counts; unknown does not. */
export function needsReview(standing: ReviewStanding): boolean {
  return standing === "never" || standing === "overdue";
}

/**
 * The standing of every plan, joined to the status response.
 *
 * ONE function for the column and the KPI. They were two expressions over two
 * different collections before — the KPI counted the STATUS rows and the column
 * read the PLAN rows — which is how a plan present in one and absent from the
 * other produced a green badge and a zero at the same time.
 */
export function planReviewStandings(
  plans: readonly PlanOut[],
  status: readonly PlanStatusOut[] | undefined,
): Map<string, ReviewStanding> {
  const overdueById = new Map((status ?? []).map((row) => [row.id, row.review_overdue]));
  return new Map(
    plans.map((plan) => [
      plan.id,
      reviewStanding(plan.last_reviewed_at, status ? overdueById.get(plan.id) : undefined),
    ]),
  );
}
