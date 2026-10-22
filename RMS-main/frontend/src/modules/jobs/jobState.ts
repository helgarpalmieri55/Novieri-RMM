import type { JobStatus } from "../../lib/api";
import type { Kind } from "../../shell/viz";
import { NO_DATE, intlLocale } from "../../lib/datetime";
import { translate } from "../../i18n/core";

export type BadgeVariant = "muted" | "info" | "success" | "destructive" | "warning";

export const JOB_STATUSES: readonly JobStatus[] = [
  "queued",
  "dispatched",
  "running",
  "succeeded",
  "failed",
  "expired",
  "refused",
  "cancelled",
] as const;

const VARIANTS: Record<JobStatus, BadgeVariant> = {
  queued: "muted",
  dispatched: "info",
  running: "info",
  succeeded: "success",
  failed: "destructive",
  expired: "warning",
  refused: "destructive",
  /**
   * MUTED, LIKE `queued`, AND NOT `destructive`.
   *
   * A cancelled job is not a failure and needs nobody's attention: somebody
   * deliberately stopped it and the platform did what they asked. Red would
   * put it in the same visual class as `failed` and `refused` -- the two
   * states an operator is meant to chase -- and a console that shouts about
   * work it carried out correctly teaches people to ignore the colour.
   */
  cancelled: "muted",
};

export function statusVariant(status: JobStatus): BadgeVariant {
  return VARIANTS[status];
}

/**
 * HOW LONG A JOB HAS BEEN WAITING, and when that becomes a problem.
 *
 * WHY THIS EXISTS (UX remediation cycle 9). Both auditors found the same
 * thing from two directions. Auditor A, on the dashboard: "[HIGH] Jobs queued
 * for over 24 hours are reported as healthy — 'Jobs needing attention: 0'".
 * Auditor B, on /jobs: "[HIGH] Four of five jobs have sat `queued` with 0
 * attempts for over 24 hours and nothing flags it … the KPI tiles read Jobs 5,
 * Running 0, Failed 0, i.e. the dashboard presents an entirely healthy
 * picture."
 *
 * A QUEUE THAT HAS DISPATCHED NOTHING IN A DAY IS AN INCIDENT, and "Failed 0"
 * reads as success while it happens. The threshold below is deliberately
 * generous — an agent's check-in interval, not a second — because the cost of
 * flagging a job that was about to run is a glance, and the cost of not
 * flagging one is the state the audit found.
 */
export const STALE_WAIT_MS = 15 * 60 * 1000;

/** Statuses where the platform is still waiting on the endpoint. */
const WAITING: readonly JobStatus[] = ["queued", "dispatched"];

export function isWaiting(status: JobStatus): boolean {
  return WAITING.includes(status);
}

/**
 * A job the operator should look at: still waiting, and waiting too long.
 *
 * Time is passed in rather than read from the clock so that this is testable
 * without freezing the world, and so a list renders every row against ONE
 * instant — rows computed against `Date.now()` each can disagree about whether
 * two jobs issued in the same second are both stale.
 */
export function isStuck(job: { status: JobStatus; issued_at: string }, now: number): boolean {
  if (!isWaiting(job.status)) return false;
  const issued = Date.parse(job.issued_at);
  if (Number.isNaN(issued)) return false;
  return now - issued >= STALE_WAIT_MS;
}

/**
 * "25h ago" — the age of an instant, in the coarsest unit that is still true.
 *
 * NOT A SECOND TIMESTAMP. The audit's complaint about `8/18/2026, 11:04:18 AM`
 * was never that it is wrong; it is that answering "is this stuck?" from it
 * requires the reader to do date arithmetic. This sits beside the absolute
 * time, it does not replace it — an evidence console has to keep the instant.
 */
export function describeAge(iso: string | null | undefined, now: number): string {
  if (!iso) return NO_DATE;
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return NO_DATE;
  const seconds = Math.round((now - then) / 1000);
  // A clock skew between server and browser must not read as the future.
  if (seconds < 45) return translate("time.justNow");
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return translate("jobs.age.minutes", { count: minutes });
  const hours = Math.round(minutes / 60);
  if (hours < 48) return translate("jobs.age.hours", { count: hours });
  return translate("jobs.age.days", { count: Math.round(hours / 24) });
}

