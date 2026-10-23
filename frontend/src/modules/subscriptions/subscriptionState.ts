import { translate } from "../../i18n/core";
import { lazyLabels } from "../../i18n/lazy";
import { humaniseEnum } from "../../lib/labels";
import type { BadgeVariant } from "../jobs/jobState";
import type { Kind } from "../../shell/viz";

// Subscription.status vocabulary exactly as written by the backend
// (backend/app/modules/subscriptions/schema.py's `SubStatus` Literal is
// `Literal["active", "trial", "cancelled"]`, the only values
// SubscriptionCreate/SubscriptionUpdate ever write to Subscription.status).
// No other status value is ever persisted -- a made-up status name here
// would repeat the Phase-5 licenses-module regression. "active" is the
// healthy/current state (success); "trial" is a distinct, lower-commitment
// state worth calling out (info); "cancelled" is over/inactive (muted, not
// destructive -- cancelling is a normal lifecycle outcome, not a failure).
const STATUS_VARIANTS: Record<string, BadgeVariant> = {
  active: "success",
  trial: "info",
  cancelled: "muted",
};

export function statusVariant(status: string): BadgeVariant {
  return STATUS_VARIANTS[status] ?? "muted";
}

// Subscription.billing_cycle vocabulary exactly as written by the backend
// (backend/app/modules/subscriptions/schema.py's `BillingCycle` Literal is
// `Literal["monthly", "quarterly", "annual"]`). There is no good/bad
// semantic here -- all three are legitimate cadences -- so "annual" gets the
// distinct "info" look (it is the cadence most likely to carry a discount
// worth surfacing) and the shorter cadences share the neutral "muted" look,
// mirroring licenseState.ts's licenseTypeVariant's one-distinct-rest-neutral
// pattern.
const BILLING_CYCLE_VARIANTS: Record<string, BadgeVariant> = {
  monthly: "muted",
  quarterly: "muted",
  annual: "info",
};

export function billingCycleVariant(cycle: string): BadgeVariant {
  return BILLING_CYCLE_VARIANTS[cycle] ?? "muted";
}

// RenewalRow.action_required is a plain boolean computed by
// backend/app/modules/subscriptions/reporting.py's renewal_calendar (true
// only when the subscription both auto-renews and today is on/after its
// notice deadline) -- not a vocabulary string, so there is nothing to invent
// or fall back on here. `true` means the org must act now to prevent an
// unwanted renewal (destructive); `false` needs no attention yet (muted).
export function actionVariant(actionRequired: boolean): BadgeVariant {
  return actionRequired ? "destructive" : "muted";
}

// THE v3 KINDS, beside the retired variants. `StatusPill` takes a `Kind`
// rather than a `BadgeVariant`; the judgement is the same one written in the
// new vocabulary. `active` is ok, `trial` is the one distinct lower-commitment
// state (info), `cancelled` is a normal lifecycle end (mut, not crit). The
// billing cadence is NOT in this vocabulary at all any more -- see
// `billingCyclePhrase` below. A closing notice deadline is a failure in the
// making (crit); a renewal on track needs nothing (mut). Nothing on a page
// calls the `BadgeVariant` functions above any more; only their tests do.
// The plan is that the closer removes them together with those test cases
// when `Badge` is retired from the kit, in one commit, rather than a page at
// a time here.

const STATUS_KINDS: Record<string, Kind> = { active: "ok", trial: "info", cancelled: "mut" };

export function statusKind(status: string): Kind {
  return STATUS_KINDS[status] ?? "mut";
}

/**
 * The cadence as a sentence, NOT AS A SEVERITY.
 *
 * `billingCycleKind` mapped annual to `info` and everything else to `mut`, so
 * every monthly and quarterly subscription drew the muted pill with the minus
 * glyph -- the vocabulary this console uses for "not applicable", "inactive",
 * "nothing measured" -- one column away from the row's real Status pill. A
 * billing cadence is a plain attribute of a live contract: none of the three
 * values is better or worse than the others, and none of them is a state
 * anybody must act on. Drawn as text, it can no longer be misread as a
 * severity, and the pills that remain on these pages all mean one thing.
 *
 * "Billed monthly", not "Monthly", because the word alone in a page head sits
 * beside a status and reads as one. The table column has its own header, so
 * it takes the bare cadence.
 */
const BILLING_CYCLE_PHRASE: Readonly<Record<string, string>> = lazyLabels({
  monthly: "subscriptions.state.billedMonthly",
  quarterly: "subscriptions.state.billedQuarterly",
  annual: "subscriptions.state.billedAnnually",
});

/** `"monthly"` -> `"Monthly"`. An unknown cadence is humanised, never dropped. */
export function billingCycleLabel(cycle: string): string {
  return humaniseEnum(cycle, "BillingCycle");
}

/** `"monthly"` -> `"Billed monthly"`. */
export function billingCyclePhrase(cycle: string): string {
  return (
    BILLING_CYCLE_PHRASE[cycle] ??
    translate("subscriptions.state.billedOther", { cycle: humaniseEnum(cycle, "BillingCycle").toLowerCase() })
  );
}

export function actionKind(actionRequired: boolean): Kind {
  return actionRequired ? "crit" : "mut";
}
