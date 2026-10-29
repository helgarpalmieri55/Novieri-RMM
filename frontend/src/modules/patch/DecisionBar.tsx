import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { ConfirmDialog } from "../../components/confirm";
import { ForEveryClientToggle } from "../../components/forEveryClient";
import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { internalClientId, newRowClient } from "../../lib/internalClient";
import { Btn } from "../../shell/components";
import { useClientName } from "../settings/api";
import { useAuthorizingChanges, usePatchBulkApproveMutation } from "./api";
import {
  type ApprovalOwner,
  approvalTarget,
  bulkApprovedMessage,
  deviceLensWarning,
  EVERY_CLIENT,
  ringReachMessage,
  selectAllMatchingLabel,
  selectionCeilingNotice,
  selectionSummary,
} from "./patchCopy";
import { BULK_APPROVAL_MAX, type MatchingSelection } from "./selection";

/** A ring an approval can go into, and how many machines it holds. */
export interface RingOption {
  name: string;
  /** Every client's machines in the ring. `null` until `GET /patch/rings` has
   *  answered: a blast radius guessed at is worse than one not shown. */
  deviceCount: number | null;
  /** Of those, each client's, by client id (`client_device_counts`); a client
   *  absent has none. A client's approval reaches only its own (MSP clients
   *  plan 2026-10-26 Task 14 review). `null` until answered. */
  clientCounts: Readonly<Record<string, number>> | null;
}

/**
 * THE ONE PLACE AN APPROVAL IS DECIDED, WHEREVER THE OPERATOR IS STANDING.
 *
 * WHAT THE OPERATOR REPORTED, 2026-08-27: "i can now see the grouped pending
 * updates per host, but when i open that host i can't approve the updates for
 * it, i have to go back and approve 25 updates per page, that's not ideal".
 * Two defects in one sentence, and this component is the answer to both.
 *
 * THE MODEL TENSION, RESOLVED RATHER THAN PAPERED OVER. `patch_approvals` is
 * `(org_id, patch_id, ring_name)`. There is no device column and
 * `approve_patch` takes no device id, so "approve the updates for this host"
 * is not a thing the database can express, and inventing a per-device approval
 * in the console would be inventing a guarantee the apply path does not
 * honour. What an operator actually wants is nearly always the honest version
 * of it: they know THIS machine needs these updates, and they want them
 * cleared. So the device is a LENS for choosing which updates — the ring is
 * still the scope, and `deviceLensWarning` says so, in the bar, before the
 * click, and again in the confirmation. The blast radius is visible at the
 * moment of the decision, which is the same rule the fleet list already
 * followed.
 *
 * AND THE BLAST RADIUS IS THE OWNER'S (MSP clients plan 2026-10-26 Task 14
 * and its review). An approval now belongs to one client -- the ROW's own
 * client on a machine's drill-down (the operator's rule of 2026-10-28), the
 * internal client otherwise until phase 3's selector -- and clears that
 * client's machines in the ring and no other's; "For every client" makes it
 * global, clearing every client's. The warning and the confirmation state the
 * reach of the approval actually being made: the owner's count, from
 * `client_device_counts`, never the ring's whole population for a client's.
 *
 * WHY IT IS ALWAYS ON SCREEN. The bulk bar used to exist only once a row was
 * ticked. Three things were wrong with that: the ring chooser and the blast
 * radius — the two facts that make the decision readable — were invisible
 * until after the operator had committed to a selection; the table jumped down
 * by a row's height on the first tick; and "select all N matching", the
 * control that fixes the 25-at-a-time complaint, had nowhere to live, because
 * it is by definition something you reach for BEFORE selecting anything. So
 * the bar is present whenever there is something to decide, and its idle state
 * carries a real enabled action rather than a greyed-out "Approve 0 updates".
 *
 * IT IS NOT A SECOND MAGENTA. `Approve` renders only once a selection exists;
 * at rest the bar's only control is `Select all`, which reveals rather than
 * commits and takes the secondary treatment (DESIGN.md, The Two Voices Rule).
 */