/**
 * "12m", "3h", "2d" — a DURATION, in the coarsest unit that is still true.
 *
 * Not `describeAge`: that one answers "how long ago", ends in "ago" and is
 * read beside a timestamp. This one answers "for how long", and is read on a
 * tile beside a count ("Longest run 12m"). A span the clock cannot support --
 * negative, because the server's clock is ahead of the browser's -- is "under
 * a minute" rather than a negative number.
 */
export function describeSpan(ms: number): string {
  const minutes = Math.round(ms / 60_000);
  if (minutes < 1) return translate("jobs.span.underMinute");
  if (minutes < 60) return translate("jobs.span.minutes", { count: minutes });
  const hours = Math.round(minutes / 60);
  if (hours < 48) return translate("jobs.span.hours", { count: hours });
  return translate("jobs.span.days", { count: Math.round(hours / 24) });
}

/**
 * "2 patch.apply · 1 inventory.collect" — what a count on a tile is MADE OF.
 *
 * v3 draws a second line under every KPI figure, and on the failure tile that
 * line is the breakdown by job type: a bare "18" says something failed, and
 * "18 patch.apply · 3 inventory.collect" says whether one capability is
 * broken or the whole fleet is. Ordered by count, then by name so equal
 * counts do not shuffle between renders; anything past `limit` collapses into
 * "+N more" rather than being dropped silently.
 */
export function typeBreakdown(items: readonly { type: string }[], limit = 2): string | null {
  if (items.length === 0) return null;
  const counts = new Map<string, number>();
  for (const item of items) counts.set(item.type, (counts.get(item.type) ?? 0) + 1);
  const ordered = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], intlLocale()));
  const shown = ordered.slice(0, limit).map(([type, count]) => `${count} ${type}`);
  const rest = ordered.length - shown.length;
  if (rest > 0) shown.push(translate("jobs.tiles.breakdownMore", { count: rest }));
  return shown.join(" · ");
}

/**
 * The parts of a job this module needs to reason about cancellation.
 *
 * A STRUCTURAL TYPE RATHER THAN `Job`, so these functions can be tested
 * against the four fields they actually read instead of a fixture carrying
 * fifteen. Same reason `isStuck` takes `{status, issued_at}`.
 */
export interface JobLifecycle {
  status: JobStatus;
  expires_at: string;
  cancel_requested_at?: string | null;
  cancel_outcome?: string | null;
  retry_of_job_id?: string | null;
}

/**
 * Can this job be stopped at all, and what would stopping it MEAN?
 *
 * TWO MECHANISMS BEHIND ONE BUTTON, AND THE CONSOLE MUST NOT CONFLATE THEM
 * (2026-08-22-job-lifecycle-design.md §3.2). Withdrawing a `queued` job is
 * something the server does on its own authority -- the signed envelope was
 * never written to a socket, so nothing on any endpoint can contradict the
 * word "cancelled". Interrupting a job that has REACHED a device is a request:
 * the platform signs a `job.cancel` job, sends it, and waits, because it has
 * no authority over a machine that already has the work in hand.
 *
 * `null` means the job cannot be stopped -- see `whyNotStoppable` for the
 * sentence that goes in its place.
 */
export type StopKind = "withdraw" | "interrupt";

export function stopKind(job: JobLifecycle): StopKind | null {
  if (job.cancel_requested_at) return null;
  if (job.status === "queued") return "withdraw";
  if (job.status === "dispatched" || job.status === "running") return "interrupt";
  return null;
}

/**
 * Can this job be taken back without asking anybody?
 *
 * ONLY FROM `queued`, and that is the design rather than a placeholder.
 * `queued` means the signed envelope was never written to a socket, which is
 * the only circumstance in which the platform can say a job was stopped
 * without asking the endpoint. This mirrors the server's own rule; it does not
 * replace it. Authorization is not consulted here at all -- the permission
 * depends on the job's TYPE and is resolved server-side, and a console that
 * guessed would either hide a control the operator is entitled to or offer one
 * they are not.
 *
 * NEITHER IS INTERRUPTIBILITY CONSULTED HERE, for the identical reason. Which
 * job types may be interrupted once they have reached a device (§9.1) is a
 * server-side fact about what each AGENT capability honours; the console holds
 * no copy of that map, and inventing one would put a stale list in front of an
 * operator. So the control is offered and a refusal is REPORTED.
 */
export function canWithdraw(job: JobLifecycle): boolean {
  return stopKind(job) === "withdraw";
}

