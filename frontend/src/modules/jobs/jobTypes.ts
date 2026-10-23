/**
 * What an operator may dispatch from the console, and what every job type is
 * CALLED.
 *
 * WHY THIS EXISTS (UX remediation cycle 9). Auditor A on /devices → Run job:
 *
 *   "[HIGH] Production job-type dropdown exposes a developer test job called
 *    `hello`, pre-filled with `ping`. (a) `hello` is unmistakably a
 *    smoke-test/scaffolding job type left in the shipped UI — and it is the
 *    *default*, so the path of least resistance dispatches a meaningless job.
 *    (b) The two options are raw internal identifiers, not human language — an
 *    operator cannot tell what `inventory.collect` does, how long it takes, or
 *    whether it is disruptive."
 *
 * Auditor B found the other end of the same defect: "[LOW] A junk job type
 * named `hello` sits in the production queue", with the UI giving no way to
 * tell a real job type from a test one.
 *
 * SO `hello` IS NOT IN `DISPATCHABLE_JOB_TYPES` AND IS IN `LABELS`. Those are
 * two different questions. Nobody should be able to send one from the console;
 * everybody must be able to read the ones already in the queue, and a job row
 * that renders as nothing at all is worse than one that says "Developer test
 * job". The backend still accepts `hello` on `POST /api/v1/jobs` for scripted
 * and development use — removing it there is a wire-protocol decision about
 * deployed agents, not a console one.
 *
 * THE DISPATCHABLE SET IS THE BACKEND'S, NOT A GUESS.
 * `backend/app/jobs/direct_dispatch.py#DIRECTLY_DISPATCHABLE_JOB_TYPES` is
 * `{hello, inventory.collect, logs.ship}` — every other registered type is
 * refused by the kernel route with a 403 naming the module route that owns it.
 * Offering `patch.scan` here (which the audit asked for, reasonably, having
 * seen it in the job feed) would build a control that 403s: patching is
 * dispatched from the patch module, where the authorization, the run record
 * and the TTL clamp live. Widening this list means widening that allowlist
 * first, in a reviewed backend change.
 */

import { translate, type MessageKey } from "../../i18n/core";

/** A job type an operator may create from the console. */
export interface DispatchableJobType {
  /** The wire type. Sent verbatim; the label never reaches the API. */
  readonly type: string;
  /** What the operator calls it. Read when drawn, so it follows the language. */
  readonly label: string;
  /** What it does, how long it takes, and whether it disturbs the endpoint. */
  readonly description: string;
}

/**
 * `label` and `description` are getters, not stored strings: a stored string
 * would be the OUTPUT of a lookup frozen in whichever language was active when
 * this module loaded, and would stay there after a switch.
 */
function dispatchable(
  type: string,
  description: "jobs.dispatchable.inventory_collect" | "jobs.dispatchable.logs_ship",
): DispatchableJobType {
  return {
    type,
    get label() {
      return jobTypeLabel(type);
    },
    get description() {
      return translate(description);
    },
  };
}

export const DISPATCHABLE_JOB_TYPES: readonly DispatchableJobType[] = [
  dispatchable("inventory.collect", "jobs.dispatchable.inventory_collect"),
  dispatchable("logs.ship", "jobs.dispatchable.logs_ship"),
];

/**
 * Every registered job type this console has a name for, including the ones
 * it cannot dispatch, because they still appear in the queue. The names are
 * in the catalogue (`enums.JobType`, keyed with `_` for `.`); this list is
 * what the tests hold to exist in both languages.
 *
 * Kept beside the dispatchable set on purpose: the two lists diverging is how
 * a job row ends up labelled with one thing and dispatched as another.
 */
export const KNOWN_JOB_TYPES: readonly string[] = [
  "inventory.collect",
  "logs.ship",
  // Not dispatchable here — each is governed by its own module's route.
  "patch.scan",
  "patch.apply",
  "patch.reboot",
  "app.deploy",
  "script.run",
  "backup.run",
  "backup.restore",
  "provision.enroll",
  "remote.session.open",
  "remote.screen.open",
  "agent.update",
  // Minted by Reject on a machine that joined through the install link (the
  // device review, never this console's dispatch form): the one job a
  // rejected machine may receive, so it shows in the queue.
  "agent.uninstall",
  "agent.push_install",
  "agent.push_probe",
  "net.sweep",
  "site.wake",
  "health.check.now",
  "health.policy.apply",
  "agent.cert.renew",
  "agent.cert.install",
  // Minted by the platform's OWN cancel route from a job it already signed --
  // never by a human, and never from this console. It is here for the same
  // reason `hello` is: an operator reading the queue will see these rows, and
  // a row rendering as a raw wire string is worse than one that says what it
  // was. Naming it "Stop a job" rather than "Cancel" is deliberate: the word
  // "Cancelled" belongs to the job that was stopped, not to the instruction.
  "job.cancel",
  "hello",
];

/**
 * The name for a job type, never blank.
 *
 * AN UNKNOWN TYPE FALLS BACK TO ITSELF rather than to "Unknown". A control
 * plane outlives the console in front of it: a job type registered by a module
 * this build has never heard of must still be readable as its wire name, which
 * is at least the string an operator can search the backend for.
 */
export function jobTypeLabel(type: string): string {
  // A `.` separates catalogue paths, so the wire type is keyed with `_`.
  const key = `enums.JobType.${type.replace(/\./g, "_")}` as MessageKey;
  const label = translate(key);
  return label !== key ? label : type;
}

/** True for a type this console will not offer — `hello` and anything retired. */
export function isDispatchable(type: string): boolean {
  return DISPATCHABLE_JOB_TYPES.some((entry) => entry.type === type);
}