export function DecisionBar({
  selectedIds,
  onSelectionChange,
  matchingTotal,
  resolveMatching,
  ringOptions,
  fleetDeviceTotal,
  scopeHostname = null,
  scopeName,
  rowClientId = null,
}: {
  /** The ids ticked right now. Owned by the caller, because the row checkboxes
   *  live in the caller's table. */
  selectedIds: readonly string[];
  onSelectionChange: (ids: string[]) => void;
  /** How many rows the CURRENT criterion matches server-side — `total` off the
   *  same response the table is paged from, so the bar and the table cannot
   *  disagree about how much there is. */
  matchingTotal: number;
  /** Resolve that criterion to an explicit id list. See `selection.ts`. */
  resolveMatching: () => Promise<MatchingSelection>;
  ringOptions: readonly RingOption[];
  fleetDeviceTotal: number | null;
  /** The machine whose list this bar sits under, or `null` on the fleet list.
   *  Present means the device is a LENS and the warning is shown. */
  scopeHostname?: string | null;
  /** Distinguishes this bar's controls in the accessibility tree from the
   *  other bars on the page. A device drill-down and the fleet list both carry
   *  a "Ring to approve into" select; a screen-reader user moving by form
   *  control needs to know which is which. */
  scopeName: string;
  /** The client of the ROW this bar decides from -- the machine's, on its
   *  drill-down. An approval made here is filed under it (the operator's rule
   *  of 2026-10-28); with none, under the internal client until phase 3. */
  rowClientId?: string | null;
}) {
  const t = useT();
  const api = useApi();
  const approve = usePatchBulkApproveMutation();
  const changes = useAuthorizingChanges();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const [ring, setRing] = useState<string>(ringOptions[0]?.name ?? "test");
  const [changeId, setChangeId] = useState<string>("");
  // AN APPROVAL IS ONE CLIENT'S unless the person asks for every client's
  // (MSP clients plan 2026-10-26 Task 14): a client's approval clears that
  // client's machines in the ring, a global one every client's.
  const [forEveryClient, setForEveryClient] = useState(false);
  // `/me` had not answered when the approval was confirmed: nothing was sent,
  // and the bar says why (`InternalScopeUnknown`).
  const [ownerError, setOwnerError] = useState<unknown>(null);
  // TRUE only while the confirmation is open; nothing is sent until it is
  // answered.
  const [confirming, setConfirming] = useState(false);
  const [resolving, setResolving] = useState(false);
  const [resolveError, setResolveError] = useState<unknown>(null);
  /** Set by the last "select all" — how many the criterion matched and whether
   *  the ceiling cut it. Kept so the bar can keep saying "500 of 812" after
   *  the selection is made, not only while the button is being read. */
  const [lastResolved, setLastResolved] = useState<MatchingSelection | null>(null);

  const option = ringOptions.find((entry) => entry.name === ring);
  const ringDeviceCount = option?.deviceCount ?? null;
  const reach = ringReachMessage(ring, ringDeviceCount, fleetDeviceTotal);

  // WHOSE MACHINES THIS APPROVAL CLEARS, and how many of them are in the ring:
  // every client's when asked; otherwise the row's client, or the internal
  // client's (its id is the organisation's, `lib/internalClient.ts`).
  const clientName = useClientName();
  const internalId = internalClientId(me.data);
  const ownerId = forEveryClient ? null : (rowClientId ?? internalId);
  const owner: ApprovalOwner = forEveryClient
    ? EVERY_CLIENT
    : {
        kind: "client",
        name:
          ownerId == null || ownerId === internalId
            ? t("patch.decision.ownClient")
            : clientName(ownerId),
      };
  const ownerCount = forEveryClient
    ? ringDeviceCount
    : ownerId == null || option?.clientCounts == null
      ? null
      : (option.clientCounts[ownerId] ?? 0);
  const ceilingNotice = selectionCeilingNotice(matchingTotal, BULK_APPROVAL_MAX);
  const selectedCount = selectedIds.length;

  async function selectAllMatching() {
    setResolveError(null);
    setResolving(true);
    try {
      const resolved = await resolveMatching();
      setLastResolved(resolved);
      onSelectionChange(resolved.ids);
    } catch (error) {
      // The alternative is a button that does nothing: the walk is several
      // requests and any one of them can 403 or 503, and a selection that
      // silently stayed at zero reads as a control that is broken rather than
      // a request that was refused.
      setResolveError(error);
    } finally {
      setResolving(false);
    }
  }

  if (matchingTotal === 0) return null;

  return (
    <div className="filterbar border-t border-line bg-surface-2">
      {selectedCount > 0 ? (
        <>
          <span className="cell-title">{selectionSummary(selectedCount, matchingTotal)}</span>
          <RingSelect
            ring={ring}
            onChange={setRing}
            options={ringOptions}
            scopeName={scopeName}
          />
          {changes.data && changes.data.length > 0 ? (
            <select
              className="select-field"
              value={changeId}
              onChange={(event) => setChangeId(event.target.value)}
              aria-label={t("patch.decision.changeAria", { scope: scopeName })}
            >
              <option value="">{t("patch.decision.noChange")}</option>
              {changes.data.map((change) => (
                <option key={change.id} value={change.id}>
                  {change.title}
                </option>
              ))}
            </select>
          ) : null}
          <Btn
            label={t("patch.decision.approve", { count: selectedCount })}
            ariaLabel={t("patch.decision.approveAria", {
              count: selectedCount,
              ring,
              chosen: scopeHostname
                ? t("patch.decision.chosenFrom", { count: selectedCount, hostname: scopeHostname })
                : "",
            })}
            variant="primary"
            small
            disabled={approve.isPending}
            onClick={() => {
              approve.reset();
              setConfirming(true);
            }}
          />
          <ForEveryClientToggle
            code="patch.approve"
            checked={forEveryClient}
            onChange={(checked) => {
              setForEveryClient(checked);
              approve.reset();
            }}
          />
          <Btn
            label={t("patch.decision.clearSelection")}
            ariaLabel={t("patch.decision.clearAria", { scope: scopeName })}
            variant="ghost"
            small
            onClick={() => {
              setLastResolved(null);
              onSelectionChange([]);
            }}
          />
        </>
      ) : (
        <>
          {/* THE CONTROL THAT ENDS THE TWENTY-FIVE-AT-A-TIME COMPLAINT, and it
              says how many it will take before it takes them. Secondary, not
              magenta: selecting reveals, it does not commit. */}
          <Btn
            label={
              resolving
                ? t("patch.decision.selecting")
                : selectAllMatchingLabel(matchingTotal, BULK_APPROVAL_MAX)
            }
            ariaLabel={t("patch.decision.selectAllAria", {
              label: selectAllMatchingLabel(matchingTotal, BULK_APPROVAL_MAX),
              scope: scopeName,
            })}
            small
            disabled={resolving}
            onClick={() => void selectAllMatching()}
          />
          <RingSelect
            ring={ring}
            onChange={setRing}
            options={ringOptions}
            scopeName={scopeName}
          />
        </>
      )}

      {/* THE BLAST RADIUS, ALWAYS, NOT ONLY ONCE SOMETHING IS TICKED. It is
          the fact that makes the ring chooser beside it mean anything.

          ONE SENTENCE, NOT TWO. The device lens needs to say both what the
          ring reaches and that the host is not the scope, and the first draft
          said them separately — "…covers all 12 devices in the test ring —
          not srv-dc01 alone." above "The test ring covers 12 of 286 devices."
          Printing 12 twice in adjacent lines reads as two different facts and
          costs a line of the drill-down for none. The lens sentence carries the
          population, so on that surface it is the only one. */}
      <span className="note w-full sm:w-auto">
        {scopeHostname ? deviceLensWarning(scopeHostname, ring, ownerCount, owner) : (reach ?? "")}
      </span>

      {ceilingNotice ? (
        <span className="note w-full">{ceilingNotice}</span>
      ) : null}

      {lastResolved?.truncated ? (
        <span role="status" className="note w-full">
          {t("patch.decision.truncated", {
            count: lastResolved.ids.length,
            matching: lastResolved.matching,
          })}
        </span>
      ) : null}

      {resolveError ? (
        <span role="alert" className="form-error w-full">
          {t("patch.decision.nothingSelected")} {describeSubmitError(resolveError)}
        </span>
      ) : null}

      {/* `String(approve.error)` rendered "Error: " for a bodyless 503 and was
          announced to nobody, so an approval the server refused looked like one
          it took. */}
      {approve.isError || ownerError ? (
        <span role="alert" className="form-error w-full">
          {describeSubmitError(approve.isError ? approve.error : ownerError)}
        </span>
      ) : null}

      {/* WHAT THE SERVER SAID IT DID, which is not the same as what was ticked
          — see `bulkApprovedMessage`. Announced, because the alternative is the
          Scan defect: a control that goes back to looking exactly as it did,
          and an operator with nothing to read clicking again. */}
      {approve.isSuccess ? (
        <span role="status" className="note w-full">
          {bulkApprovedMessage(approve.data, approve.variables?.ring_name ?? ring)}
        </span>
      ) : null}

      {/* APPROVING IS WHAT AUTHORISES AN UPDATE TO BE INSTALLED: the apply path
          ships only what an approval covers, so this click is the gate between
          a catalogued update and code running on every device in that ring.
          `broad` is the whole fleet.

          NO `requireTyping`: the act is reversible from the Approvals panel by
          a named actor, and typing a word here would teach the
          copy-without-reading habit that makes the confirmations which DO need
          it worthless. */}
      <ConfirmDialog
        open={confirming}
        title={t("patch.decision.confirmTitle")}
        subject={t("patch.decision.subject", { count: selectedCount, ring })}
        body={
          `${
            scopeHostname
              ? `${deviceLensWarning(scopeHostname, ring, ownerCount, owner)} `
              : ""
          }${t("patch.decision.bodyIntro", {
            count: selectedCount,
            target: approvalTarget(owner, ring, ownerCount),
          })}${changeId ? "" : ` ${t("patch.decision.noChangeNote")}`}`
        }
        confirmLabel={t("patch.decision.confirmLabel")}
        destructive
        onCancel={() => setConfirming(false)}
        onConfirm={() => {
          setConfirming(false);
          if (selectedIds.length === 0) return;
          let filedUnder: { client_id?: string };
          try {
            // The ROW's client where there is one (a machine's drill-down);
            // none at all when asked for every client.
            filedUnder =
              forEveryClient || rowClientId == null
                ? // PHASE 3: the client/site selector. Until it exists an
                  // approval with no row is the organisation's internal
                  // client's (`lib/internalClient.ts`), or, when asked, every
                  // client's.
                  newRowClient(me.data, forEveryClient)
                : { client_id: rowClientId };
          } catch (error) {
            // `/me` has not answered: nothing is sent without its client.
            setOwnerError(error);
            return;
          }
          setOwnerError(null);
          approve.mutate(
            {
              patch_ids: [...selectedIds],
              ring_name: ring,
              ...(changeId ? { change_id: changeId } : {}),
              ...filedUnder,
            },
            // Cleared ONLY on a 2xx. A failed batch keeps the selection, so the
            // operator can read the error and retry the same decision rather
            // than reconstruct it.
            {
              onSuccess: () => {
                setLastResolved(null);
                onSelectionChange([]);
              },
            },
          );
        }}
      />
    </div>
  );
}

/**
 * The ring, and how many machines it holds, on the option itself.
 *
 * The rings arrive in ROLLOUT order (`ring_order`) rather than alphabetically —
 * test before pilot before broad is the staged rollout, and a list sorted any
 * other way invites the operator to reach for the widest ring first.
 */
function RingSelect({
  ring,
  onChange,
  options,
  scopeName,
}: {
  ring: string;
  onChange: (ring: string) => void;
  options: readonly { name: string; deviceCount: number | null }[];
  scopeName: string;
}) {
  const t = useT();
  return (
    <select
      className="select-field"
      value={ring}
      onChange={(event) => onChange(event.target.value)}
      aria-label={t("patch.decision.ringAria", { scope: scopeName })}
    >
      {options.map((option) => (
        <option key={option.name} value={option.name}>
          {option.deviceCount == null
            ? option.name
            : t("patch.decision.ringOption", { name: option.name, count: option.deviceCount })}
        </option>
      ))}
    </select>
  );
}
