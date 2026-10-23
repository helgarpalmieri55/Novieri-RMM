/**
 * The sentences the patch page says about what it did, and the small
 * arithmetic behind them.
 *
 * PULLED OUT OF `FleetPatchPage.tsx` when it was split, because these are the
 * parts with no JSX in them and the ones most worth testing directly: each is
 * a claim about what the server actually did, and this module has twice
 * shipped a page that claimed something it had not measured.
 */

import { translate } from "../../i18n/core";
import { humaniseEnum } from "../../lib/labels";
import { FAMILY_LABEL } from "./patchTabs";

/** The severities that demand a decision, and the console's default filter.
 *
 *  REVERSIBLE, AND HERE IS THE RISK IT CARRIES.
 *  `agent/internal/capabilities/patch/winupdate.go` maps an unrated
 *  `IUpdate.MsrcSeverity` to `unknown`, and its own comment says most Windows
 *  11 cumulative updates carry no MSRC severity despite containing security
 *  fixes -- so on Windows this default can push the monthly rollup off the
 *  first screen. It is aimed at the platform that actually produces the long
 *  list: `apt.go` marks a security-pocket version `important` and everything
 *  else `unknown`, which is exactly the 300-package Ubuntu case the operator
 *  reported. Nothing is HIDDEN by it -- the count of what it excludes is on
 *  the page beside the table, one click widens it, the device panel is never
 *  severity-filtered and the evidence export is untouched. Changing it is
 *  this one line. */
export const DEFAULT_SEVERITIES = ["critical", "important"] as const;

/** The URL value that means "the severity filter is off", as opposed to an
 *  absent parameter, which means "nobody has chosen, so use the default". The
 *  two must be distinguishable or the widening control cannot be linked to. */
export const ALL_SEVERITIES_PARAM = "all";

/**
 * The severity filter this URL asks for: `null` -> the default, `all` -> no
 * filter, otherwise the comma-separated list.
 *
 * THE DEFAULT IS NOW THE TAB'S, not the module's, and the caller supplies it.
 * `defaultSeveritiesFor` in `patchTabs.ts` carries the derivation: apt's
 * `important` is the security pocket exactly, Microsoft leaves most cumulative
 * updates unrated, and Apple publishes no severity at all, so one constant
 * cannot be right on three platforms. An absent parameter still means "nobody
 * has chosen"; what it resolves to is what changed.
 */
export function severitiesFromParam(value: string | null, fallback: readonly string[]): string[] {
  if (value === null) return [...fallback];
  if (value === ALL_SEVERITIES_PARAM) return [];
  return value.split(",").filter(Boolean);
}

export function severityParamFor(severities: string[]): string {
  return severities.length === 0 ? ALL_SEVERITIES_PARAM : severities.join(",");
}

/** "1 update" / "4 updates" — used in the button, the count and the dialog, so
 *  the three cannot disagree about what is about to happen. */
export function updateCount(count: number): string {
  return translate("patch.count.updates", { count });
}

export function deviceCount(count: number): string {
  return translate("patch.count.devices", { count });
}

/**
 * "3 of 60 Windows machines with an agent need at least one update."
 *
 * TWO NUMBERS, AND THE SECOND ONE IS THE PLATFORM'S. "1 device needs patches"
 * says nothing about whether that is the whole estate or a corner of it — and
 * the inverse of that mistake, reading a findings list's length as a device
 * count, is what put "66 exposed devices" on this page for one machine missing
 * 66 packages.
 *
 * THE DENOMINATOR CANNOT COME FROM THE PATCH API. `fleet_scan_coverage`
 * computes `devices_total` for the whole ORGANISATION whatever `os_family` was
 * filtered on, so on a platform tab it would measure this platform's machines
 * against a fleet that is mostly not this platform. It comes from
 * `GET /devices?os_family=…` instead, and is `null` when that is unreadable —
 * in which case the sentence says less rather than saying something false.
 */
