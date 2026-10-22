/**
 * The onboarding journey's state, as a pure function.
 *
 * WHAT THIS PAGE IS FOR, in one sentence from the operator: "from the inventory
 * tab itself, I need an option to scan subnets and add devices automatically,
 * then from there I can deploy the agents."
 *
 * NOTHING IN THAT SENTENCE IS MISSING FROM THE PRODUCT. Every step already
 * exists -- register a subnet (IPAM), authorize the scan scope (IPAM), sweep
 * (IPAM), review what answered (Fleet -> Discovered), get an agent onto them
 * (Platform -> Agent Packages). They are five features across three nav groups
 * with nothing linking them, which is very likely why the operator did not find
 * this as a feature: it is not a feature, it is five features that happen to
 * compose. `docs/modules/agent-distribution/spec-push-install.md` §10.
 *
 * SO THIS DEEP-LINKS AND DOES NOT DUPLICATE. Every control stays where it is,
 * under the permission it already has. In particular the scan-authorization
 * screen is admin-tier -- it is the grant that decides where this platform puts
 * packets on somebody's wire -- and reimplementing its controls behind a
 * friendlier gate would move a high-privilege act onto a page designed to feel
 * easy. What is added here is the thread: where you are, and what is stopping
 * the next step.
 *
 * WHY THE LOGIC IS A PURE FUNCTION AND NOT JSX. Five steps x four states x a
 * permission the caller may not hold is more branching than a component test
 * can cover by mounting, and the branches that matter are the ones nobody
 * reaches by accident -- `no_agent`, an unauthorized scope, an empty catalogue.
 * `__tests__/journey.test.ts` walks them; the page renders whatever comes back.
 */

import { translate as t } from "../../i18n/core";
import { effectiveSweepScope } from "../ipam/ipamState";
import type { ScopeOut, SubnetOut, SweepRunOut } from "../ipam/types";

/**
 * `unknown` IS NOT A FIFTH KIND OF PROGRESS. It means a query has not answered
 * yet, and it exists so no step renders a count nothing backs -- the Devices
 * page's rule, and the reason a zero here is never printed as "none found"
 * before the request has landed.
 */
export type StepState = "done" | "partial" | "blocked" | "unknown";

export type StepKey = "register" | "authorize" | "sweep" | "review" | "agent";

export interface JourneyStep {
  readonly key: StepKey;
  readonly title: string;
  readonly state: StepState;
  /** What is true right now. Always populated, including when nothing is. */
  readonly status: string;
  /** What to do next. "" when the step is satisfied and there is nothing to do. */
  readonly remedy: string;
  /** Where the doing happens. `null` when this step has no destination. */
  readonly to: string | null;
  readonly toLabel: string;
  /**
   * The permission the destination's ACTION needs, when the caller does not
   * hold it. Never used to hide the step -- a hidden prerequisite is
   * indistinguishable from a broken feature, which is the whole failure this
   * journey exists to fix. It changes the wording to "an administrator must do
   * this", and names the code so the operator can ask for it by name.
   */
  readonly missingPermission: string | null;
}

export interface JourneyInput {
  readonly permissions: readonly string[];
  /** `undefined` = the query has not answered. Never conflated with `[]`. */
  readonly subnets: readonly SubnetOut[] | undefined;
  readonly scopes: readonly ScopeOut[] | undefined;
  /** The subnet the operator is looking at, when they have chosen one. */
  readonly selected: SubnetOut | null;
  /** The selected subnet's most recent run. `undefined` = not loaded, `null` = never swept. */
  readonly latestRun: SweepRunOut | null | undefined;
  readonly candidates:
    | { readonly total: number; readonly unsweepable_subnets: number }
    | undefined;
  readonly releases: readonly { readonly has_artifact: boolean }[] | undefined;
  readonly now?: number;
}

function gate(permissions: readonly string[], code: string): string | null {
  return permissions.includes(code) ? null : code;
}

/** How many of these subnets an active scan authorization covers. */
export function authorizedSubnets(
  subnets: readonly SubnetOut[],
  scopes: readonly ScopeOut[],
  now: number,
): readonly SubnetOut[] {
  return subnets.filter((subnet) => effectiveSweepScope(subnet, scopes, now).authorized);
}

