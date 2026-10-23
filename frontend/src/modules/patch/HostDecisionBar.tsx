import { useState } from "react";
import { ConfirmDialog } from "../../components/confirm";
import { describeSubmitError } from "../../components/form";
import { translate } from "../../i18n/core";
import { useT } from "../../i18n/context";
import { Btn } from "../../shell/components";
import { usePatchApplyMutation } from "./api";
import { applyOutcomeMessage, hostSelectionSummary, selectAllHostsLabel } from "./patchCopy";
import { APPLY_HOST_MAX, type MatchingHosts, type SelectedHost } from "./selection";

/**
 * THE MACHINE-SCOPED SIBLING OF `DecisionBar`, AND IT COMMITS ONE THING ONLY.
 *
 * WHY IT EXISTS. The operator, 2026-08-26, choosing the Linux model with the
 * trade-offs in front of them: "Apply per device, security-first. Schema
 * change: NONE. Unit of decision: the machine ... Why: apt/dnf resolve
 * dependencies as a SET — cherry-picking single packages often isn't actionable
 * anyway." `DecisionBar` selects UPDATES and approves them into a ring. This
 * one selects MACHINES and installs on them. Everything structural is borrowed
 * from it unchanged: always on screen so the operator can read what a control
 * costs before committing to a selection, "select all N matching" resolved to
 * an explicit list in the browser, a `ConfirmDialog` that states the blast
 * radius, and no optimistic success anywhere.
 *
 * ------------------------------------------------------------------------
 * THE BUTTON THE OPERATOR ASKED FOR IS NOT THE BUTTON THIS SHIPS, AND THAT IS
 * DELIBERATE.
 *
 * They sketched "Update security" / "Update all". Neither is expressible.
 * `dispatch_apply` (`backend/app/modules/patch/apply.py`) takes no severity
 * argument, so "security only" cannot be asked for; and every patch it ships
 * must already carry an un-revoked `patch_approvals` row for the device's
 * resolved ring, so "all" cannot be promised either. What the endpoint really
 * does is one thing:
 *
 *     install, on these machines, what is ALREADY APPROVED for their ring —
 *     and only on the ones whose maintenance window is open right now.
 *
 * So that is the label. A button reading "Update security" that shipped
 * whatever happened to be approved would be worse than useless on the one
 * screen in this product that authorises root-level code execution; the module
 * brief says so in as many words, and `patchCopy.applyOutcomeMessage` carries
 * the rest of the reasoning.
 *
 * WHERE SECURITY-FIRST ACTUALLY LIVES, since it is a real requirement and not
 * a wish. Two places, both already built, both honest:
 *   - `PatchPolicy.auto_approve_security`, the standing setting that clears
 *     security-severity updates into the `test` ring as each scan reports them
 *     (`ingest.py` step 5); and
 *   - this tab's severity default, which is critical+important — and on apt
 *     that is the security pocket EXACTLY (`apt.go`'s `aptSeverity`), so the
 *     evidence panel's own "Select all N updates" control is literally "approve
 *     every outstanding Linux security update". It is named that way there.
 *
 * ------------------------------------------------------------------------
 * ONE MAGENTA, AND ONLY ONCE THERE IS SOMETHING TO COMMIT. At rest this bar's
 * only control is `Select all`, which reveals rather than commits and takes the
 * secondary treatment (DESIGN.md, The Two Voices Rule). `Install` appears with
 * the selection. That is the same rule `DecisionBar` follows, for the same
 * reason.
 */