export function machinesNeedingUpdates(
  needing: number,
  managed: number | null,
  platform: string | null,
): string {
  const noun = platform
    ? translate("patch.copy.platformMachinesNoun", { platform })
    : translate("patch.copy.machinesNoun");
  if (managed == null) return translate("patch.copy.needingUnknown", { count: needing });
  return translate("patch.copy.needingOf", { count: needing, managed, noun });
}

/**
 * "Showing 1–25 of 300 updates." — where the page is inside the whole set.
 *
 * NOT OPTIONAL, and not only when the page is truncated. The previous console
 * printed a note only when the catalogue exceeded its cap, so on any smaller
 * fleet the reader had no way to know a cap existed at all; a page that always
 * states its position teaches the reader that `total` is the real number.
 */
export function pageRangeMessage(shown: number, offset: number, total: number): string {
  if (total === 0) return "";
  const first = offset + 1;
  const last = offset + shown;
  return translate("patch.copy.pageRange", { first, last, count: total });
}

/**
 * What the severity filter is currently hiding, in words.
 *
 * THE HALF THAT KEEPS A FILTERED COUNT FROM READING AS A TOTAL. "8 updates" on
 * a machine missing 300 is a lie the operator cannot detect from the page
 * alone, and the operator's report was that the page was already telling them
 * something useless. Returns `""` when the filter hides nothing, because a
 * permanent notice about zero hidden rows is noise that trains the reader to
 * skip the notice that matters.
 */
export function hiddenBySeverityMessage(total: number, totalUnfiltered: number): string {
  const hidden = totalUnfiltered - total;
  if (hidden <= 0) return "";
  return translate("patch.copy.hidden", { count: hidden });
}

/**
 * What the scan actually dispatched, in a sentence.
 *
 * THIS IS THE FINDING, not a nicety. Auditor A (HIGH), /patch -> Scan now:
 * "queues a fleet scan with zero feedback — and this is demonstrably what
 * produced the duplicate jobs on the dashboard". The control disabled itself
 * for the length of the request and then went back to looking exactly as it
 * had, so an operator with nothing to read clicked again, and the second
 * click dispatched a second scan to every device.
 *
 * Zero is reported as zero. A fleet scan that matched no device is a fact the
 * operator needs — rounding it up to "queued" is how a scan that never ran
 * gets mistaken for one that did.
 */
export function scanQueuedMessage(data: { job_ids: string[] } | undefined): string {
  const count = data?.job_ids.length ?? 0;
  if (count === 0) return translate("patch.copy.scanNone");
  return translate("patch.copy.scanQueued", { count });
}

/**
 * What the batch actually changed, in a sentence.
 *
 * NOT THE NUMBER OF ROWS THAT WERE TICKED. Re-approving a patch already cleared
 * for this ring changes nothing and writes no audit event, so the server reports
 * `newly_approved` and `already_in_force` separately — and a console printing
 * the selection size would be claiming approvals the log does not contain. An
 * operator sweeping the list after a scan will re-tick cleared rows constantly,
 * so this is the common case rather than an edge one.
 */
export function bulkApprovedMessage(
  data: { newly_approved: number; already_in_force: number } | undefined,
  ring: string,
): string {
  if (!data) return "";
  const approved = translate("patch.copy.approved", { count: data.newly_approved, ring });
  if (data.already_in_force === 0) return approved;
  const already = translate("patch.copy.alreadyInForce", { count: data.already_in_force });
  return `${approved} ${already}`;
}

/* ------------------------------------------------------------------------ *
 * THE SENTENCES A RING DECISION IS MADE ON.
 *
 * All four below exist because approving is `(org_id, patch_id, ring_name)`
 * and has NO DEVICE COLUMN. The operator, 2026-08-27: "when i open that host i
 * can't approve the updates for it". They can now — but the thing they are
 * doing when they do it is not "approve for this host", because the database
 * cannot express that. The device is a LENS for choosing WHICH updates; the
 * ring is the scope. These sentences are how that is said at the moment of the
 * decision rather than discovered afterwards in the Approvals list.
 * ------------------------------------------------------------------------ */