function registerStep(input: JourneyInput): JourneyStep {
  const base = {
    key: "register" as const,
    title: t("onboarding.journey.register.title"),
    to: "/ipam",
    toLabel: t("onboarding.journey.register.toLabel"),
    missingPermission: gate(input.permissions, "ipam.manage"),
  };
  if (input.subnets === undefined) {
    return { ...base, state: "unknown", status: t("onboarding.journey.register.loading"), remedy: "" };
  }
  if (input.subnets.length === 0) {
    return {
      ...base,
      state: "blocked",
      status: t("onboarding.journey.register.none"),
      remedy: t("onboarding.journey.register.noneRemedy"),
    };
  }
  return {
    ...base,
    state: "done",
    status: t("onboarding.journey.register.done", { count: input.subnets.length }),
    remedy: "",
  };
}

function authorizeStep(input: JourneyInput): JourneyStep {
  const base = {
    key: "authorize" as const,
    title: t("onboarding.journey.authorize.title"),
    to: "/ipam/scan-authorizations",
    toLabel: t("onboarding.journey.authorize.toLabel"),
    missingPermission: gate(input.permissions, "ipam.scope.manage"),
  };
  // THIS STEP IS WHY THE JOURNEY EXISTS. Authorization is deny-by-default and
  // documenting a subnet does not authorize probing it -- an operator who has
  // never read the IPAM spec finds the sweep button, gets a 403, and concludes
  // the feature is broken. Nothing else in the console tells them in advance.
  if (input.subnets === undefined || input.scopes === undefined) {
    return { ...base, state: "unknown", status: t("onboarding.journey.authorize.loading"), remedy: "" };
  }
  if (input.subnets.length === 0) {
    return {
      ...base,
      state: "blocked",
      status: t("onboarding.journey.authorize.nothingYet"),
      remedy: t("onboarding.journey.authorize.finishStep1"),
    };
  }
  const now = input.now ?? Date.now();
  const covered = authorizedSubnets(input.subnets, input.scopes, now).length;
  const remedy = t("onboarding.journey.authorize.remedy");
  if (covered === 0) {
    return { ...base, state: "blocked", status: t("onboarding.journey.authorize.none"), remedy };
  }
  if (covered < input.subnets.length) {
    return {
      ...base,
      state: "partial",
      status: t("onboarding.journey.authorize.partial", { covered, total: input.subnets.length }),
      remedy,
    };
  }
  return {
    ...base,
    state: "done",
    status: t("onboarding.journey.authorize.done", { count: covered }),
    remedy: "",
  };
}

function sweepStep(input: JourneyInput): JourneyStep {
  const base = {
    key: "sweep" as const,
    title: t("onboarding.journey.sweep.title"),
    to: input.selected ? `/ipam/subnets/${input.selected.id}` : "/ipam",
    toLabel: input.selected
      ? t("onboarding.journey.sweep.open", { prefix: input.selected.prefix })
      : t("onboarding.journey.sweep.chooseNetwork"),
    missingPermission: gate(input.permissions, "ipam.sweep.run"),
  };
  if (input.subnets === undefined || input.scopes === undefined) {
    return { ...base, state: "unknown", status: t("onboarding.journey.sweep.loading"), remedy: "" };
  }
  const now = input.now ?? Date.now();
  const authorized = input.subnets.length ? authorizedSubnets(input.subnets, input.scopes, now) : [];
  if (authorized.length === 0) {
    return {
      ...base,
      state: "blocked",
      status: t("onboarding.journey.sweep.noneAuthorized"),
      remedy: t("onboarding.journey.sweep.noneAuthorizedRemedy"),
    };
  }

  // `no_agent` GETS ITS OWN STATE AND ITS OWN REMEDY, never an empty list.
  // "nothing answered" and "nothing could look" mean opposite things and IPAM
  // records the difference deliberately so a screen can show it. The remedy is
  // the one thing an operator would never guess: a sweep originates from an
  // enrolled agent INSIDE the segment, because the control plane never probes
  // an endpoint (invariant 2) -- so the first machine on a new segment has to
  // be installed by hand, and only then can it find the rest.
  if (input.latestRun?.state === "no_agent") {
    return {
      ...base,
      state: "blocked",
      status: t("onboarding.journey.sweep.noAgent", {
        network: input.selected?.prefix ?? t("onboarding.journey.sweep.thisNetwork"),
      }),
      remedy: t("onboarding.journey.sweep.noAgentRemedy"),
      to: "/agent-packages",
      toLabel: t("onboarding.journey.sweep.noAgentToLabel"),
      missingPermission: gate(input.permissions, "agent.release.read"),
    };
  }

  const swept = authorized.filter((subnet) => subnet.last_swept_at != null).length;
  if (swept === 0) {
    return {
      ...base,
      state: "blocked",
      status: t("onboarding.journey.sweep.noneSwept"),
      remedy: t("onboarding.journey.sweep.noneSweptRemedy"),
    };
  }
  if (swept < authorized.length) {
    return {
      ...base,
      state: "partial",
      status: t("onboarding.journey.sweep.partial", { swept, total: authorized.length }),
      remedy: t("onboarding.journey.sweep.partialRemedy"),
    };
  }
  return {
    ...base,
    state: "done",
    status: t("onboarding.journey.sweep.done", { count: swept }),
    remedy: "",
  };
}