export function HostDecisionBar({
  selected,
  onSelectionChange,
  matchingTotal,
  resolveMatching,
  familyLabel,
  canApply,
}: {
  /** The machines ticked right now, named. Owned by the caller, because the row
   *  checkboxes live in the caller's table. */
  selected: readonly SelectedHost[];
  onSelectionChange: (hosts: SelectedHost[]) => void;
  /** How many machines the CURRENT criterion matches server-side — `total` off
   *  the same `GET /patch/device-rollup` response the table is paged from, so
   *  the bar and the table cannot disagree about how much there is. */
  matchingTotal: number;
  resolveMatching: () => Promise<MatchingHosts>;
  familyLabel: string;
  canApply: boolean;
}) {
  const t = useT();
  const apply = usePatchApplyMutation();
  const [confirming, setConfirming] = useState(false);
  const [resolving, setResolving] = useState(false);
  const [resolveError, setResolveError] = useState<unknown>(null);
  const [lastResolved, setLastResolved] = useState<MatchingHosts | null>(null);
  /** How many hosts the LAST request was sent for. Read after it returns, so
   *  the outcome sentence cannot be recomputed against a selection the operator
   *  has since cleared or changed. */
  const [dispatchedFor, setDispatchedFor] = useState(0);

  async function selectAllMatching() {
    setResolveError(null);
    setResolving(true);
    try {
      const resolved = await resolveMatching();
      setLastResolved(resolved);
      onSelectionChange(resolved.hosts);
    } catch (error) {
      // A selection that silently stayed at zero reads as a broken control
      // rather than a refused request; the walk is several round trips and any
      // one of them can 403 or 503.
      setResolveError(error);
    } finally {
      setResolving(false);
    }
  }

  if (matchingTotal === 0 || !canApply) return null;

  const count = selected.length;

  return (
    <div className="filterbar border-t border-line bg-surface-2">
      {count > 0 ? (
        <>
          <span className="cell-title">{hostSelectionSummary(count, matchingTotal)}</span>
          <Btn
            label={t("patch.hostBar.install")}
            ariaLabel={t("patch.hostBar.installAria", {
              count,
              hostname: (selected[0] as SelectedHost).hostname,
              family: familyLabel,
            })}
            variant="primary"
            small
            disabled={apply.isPending}
            onClick={() => {
              apply.reset();
              setConfirming(true);
            }}
          />
          <Btn
            label={t("patch.hostBar.clearSelection")}
            ariaLabel={t("patch.hostBar.clearAria", { family: familyLabel })}
            variant="ghost"
            small
            onClick={() => {
              setLastResolved(null);
              onSelectionChange([]);
            }}
          />
        </>
      ) : (
        <Btn
          label={resolving ? t("patch.hostBar.selecting") : selectAllHostsLabel(matchingTotal, APPLY_HOST_MAX)}
          ariaLabel={t("patch.hostBar.selectAllAria", {
            label: selectAllHostsLabel(matchingTotal, APPLY_HOST_MAX),
            family: familyLabel,
          })}
          small
          disabled={resolving}
          onClick={() => void selectAllMatching()}
        />
      )}

      {/* WHAT THIS CONTROL IS FOR, AND WHEN IT WILL DO NOTHING — said before
          the click, not discovered from an empty result.

          Approved updates already install on their own: the worker's window
          sweep runs `dispatch_apply` for the whole org, so a machine whose
          window opens tonight gets its patches tonight with nobody present.
          What this button adds is IMMEDIACY, and `window_open` gates it exactly
          as it gates the sweep — so on a host whose window is shut, "install
          now" is a no-op, and an operator who did not know that would read the
          empty `job_ids` as a failure. */}
      <span className="note w-full sm:w-auto">
        {t("patch.hostBar.note")}
      </span>

      {lastResolved?.truncated ? (
        <span role="status" className="note w-full">
          {t("patch.hostBar.truncated", {
            count: lastResolved.hosts.length,
            matching: lastResolved.matching,
          })}
        </span>
      ) : null}

      {resolveError ? (
        <span role="alert" className="form-error w-full">
          {t("patch.hostBar.nothingSelected")} {describeSubmitError(resolveError)}
        </span>
      ) : null}

      {apply.isError ? (
        <span role="alert" className="form-error w-full">
          {describeSubmitError(apply.error)}
        </span>
      ) : null}

      {/* WHAT THE SERVER SAID IT DISPATCHED, which is routinely fewer jobs than
          hosts. See `applyOutcomeMessage`: one job per eligible device, and a
          device outside its window produces none. */}
      {apply.isSuccess ? (
        <span role="status" className="note w-full">
          {applyOutcomeMessage(apply.data?.job_ids, dispatchedFor)}
        </span>
      ) : null}

      {/* THE ONE PLACE IN THIS MODULE WHERE THE CLICK RUNS CODE.
          Approving authorises; this dispatches. `patch.apply` is a signed job
          the agent verifies and executes as root, so the dialog names the
          machines, states the two gates it still has to clear, and says what
          the agent will and will not do about a restart — which is a stated gap
          rather than a policy, and the operator should not learn it from a
          machine that stayed up. */}
      <ConfirmDialog
        open={confirming}
        title={t("patch.hostBar.confirmTitle")}
        subject={t("patch.hostBar.subject", {
          count,
          hostname: (selected[0] as SelectedHost | undefined)?.hostname ?? "",
          family: familyLabel,
        })}
        body={`${namesSentence(selected)} ${t("patch.hostBar.bodyTail")}`}
        confirmLabel={t("patch.hostBar.confirmLabel")}
        destructive
        onCancel={() => setConfirming(false)}
        onConfirm={() => {
          setConfirming(false);
          if (selected.length === 0) return;
          setDispatchedFor(selected.length);
          apply.mutate({ device_ids: selected.map((host) => host.id) });
        }}
      />
    </div>
  );
}

/**
 * The machines, by name, in the confirmation.
 *
 * NAMED RATHER THAN COUNTED, up to a readable number of them. "Install on 3
 * hosts" is a number an operator can agree to without knowing which three, and
 * the whole reason this path exists is that the machine is the unit of the
 * decision. Past four the list stops being readable and the count plus the
 * first few is the honest compromise — the table behind the dialog still holds
 * the selection, and the dialog is cancellable.
 */
function namesSentence(hosts: readonly SelectedHost[]): string {
  const names = hosts.map((host) => host.hostname);
  if (names.length === 1) return translate("patch.hostBar.namesOne", { name: names[0] as string });
  if (names.length <= 4) {
    return translate("patch.hostBar.namesFew", {
      list: names.slice(0, -1).join(", "),
      last: names.at(-1) as string,
    });
  }
  return translate("patch.hostBar.namesMany", {
    count: names.length,
    first: names.slice(0, 3).join(", "),
  });
}