/**
 * How far one approval reaches, in words. `null` when nobody knows yet.
 *
 * Approving clears an update for every machine that resolves into the ring —
 * now AND at every future scan — so the ring's population is the reach of the
 * decision itself rather than of any one install. It is not derivable in the
 * browser: it needs `patch_policies` joined to `devices` through
 * `device_groups` with the org-default fallback, which is why
 * `GET /patch/rings` computes it with the same resolver the approval uses.
 * A blast radius guessed at is worse than one not shown, so an unanswered
 * `/patch/rings` returns `null` here and the caller prints nothing.
 */
export function ringReachMessage(
  ring: string,
  ringDeviceCount: number | null,
  fleetDeviceTotal: number | null,
): string | null {
  if (ringDeviceCount == null) return null;
  if (ringDeviceCount === 0) return translate("patch.copy.ringEmpty", { ring });
  return fleetDeviceTotal == null
    ? translate("patch.copy.ringCovers", { ring, count: ringDeviceCount })
    : translate("patch.copy.ringCoversOf", { ring, count: ringDeviceCount, total: fleetDeviceTotal });
}

/**
 * THE SENTENCE THAT KEEPS A LENS FROM READING AS A SCOPE.
 *
 * Said on the device drill-down, where an operator has just picked updates off
 * one machine's list and is about to clear them for a whole ring. Without it
 * the most natural reading of the screen — "I am approving srv-dc01's
 * updates" — is wrong in the one direction that matters: it understates the
 * blast radius.
 */
export function deviceLensWarning(
  hostname: string,
  ring: string,
  ringDeviceCount: number | null,
): string {
  return translate("patch.copy.lens", { hostname, reach: ringReach(ring, ringDeviceCount) });
}

/** How many devices an approval into `ring` reaches, as a phrase ("all 12 devices in the test ring"). */
function ringReach(ring: string, ringDeviceCount: number | null): string {
  return ringDeviceCount == null
    ? translate("patch.copy.reachEvery", { ring })
    : translate("patch.copy.reachCount", { ring, count: ringDeviceCount });
}

/**
 * The label on the control that turns the CURRENT FILTER into an explicit
 * selection.
 *
 * WHY THE FILTER IS RESOLVED IN THE BROWSER AND SENT AS A LIST.
 * `BulkApprovalCreateRequest` takes `patch_ids` rather than a filter, and its
 * docstring says exactly why: "A filter ('approve everything critical') is
 * evaluated on the server AFTER the operator has read the screen, so the set
 * that gets cleared is not the set they looked at; a list is exactly what they
 * ticked." That principle is untouched here. The control below resolves the
 * filter to ids at the instant it is clicked, states the exact count before
 * anything is sent, and sends that list — so the set approved is still the set
 * the operator decided on. What it removes is the twelve page-loads of ticking
 * that stood between the decision and the click.
 */
export function selectAllMatchingLabel(matching: number, ceiling: number): string {
  return matching > ceiling
    ? translate("patch.copy.selectFirst", { ceiling, matching })
    : translate("patch.copy.selectAll", { count: matching });
}

/**
 * WHAT HAPPENS PAST THE CEILING, SAID BEFORE IT HAPPENS.
 *
 * `""` when the filter fits. Above it the alternative — quietly approving the
 * first 500 of 812 — is the same defect as a filtered count read as a total,
 * one layer along: the operator commits to a number the screen never showed
 * them. So the count, the ceiling and the remainder are all named, and the
 * control's own label says "the first 500 of 812" rather than "select all".
 */
export function selectionCeilingNotice(matching: number, ceiling: number): string {
  if (matching <= ceiling) return "";
  return translate("patch.copy.ceilingNotice", { matching, ceiling, rest: matching - ceiling });
}

