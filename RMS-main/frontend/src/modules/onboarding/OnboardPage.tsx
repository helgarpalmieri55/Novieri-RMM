/**
 * Add devices — the one journey, from an empty network to an enrolled fleet.
 *
 * `docs/modules/agent-distribution/spec-push-install.md` §10.
 *
 * IT OWNS NO CONTROL AND DUPLICATES NONE. Every step deep-links into the page
 * that already holds the act, under the permission that page already enforces.
 * The scan-authorization screen in particular stays exactly where it is: that
 * grant decides where this platform puts packets on somebody's wire, it is
 * admin-tier for that reason, and reimplementing its form behind a friendlier
 * gate would move a high-privilege act onto a page designed to feel easy.
 *
 * WHAT IT ADDS is the thread. Five features across three nav groups compose
 * into "find machines and put agents on them", and nothing in the console said
 * so -- which is why an operator asked for a feature that already existed. This
 * page says where you are and what is stopping the next step, and nothing else.
 *
 * THE THREE HONESTY RULES, carried from the pages it wraps and enforced in
 * `journey.ts` rather than here:
 *
 *   1. A step the caller may not perform says so and NAMES the permission. It
 *      is never hidden -- a hidden prerequisite is indistinguishable from a
 *      broken feature, which is the failure that produced the request.
 *   2. `no_agent` and "nothing found" are different states with different
 *      words. They render identically as an empty list and mean opposite
 *      things.
 *   3. No step renders a count nothing backs. A query that has not answered is
 *      `unknown`, not zero.
 *
 * DRAWN AS v3's `steps`: one `Steps` list, the bar and the pill carrying the
 * state, the destination as the action on the right. THE BAR REPORTS THE
 * STEP'S STATE, NOT THE OPERATOR'S POSITION: the kit's `kind` (a wave-2
 * design addition) draws `.step.warn .sbar` for a step that is neither done
 * nor untouched, so two steps wearing the same pill wear the same gutter
 * whichever one the operator happens to be standing on. Where they are is
 * carried by `aria-current="step"` and by the words "You are here", both of
 * which a colour bar cannot say to a screen reader anyway.
 */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { Btn, PageHead, Panel, QueryState, StatusPill, TableWrap } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { permissionsSentence } from "../../lib/permissions";
import { Steps, type Kind, type StepState as StepBar } from "../../shell/viz";
import { Td, Th } from "../../components/ui";
import { useIpamScanAuthorizations, useIpamSubnets, useIpamSweepRuns } from "../ipam/api";
import { formatAge } from "../ipam/ipamState";
import type { SubnetOut } from "../ipam/types";
import { currentStep, journeySteps, type JourneyStep, type StepState } from "./journey";

/** The state as a pill kind. `unknown` is deliberately the quietest: a query
 *  that has not answered must not look like a verdict. */
const KIND: Record<StepState, Kind> = {
  done: "ok",
  partial: "warn",
  blocked: "warn",
  unknown: "mut",
};

const LABEL: Record<StepState, MessageKey> = {
  done: "onboarding.state.done",
  partial: "onboarding.state.partial",
  blocked: "onboarding.state.blocked",
  unknown: "onboarding.state.unknown",
};

/** v3's three bar states: satisfied, the one the operator is on, the rest. */
function bar(step: JourneyStep, here: boolean): StepBar {
  if (here) return "now";
  return step.state === "done" ? "done" : "todo";
}

/**
 * THE SEVERITY RUNG, and why the bar is not decided by `bar()` alone.
 *
 * `bar()` answers "where is the operator"; three of its four values leave the
 * gutter blank, so a step that is half finished or blocked drew nothing at all
 * unless it happened to be the current one. Two steps could then carry the
 * identical "Partly done" pill and be drawn with different gutters -- the one
 * under the cursor accented, the one below it empty -- which reads as a
 * difference in state that does not exist.
 *
 * So a step whose state has a severity says so, and the kit's `.step.warn`
 * (added to the design in wave 2 for exactly this) paints the rung. `done`
 * already has `.step.ok` from `bar()` and `unknown` deliberately has no rung:
 * a query that has not answered must not look like a verdict, which is the
 * same reason its pill is the muted one.
 */