/**
 * What the confirmation dialog promises, per mechanism.
 *
 * The two sentences differ in the one way that matters: whether the device is
 * contacted. Somebody withdrawing a queued job is told nothing reaches the
 * machine; somebody interrupting a running one is told a signed instruction is
 * sent and that the endpoint decides what happens next. A single dialog
 * covering both would have to be vague about exactly the fact an operator
 * needs.
 */
export function describeStop(kind: StopKind): { title: string; body: string; confirm: string } {
  if (kind === "withdraw") {
    return {
      title: translate("jobs.stop.withdraw.title"),
      body: translate("jobs.stop.withdraw.body"),
      confirm: translate("jobs.stop.withdraw.confirm"),
    };
  }
  return {
    title: translate("jobs.stop.interrupt.title"),
    body: translate("jobs.stop.interrupt.body"),
    confirm: translate("jobs.stop.interrupt.confirm"),
  };
}

/**
 * The one-line explanation under the stop button.
 */
export function describeStopEffect(kind: StopKind, expiry: string): string {
  if (kind === "withdraw") return translate("jobs.stop.withdraw.effect", { expiry });
  return translate("jobs.stop.interrupt.effect", { expiry });
}

/**
 * A cancel that has been asked for and not yet answered.
 *
 * **THIS IS THE SENTENCE THE WHOLE DESIGN EXISTS TO MAKE POSSIBLE** (§3.6). A
 * job can be `running` AND carry a cancel request, and those are two true facts
 * that must be shown together. The status badge keeps saying `running`, because
 * it is; this line says a human asked and nobody has confirmed. Collapsing them
 * into a "Cancelling" badge would delete a fact the operator can act on in
 * favour of one the platform is only hoping for.
 *
 * `null` for a job with no outstanding request, and for one that has already
 * reached a terminal state -- `describeCancellation` and `describeLateCancel`
 * own those.
 */
export function describeCancelRequest(job: JobLifecycle, asked: string): string | null {
  if (!job.cancel_requested_at) return null;
  if (job.status !== "dispatched" && job.status !== "running") return null;
  if (job.cancel_outcome === "unsupported") {
    return translate("jobs.stop.requestUnsupported", { asked });
  }
  return translate("jobs.stop.requested", { asked });
}

/**
 * A job that finished on its own after somebody asked for it to stop.
 *
 * The case the design exists to tell the truth about: the cancel arrived too
 * late. Saying "Cancelled" here would be a lie about a machine that did the
 * work, and saying nothing would leave an operator who pressed the button
 * wondering whether it did anything.
 */
export function describeLateCancel(job: JobLifecycle): string | null {
  if (!job.cancel_requested_at) return null;
  if (job.status === "succeeded") {
    return translate("jobs.stop.lateSucceeded");
  }
  if (job.status === "expired") {
    return translate("jobs.stop.lateExpired");
  }
  return null;
}

/**
 * What the console may say about a cancelled job, or `null` for one nobody
 * cancelled.
 *
 * THE WORD "CANCELLED" APPEARS ONLY WHERE THERE IS EVIDENCE, and the sentence
 * beside it is the evidence. `withdrawn` is a claim the server can back up on
 * its own authority: the job never left it. `dropped` and `interrupted` are
 * things an ENDPOINT reported about itself, and the two are not the same fact
 * -- one never started, the other was stopped mid-flight, and only the second
 * can have left work half-done.
 *
 * AN UNRECOGNISED OUTCOME GETS THE BARE FACT AND NO STORY. Rendering an
 * unknown value as "withdrawn" would assert the job never reached the device
 * when the endpoint may well have run it.
 */
export function describeCancellation(job: JobLifecycle): string | null {
  if (job.status !== "cancelled") return null;
  switch (job.cancel_outcome) {
    case "withdrawn":
      return translate("jobs.stop.withdrawn");
    case "dropped":
      return translate("jobs.stop.dropped");
    case "interrupted":
      return translate("jobs.stop.interrupted");
    default:
      return translate("jobs.stop.cancelled");
  }
}

/**
 * Why this job cannot be stopped, or `null` when it can.
 *
 * These mirror the server's own 409 messages so the operator reads the same
 * reason whether the console declines to offer the button or the API refuses
 * the call. A control that is simply absent, with no sentence, is the state
 * the audit filed as a BLOCKER on this page.
 *
 * `expiry` IS PASSED IN ALREADY FORMATTED, not read off the job here: every
 * instant on this page is rendered in the operator's own profile zone
 * (`formatDateTime`), and a second one produced in the browser's zone would
 * reintroduce exactly the defect cycle 5 closed on the `Issued` column.
 */