/**
 * "12 updates selected of 300 matching." — what is about to be approved, and
 * what it was chosen out of.
 *
 * BOTH NUMBERS, ALWAYS. A selection can now exceed the page it was made on, so
 * "12 selected" beside a table of 25 rows is no longer self-evidently about
 * what is on screen. The second number is the criterion it came from.
 */
export function selectionSummary(selected: number, matching: number): string {
  return translate("patch.copy.selectionSummary", { count: selected, matching });
}

/* ------------------------------------------------------------------------ *
 * THE SENTENCES THE PER-MACHINE PATH SAYS, AND WHAT THEY ARE ALLOWED TO CLAIM.
 *
 * The operator, 2026-08-26, choosing the Linux model: "Apply per device,
 * security-first ... UI: pick host(s) -> 'Update security' / 'Update all' /
 * schedule ... Already built: POST /apply {device_ids}".
 *
 * `POST /apply` IS BUILT AND IT IS NOT THAT. `dispatch_apply`
 * (`backend/app/modules/patch/apply.py`) takes no severity argument at all, so
 * "security only" is not a thing it can be asked for. What it does, exactly, is
 * batch into ONE signed job every patch on the device that clears three gates:
 *
 *   1. an UN-REVOKED `patch_approvals` row for (org, patch, the device's
 *      resolved ring) -- so nothing installs that was not approved;
 *   2. `device_patch_status.state` in {approved, missing};
 *   3. `window_open(policy.maintenance_window, now)` -- and a device whose
 *      window is shut is skipped entirely, with no job and no explanation.
 *
 * So BOTH of the buttons the operator sketched are, today, the same button:
 * "install what has already been approved for this host's ring, if its window
 * is open right now". Naming it "Update security" would be a claim the apply
 * path cannot honour, and naming it "Update all" would be a claim the approval
 * gate cannot honour. It is named for what it does, and gate 3 is why the
 * outcome sentence below counts what was SKIPPED rather than reporting a
 * request as a result.
 * ------------------------------------------------------------------------ */

/**
 * What the install actually dispatched, in a sentence.
 *
 * ONE JOB PER DEVICE (`dispatch_apply` builds exactly one per eligible device),
 * so `job_ids.length` is the number of hosts that received anything and the
 * remainder is the number that did not. Reporting a request as a result is the
 * `Scan` defect that produced duplicate fleet scans on the dashboard; reporting
 * "5 jobs queued" for 12 selected hosts without naming the other 7 would be the
 * same defect with better arithmetic.
 *
 * THE TWO REASONS ARE BOTH NAMED because the response cannot distinguish them.
 * `ApplyResponse` carries `job_ids` and nothing else, so a host that was skipped
 * for a shut maintenance window and one that had nothing approved come back
 * identically. Guessing which would be inventing a fact; naming both is the
 * true statement.
 */
export function applyOutcomeMessage(jobIds: readonly string[] | undefined, hosts: number): string {
  const queued = jobIds?.length ?? 0;
  const skipped = Math.max(0, hosts - queued);
  if (queued === 0) return translate("patch.copy.applyNothing", { count: hosts });
  const queuedPart = translate("patch.copy.applyQueued", { count: queued });
  if (skipped === 0) return queuedPart;
  return `${queuedPart} ${translate("patch.copy.applySkipped", { count: skipped })}`;
}

/** "3 hosts selected of 12 with something outstanding." — the machine-scoped
 *  sibling of `selectionSummary`, and it states both numbers for the same
 *  reason: a selection that can exceed the page it was made on is no longer
 *  self-evidently about what is on screen. */
export function hostSelectionSummary(selected: number, matching: number): string {
  return translate("patch.copy.hostSelection", { count: selected, matching });
}