function rung(step: JourneyStep): Kind | undefined {
  return step.state === "partial" || step.state === "blocked" ? KIND[step.state] : undefined;
}

export function OnboardPage() {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];

  const subnets = useIpamSubnets();
  const scopes = useIpamScanAuthorizations();
  const releases = useQuery({
    queryKey: ["agent-releases"],
    queryFn: () => api.listAgentReleases(),
  });
  const candidates = useQuery({
    queryKey: ["asset-candidates"],
    queryFn: () => api.listAssetCandidates(),
  });

  /**
   * WHICH NETWORK THE OPERATOR IS ASKING ABOUT.
   *
   * Step 3's most important state -- `no_agent`, "nothing could look here" --
   * lives on a sweep RUN, and runs are per subnet. Fetching every subnet's
   * history to render one line would be N requests on page load for a number
   * most of them do not change. So the fleet-wide steps read the subnet rows
   * (which carry `last_swept_at`), and the one selected subnet gets its run
   * history. Nothing is selected until the operator picks one, and the step
   * says so rather than pretending the fleet has one answer.
   */
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected: SubnetOut | null =
    subnets.data?.find((subnet) => subnet.id === selectedId) ?? null;
  const runs = useIpamSweepRuns(selected?.id, 1);

  const steps = journeySteps({
    permissions,
    subnets: subnets.data,
    scopes: scopes.data,
    selected,
    // `undefined` while the request is in flight, `null` once it has answered
    // with no runs at all -- "never swept" and "not loaded" are different
    // facts and the step words them differently.
    latestRun: selected == null ? null : runs.data === undefined ? undefined : (runs.data[0] ?? null),
    candidates: candidates.data,
    releases: releases.data,
  });
  const here = currentStep(steps);

  return (
    <>
      <PageHead
        title={t("onboarding.page.title")}
        description={t("onboarding.page.description")}
      />

      {/* THE COMMON CASE FIRST. This page is the console's one "Add a
          computer" entry (top bar and Devices head), and most of the time the
          machine is in front of the operator: the review found this panel at
          the bottom, below a five-step network journey. */}
      <Panel
        title={t("onboarding.page.oneComputer.title")}
        subtitle={t("onboarding.page.oneComputer.subtitle")}
        actions={
          <>
            <Link className="btn" to="/agent-packages">
              <Icon name="download" size={14} />
              <span>{t("onboarding.page.oneComputer.getInstaller")}</span>
            </Link>
            <Link className="btn" to="/enrollment">
              <Icon name="certificate" size={14} />
              <span>{t("onboarding.page.oneComputer.enrollmentTokens")}</span>
            </Link>
          </>
        }
      >
        {/* KEPT, AND DELIBERATELY. The private half never leaves the machine
            on either path -- the one fact here an operator would be surprised
            by, and the console says it nowhere else. */}
        <p className="note">
          {t("onboarding.page.oneComputer.note")}
        </p>
      </Panel>

      {/* SAID ONCE, AT THE TOP, BECAUSE IT SHAPES EVERY STEP BELOW. An
          operator who does not know it reads step 3's `no_agent` as a bug and
          step 5 as a missing button. What was cut is the mechanism -- who runs
          a sweep, who runs an installer -- which is a docs page (copy-voice
          spec rule 5); what stays is the invariant and the consequence for
          step 1. */}
      <p className="note mb-4">
        {t("onboarding.page.manyNote")}
      </p>

      <Panel title={t("onboarding.page.manyTitle")} bare>
        <Steps
          items={steps.map((step) => {
            const isHere = here?.key === step.key;
            return {
              label: step.title,
              state: bar(step, isHere),
              kind: rung(step),
              detail: (
                <>
                  {/* THE JOURNEY STATE SITS IN THE DETAIL LINE, so v3's
                      `.sgo` holds one thing: the destination. A pill and a
                      button side by side doubled the right column on every
                      row, and the current step read "Partly done" beside a
                      primary. */}
                  <StatusPill value={step.state} kind={KIND[step.state]} label={t(LABEL[step.state])} />{" "}
                  {step.status}
                  {step.remedy ? <> {step.remedy}</> : null}
                  {/* THE PERMISSION IS NAMED, AND THE LINK IS STILL OFFERED.
                      Hiding the destination would hide the prerequisite; the
                      route refuses the write server-side either way
                      (invariant 6), so what this changes is whether the
                      operator learns what to ask for or discovers a 403. */}
                  {step.missingPermission ? (
                    <span className="block note">
                      {t("onboarding.page.needsAdmin", {
                        permissions: permissionsSentence([step.missingPermission]),
                      })}
                    </span>
                  ) : null}
                  {isHere ? <span className="block note">{t("onboarding.page.youAreHere")}</span> : null}
                </>
              ),
              // Every link is kept (the destination is the step). A step
              // with no destination leaves the slot to `Steps`, which draws
              // its own state pill there, as v3's blocked step 05 does.
              action: step.to ? (
                <Link className={isHere ? "btn primary" : "btn"} to={step.to}>
                  <Icon name="arrow-right" size={14} />
                  <span>{step.toLabel}</span>
                </Link>
              ) : undefined,
            };
          })}
        />
      </Panel>

      {/* THE PER-NETWORK TABLE, and it is not a second copy of the subnet list.
          It carries exactly the two columns the journey is about -- is this
          authorized, and when was it last swept -- because those are the two an
          operator cannot get from any single existing screen. Everything else
          about a subnet stays on the subnet page. */}
      <Panel
        title={t("onboarding.page.networks.title")}
        subtitle={t("onboarding.page.networks.subtitle")}
        bare
      >
        <QueryState
          loading={subnets.isLoading}
          error={subnets.error}
          empty={(subnets.data ?? []).length === 0}
          emptyLabel={t("onboarding.page.networks.empty")}
          emptyIcon="network"
        >
          <TableWrap>
            <table>
              <thead>
                <tr>
                  <th scope="col">{t("onboarding.page.networks.network")}</th>
                  <th scope="col">{t("onboarding.page.networks.lastSwept")}</th>
                  {/* `Th` with no children renders the `sr-only` name AND
                      makes the cell its containing block; a bare sr-only span
                      in a `th` widens the document at 390px (see `Th`). */}
                  <Th />
                </tr>
              </thead>
              <tbody>
                {(subnets.data ?? []).map((subnet) => (
                  <tr key={subnet.id}>
                    <Td lead={<Icon name="network" size={18} />}>
                      <div className="cell-title mono">{subnet.prefix}</div>
                      <div className="cell-sub">{subnet.name || t("onboarding.page.networks.unnamed")}</div>
                    </Td>
                    {/* AN AGE, NEVER A CLOCK TIME. IPAM's own rule: "last
                        swept" answers how stale the documentation is, not
                        whether anything is up, and a timestamp invites the
                        second reading. */}
                    <td className="mono">{formatAge(subnet.last_swept_at)}</td>
                    <td>
                      <Btn
                        small
                        label={
                          subnet.id === selectedId
                            ? t("onboarding.page.networks.selected")
                            : t("onboarding.page.networks.checkThisOne")
                        }
                        icon={subnet.id === selectedId ? "check" : undefined}
                        ariaLabel={
                          subnet.id === selectedId
                            ? t("onboarding.page.networks.selectedAria", { prefix: subnet.prefix })
                            : t("onboarding.page.networks.checkAria", { prefix: subnet.prefix })
                        }
                        onClick={() => setSelectedId(subnet.id === selectedId ? null : subnet.id)}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
        </QueryState>
      </Panel>
    </>
  );
}