export function whyNotStoppable(job: JobLifecycle, expiry: string): string | null {
  if (stopKind(job) !== null) return null;
  if (job.cancel_requested_at) {
    return null;
  }
  return translate("jobs.stop.finished", { expiry });
}

/**
 * Can this job be re-run from the console?
 *
 * FOUR TERMINAL STATES AND NOT `succeeded`
 * (2026-08-22-job-lifecycle-design.md §4.2). `expired` is the commonest by a
 * long way -- the device was off and the job was never confirmed to have run.
 * `failed` and `refused` are the operator's call. `cancelled` is a fresh
 * deliberate act, and the dialog names the earlier cancellation so nobody
 * re-runs a job somebody else stopped on purpose.
 *
 * `succeeded` IS ABSENT ON PURPOSE: re-running work that worked is a new
 * dispatch and belongs on the route that owns the record of what was asked for.
 * The unfinished states are absent because there is nothing to re-run yet --
 * the platform's own stuck-dispatch sweep already re-sends a job that never
 * reached its device.
 *
 * NEITHER AUTHORIZATION NOR RETRYABILITY-BY-TYPE IS DECIDED HERE, for the same
 * reason `stopKind` decides neither. A retry is gated on the job TYPE's own
 * dispatch permission, and whether the kernel route may re-run a type at all
 * depends on whether its module has declared how to repoint its record -- both
 * server-side facts. The console offers the control and reports the refusal.
 */
const RETRYABLE: readonly JobStatus[] = ["failed", "expired", "refused", "cancelled"];

export function canRetry(job: JobLifecycle): boolean {
  return RETRYABLE.includes(job.status);
}

/**
 * What the retry confirmation says, per source state.
 *
 * A RETRY IS A NEW JOB AND THE DIALOG SAYS SO, because the operator's mental
 * model matters here: the row they are looking at does not change, a second
 * one appears, and the endpoint is sent a freshly signed instruction. The
 * `cancelled` case names the earlier cancellation, since re-running something
 * a colleague deliberately stopped is the one press somebody would want back.
 */
export function describeRetry(job: JobLifecycle): { title: string; body: string } {
  return {
    title: translate("jobs.retry.title"),
    body: translate(job.status === "cancelled" ? "jobs.retry.bodyCancelled" : "jobs.retry.body"),
  };
}

/**
 * Why this job cannot be re-run, or `null` when it can.
 *
 * Mirrors the server's own 409s, so the operator reads the same reason whether
 * the console declines to offer the button or the API refuses the call.
 */
export function whyNotRetryable(job: JobLifecycle): string | null {
  if (canRetry(job)) return null;
  if (job.status === "succeeded") {
    return translate("jobs.retry.succeeded");
  }
  return translate("jobs.retry.notFinished");
}

/**
 * The console's badge vocabulary as v3's six kinds, for `StatusPill`.
 *
 * ONE TABLE, HERE, because this file owns `BadgeVariant` and the security and
 * remote modules already import their variant helpers' type from it. Each
 * module keeps its own `Record<state, BadgeVariant>` (the wire word that
 * needs a person is that module's knowledge) and the last step to a kind is
 * shared, so the same variant is the same glyph on every page.
 */
export const VARIANT_KIND: Record<BadgeVariant, Kind> = {
  muted: "mut",
  info: "info",
  success: "ok",
  destructive: "crit",
  warning: "warn",
};

export function variantKind(variant: BadgeVariant): Kind {
  return VARIANT_KIND[variant];
}

/**
 * A job status as the kind v3's jobs page draws it.
 *
 * WORK IN FLIGHT IS `run`, NOT `info`. `BadgeVariant` has no word for "still
 * happening" (its `info` covered running and dispatched alike), but v3's six
 * kinds do: `run` is the progress glyph and `--sev-run` blue, and the jobs
 * screen draws it on the Running pill, the row gutter and the timeline bar.
 * The variant table above stays as the badge vocabulary; this is the one
 * step past it that the design draws differently.
 */
export function jobStatusKind(status: JobStatus): Kind {
  if (status === "running" || status === "dispatched") return "run";
  return variantKind(statusVariant(status));
}