/** "Select all 12 hosts" / "Select the first 500 of 812". */
export function selectAllHostsLabel(matching: number, ceiling: number): string {
  return matching > ceiling
    ? translate("patch.copy.selectFirstHosts", { ceiling, matching })
    : translate("patch.copy.selectAllHosts", { count: matching });
}

/**
 * THE SAME LENS WARNING `deviceLensWarning` MAKES, FOR A SET OF MACHINES.
 *
 * Approving from a host selection is still `(org_id, patch_id, ring_name)` with
 * no device column, so picking three Linux boxes and clearing their security
 * updates clears those updates for every machine in the ring — which will
 * usually be more than three, and on `broad` is the fleet. The single-host
 * version of this sentence has been on the drill-down since 2026-08-27; a
 * multi-host control that did not say it would be the same understatement of
 * blast radius, at three times the size.
 */
export function hostLensWarning(
  hosts: readonly string[],
  ring: string,
  ringDeviceCount: number | null,
): string {
  const chosen =
    hosts.length === 1
      ? (hosts[0] as string)
      : hosts.length === 2
        ? translate("patch.copy.chosenTwo", { first: hosts[0] as string, second: hosts[1] as string })
        : translate("patch.copy.chosenMany", { count: hosts.length });
  return translate("patch.copy.hostLens", {
    chosen,
    reach: ringReach(ring, ringDeviceCount),
    alone: hosts.length === 1 ? chosen : translate("patch.copy.thoseHosts"),
  });
}

/**
 * WHY AN EMPTY TAB IS EMPTY, AND THE THREE ANSWERS ARE NOT INTERCHANGEABLE.
 *
 * This module has already shipped this conflation twice. `CompliancePanels`
 * rendered a row of 0% bars on a fleet nothing had ever scanned, and the
 * dashboard put nine confident zeros on an unmeasured page. An empty tab has
 * three genuinely different causes and each one leads somewhere else:
 *
 *   UNENROLLED  there is no fleet of this platform. Nothing to scan, nothing to
 *               approve; the way out is enrolment, not a scan.
 *   UNSCANNED   there is a fleet and nobody has measured it. The list is empty
 *               because nothing has reported, which is the most dangerous of
 *               the three to read as an all-clear.
 *   COMPLIANT   measured, and genuinely nothing outstanding. The only one that
 *               is good news.
 *
 * And a fourth, which is the honest answer when the console cannot tell:
 *
 *   UNCOUNTED   this operator cannot read `GET /devices` (no `devices.read`),
 *               so the size of this platform's fleet is unknown here and an
 *               empty list cannot be separated from an absent one. Saying so
 *               beats picking whichever of the two sounds better.
 */
export type TabEmptyReason = "unenrolled" | "unscanned" | "compliant" | "uncounted";

export function tabEmptyReason(args: {
  /** Machines of this family that carry an agent, or `null` when unreadable. */
  enrolled: number | null;
  /** Devices anywhere in the org that have COMPLETED a patch scan. */
  devicesScanned: number;
}): TabEmptyReason {
  if (args.enrolled === 0) return "unenrolled";
  if (args.devicesScanned === 0) return "unscanned";
  if (args.enrolled == null) return "uncounted";
  return "compliant";
}

export function formatPct(value: number | undefined): string {
  return value == null ? "—" : `${value.toFixed(1)}%`;
}

/**
 * The tone a compliance bar takes.
 *
 * THRESHOLDS, NOT A GRADIENT, and they are stated here rather than inline so
 * the two compliance breakdowns cannot disagree about what "behind" means.
 * A hue is applied only where it means something (DESIGN.md, The Rationed Hue
 * Rule): green is "this slice is done", gold is "this slice is drifting", red
 * is "this slice is the problem". The bar is non-text, so the 3:1 floor
 * applies and every one of these tokens clears it.
 */
export function compliancePctTone(value: number): "gold" | "green" | "red" {
  if (value >= 90) return "green";
  if (value >= 70) return "gold";
  return "red";
}