function reviewStep(input: JourneyInput): JourneyStep {
  const base = {
    key: "review" as const,
    title: t("onboarding.journey.review.title"),
    to: "/discovered",
    toLabel: t("onboarding.journey.review.toLabel"),
    missingPermission: gate(input.permissions, "devices.read"),
  };
  // THIS STEP NEVER BLOCKS THE NEXT ONE, and that is deliberate rather than an
  // oversight. Whether a sweep's findings become asset records on their own or
  // only when an operator promotes them is a decision belonging to the asset
  // module, and it is being changed underneath this page: as of the operator's
  // ruling of 2026-08-26 a sweep records what it finds as an asset directly,
  // which turns "review the candidates" from a GATE into a review after the
  // fact. A journey that gated step 5 on "you have promoted these" would be
  // wrong the day that landed, and would read as broken to an operator whose
  // records already exist. So this step REPORTS, and its wording is true under
  // both regimes: it names what to look at rather than what to do to it, and
  // it names BOTH places findings can land, because under the new one a clean
  // Discovered list is not evidence that nothing was found.
  if (input.candidates === undefined) {
    return { ...base, state: "unknown", status: t("onboarding.journey.review.loading"), remedy: "" };
  }
  const { total, unsweepable_subnets: unsweepable } = input.candidates;
  // "nothing was found" and "nothing could look" are distinct sentences,
  // because they are distinct facts.
  const blind = unsweepable > 0 ? ` ${t("onboarding.journey.review.blind", { count: unsweepable })}` : "";
  // SAID WHENEVER A SWEEP HAS RUN, not only when Discovered has rows. A sweep
  // that writes its findings straight into the fleet list leaves Discovered
  // empty, and "nothing is waiting for you here" would then be read as
  // "nothing was found" -- the opposite of the truth.
  const alsoInFleet = t("onboarding.journey.review.alsoInFleet");
  if (total === 0) {
    return {
      ...base,
      state: unsweepable > 0 ? "partial" : "done",
      status: `${t("onboarding.journey.review.nothingWaiting")}${blind}`,
      remedy:
        (unsweepable > 0 ? `${t("onboarding.journey.review.goBackStep3")} ` : "") + alsoInFleet,
    };
  }
  return {
    ...base,
    state: "partial",
    status: `${t("onboarding.journey.review.answered", { count: total })}${blind}`,
    remedy: `${t("onboarding.journey.review.answeredRemedy")} ${alsoInFleet}`,
  };
}

function agentStep(input: JourneyInput): JourneyStep {
  const base = {
    key: "agent" as const,
    title: t("onboarding.journey.agent.title"),
    to: "/agent-packages",
    toLabel: t("onboarding.journey.agent.toLabel"),
    missingPermission: gate(input.permissions, "agent.release.read"),
  };
  if (input.releases === undefined) {
    return { ...base, state: "unknown", status: t("onboarding.journey.agent.loading"), remedy: "" };
  }
  const installable = input.releases.filter((release) => release.has_artifact).length;
  if (installable === 0) {
    return {
      ...base,
      state: "blocked",
      status: t("onboarding.journey.agent.none"),
      // The catalogue being empty is not a fault, and the Agent Packages page
      // already explains at length why nothing publishes itself. Repeating the
      // whole explanation here would be a second copy to keep true; naming the
      // cause and sending them to the page that owns it is not.
      remedy: t("onboarding.journey.agent.noneRemedy"),
    };
  }
  return {
    ...base,
    state: "done",
    status: t("onboarding.journey.agent.ready", { count: installable }),
    remedy: t("onboarding.journey.agent.readyRemedy"),
  };
}

export function journeySteps(input: JourneyInput): readonly JourneyStep[] {
  return [
    registerStep(input),
    authorizeStep(input),
    sweepStep(input),
    reviewStep(input),
    agentStep(input),
  ];
}

/**
 * The first step that is not satisfied — "you are here".
 *
 * `unknown` COUNTS AS UNFINISHED rather than as a place to stand: pointing at a
 * step whose query has not answered would move the marker as the page loads.
 * Returns `null` only when every step is `done`.
 */
export function currentStep(steps: readonly JourneyStep[]): JourneyStep | null {
  return steps.find((step) => step.state !== "done") ?? null;
}