/**
 * WHAT THE COLOUR OF A COMPLIANCE BAR MEANS, SAID ON THE PAGE.
 *
 * REVIEW, 2026-09-08 [MINOR, a11y]: "In the By severity group the bar hue
 * encodes the compliance percentage while the row label is a severity name, so
 * 'critical 77.8%' draws in the warn gold and 'important 33.3%' draws in the
 * crit red -- the severity palette inverted against the words beside it.
 * Nothing on the panel says the colour means a target band."
 *
 * That is exactly right, and the inversion is not a bug in the thresholds: a
 * severity pill's hue says how bad the update is, a compliance bar's hue says
 * how far the fleet is from being done, and the two scales genuinely run in
 * opposite directions. The console cannot have one hue mean both things on one
 * screen without saying which is which, so it says which: the legend is
 * rendered once under the panel's own heading, and each bar's accessible name
 * carries the same band in words, so the hue is never the only carrier
 * (WCAG 2.2 SC 1.4.1) and a reader who takes the gold for "important" has a
 * sentence on the page that corrects them.
 */
export function complianceToneLegend(): string {
  return translate("patch.copy.complianceLegend");
}

/** The band a tone stands for, in words, for the bar's accessible name. */
export function complianceBandLabel(tone: "" | "gold" | "green" | "red" | "mut"): string {
  if (tone === "green") return translate("patch.copy.bandGreen");
  if (tone === "gold") return translate("patch.copy.bandGold");
  if (tone === "red") return translate("patch.copy.bandRed");
  return translate("patch.copy.bandNone");
}

/**
 * A compliance row's label, as a person reads it.
 *
 * REVIEW, 2026-09-08 [MAJOR, copy]: the panel labelled its rows with the raw
 * wire values -- "windows", "macos", "critical" -- directly under tabs reading
 * "Windows / Linux / macOS" and pills reading "Critical / Important /
 * Unknown". Two casings for the same words on one screen.
 *
 * SO THE PANEL BORROWS THE NAMES THE REST OF THE PAGE ALREADY USES rather than
 * inventing a third set: `FAMILY_LABEL` is the tab strip's own map (and the
 * reason "macOS" is not "Macos"), and the severity rows take `severityLabel`
 * itself, the function the severity pill in the updates table prints through. A key neither map knows is a value this console has
 * not seen before -- a new `os_family` from a newer agent -- and it is
 * sentence-cased rather than dropped, because an unfamiliar platform being
 * behind is a fact worth reading.
 */
export function compliancePlatformLabel(key: string): string {
  return (FAMILY_LABEL as Record<string, string>)[key] ?? humaniseEnum(key, "PatchFamily");
}

/**
 * The person behind an actor id, or `null` when this console cannot say.
 *
 * REVIEW, 2026-09-08 [MINOR, copy]: the Approvals table printed raw account ids
 * in its "By" column. The wire value is a uuid (`ApprovalOut.approved_by`), so
 * the fix is a lookup, not a formatter -- and it is the lookup
 * `subscriptions/pickerOptions.ownerName` already does for `owner_user_id`,
 * for the same reason and with the same fallback.
 *
 * `null` RATHER THAN A GUESS. `GET /users` is `users.read`-gated and
 * `patch.approve` does not imply it, so an operator can legitimately be unable
 * to resolve the name; a deleted account is retained but may not be listed. In
 * either case the id itself is still the only handle on that row and printing
 * it beats printing nothing (the audit page makes the same call for
 * `actor_id`, which has no name join at all).
 */
export function approverName(
  users: readonly { id: string; display_name: string; email: string }[] | undefined,
  actorId: string,
): string | null {
  const user = users?.find((candidate) => candidate.id === actorId);
  if (!user) return null;
  return user.display_name || user.email || null;
}

// Triggers a browser download of a Blob. No existing download helper exists
// elsewhere in src/ (grepped for Blob/createObjectURL/download); this is the
// standard object-URL + anchor-click pattern.
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
