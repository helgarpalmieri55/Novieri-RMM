import type { UseQueryResult } from "@tanstack/react-query";
import { useState } from "react";
import { RelTime } from "../../components/reltime";
import { ConfirmDialog } from "../../components/confirm";
import { describeSubmitError } from "../../components/form";
import { RowMenu } from "../../components/rowmenu";
import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { formatDateTime } from "../../lib/datetime";
import { Panel, QueryState, StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useApprovalActorDirectory, usePatchRevokeApprovalMutation } from "./api";
import { approverName } from "./patchCopy";
import type { ApprovalListResponse, PatchApproval } from "./types";

/**
 * The update an approval covers, named.
 *
 * OFF THE ROW ITSELF. This panel used to take an `identifierFor` prop, and the
 * page behind it built the map by fetching 500 catalogue rows on every visit —
 * for a table of a handful. `ApprovalOut.patch_identifier` carries the name
 * now; `null` means this org has no patch by that id, and the uuid is the
 * honest fallback because it is the only handle on such a row.
 */
function patchName(approval: PatchApproval): string {
  return approval.patch_identifier ?? approval.patch_id;
}

/**
 * The account behind an actor id, as a person reads it.
 *
 * The name is ordinary text; the unresolved id keeps the mono treatment this
 * console gives every raw wire handle (the audit page's actor cell, the update
 * identifiers two columns to the left), so the two are never mistaken for one
 * another.
 */
function ActorName({
  id,
  users,
}: {
  id: string;
  users: readonly { id: string; display_name: string; email: string }[] | undefined;
}) {
  const name = approverName(users, id);
  return name != null ? (
    <span>{name}</span>
  ) : (
    <span className="mono" title={id}>
      {id}
    </span>
  );
}

/**
 * WHAT IS CLEARED TO INSTALL, AND WHAT ONCE WAS.
 *
 * THE LIST IS WHY THE REVOKE EXISTS AT ALL. `DELETE /patch/approvals/{id}`
 * shipped with the module and had never been callable — an approval id came
 * back from exactly one place, the POST that created it, so once that response
 * scrolled away the clearance was permanent for anybody who was not looking at
 * the screen at the time.
 *
 * REVOKED ROWS STAY, AND ARE MARKED. The backend soft-revokes so the trail
 * holds (spec §5); a console that filtered them out would tell a different
 * story from the audit log about what was ever approved. The `Status` column is
 * the whole difference between a record and a live gate, and a revoked row
 * carries no Revoke control because there is nothing left to withdraw.
 *
 * THE UPDATE IS NAMED FROM THE ROW. `patch_identifier` arrives on the
 * approval; this panel used to be handed a lookup built from a 500-row
 * catalogue fetch that the page made on every visit and rendered nowhere.
 *
 * "WHICH RINGS IS THIS UPDATE CLEARED FOR" IS NO LONGER ASKED HERE. That is a
 * column on the update row now, computed server-side; this panel had been
 * doing double duty as the source of that column while being capped itself,
 * which is why it used to carry a disclaimer saying the column under-reports.
 */
export function ApprovalsPanel({
  query,
  canApprove,
  canReadUsers,
  zone,
}: {
  query: UseQueryResult<ApprovalListResponse>;
  canApprove: boolean;
  /** Whether this operator holds `users.read`. It decides whether the By
   *  column can name a person at all -- see `useApprovalActorDirectory`. */
  canReadUsers: boolean;
  zone: string | null;
}) {
  const t = useT();
  const revoke = usePatchRevokeApprovalMutation();
  // The approval the operator has ASKED to revoke, not the one being revoked:
  // nothing is sent until the confirmation is answered.
  const [pendingRevoke, setPendingRevoke] = useState<PatchApproval | null>(null);

  const items = query.data?.items ?? [];
  const total = query.data?.total ?? 0;

  // ONLY WHEN THERE IS SOMEBODY TO NAME. A page of policy auto-approvals
  // carries no actor id, and a directory read to resolve nothing is a request
  // this page can simply not make.
  const directory = useApprovalActorDirectory(
    canReadUsers && items.some((approval) => approval.approved_by != null),
  );
  const users = directory.data?.users;

  const approvalSubject = (approval: PatchApproval): string =>
    t("patch.approvals.subject", { patch: patchName(approval), ring: approval.ring_name });

  return (
    <>
      <Panel
        title={t("patch.approvals.title")}
        subtitle={t("patch.approvals.subtitle")}
        bare
      >
        {/* 18px is the inset every other element inside a `bare` panel sits at
            (`.filterbar`), so these lines align with the table rather than with
            the panel's edge.

            ALWAYS, NOT ONLY WHEN THE CAP BITES — the rule `pageRangeMessage`
            was written for, applied to the one list in this module that was
            still breaking it. A note that appears only past 100 approvals
            means that below 100 the reader has no way to know a cap exists at
            all, and then learns it from a list that has quietly stopped being
            complete. */}
        {query.data != null && total > 0 && (
          <p className="note px-4.5 pt-2.5 pb-1.5">
            {items.length < total
              ? t("patch.approvals.showingNewest", { count: items.length, total })
              : t("patch.approvals.total", { count: total })}
          </p>
        )}
        {revoke.isError && (
          <div className="mx-4.5 mt-2.5">
            <p role="alert" className="notice red">
              <Icon name="circle-x" size={16} />
              <span>
                {t("patch.approvals.revokeFailed")}{" "}
                {describeSubmitError(revoke.error)}
              </span>
            </p>
          </div>
        )}
        <QueryState
          loading={query.isLoading}
          error={query.error}
          empty={query.data != null && items.length === 0}
          emptyIcon="patch"
          emptyLabel={t("patch.approvals.empty")}
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("patch.approvals.update")}</Th>
                <Th>{t("patch.approvals.ring")}</Th>
                <Th>{t("patch.approvals.approved")}</Th>
                <Th>{t("patch.approvals.by")}</Th>
                <Th>{t("patch.approvals.status")}</Th>
                <Th>{""}</Th>
              </tr>
            </thead>
            <tbody>
              {items.map((approval) => (
                <tr key={approval.id} data-approval-id={approval.id}>
                  <Td lead={<Icon name="patch" size={18} />}>
                    <div className="cell-title mono">{patchName(approval)}</div>
                  </Td>
                  <Td>{approval.ring_name}</Td>
                  <Td>
                    <RelTime iso={approval.approved_at} zone={zone} />
                  </Td>
                  <Td>
                    {/* `null` here is a POLICY auto-approval (PD-02), not an
                        unknown actor, and the two must not print the same.

                        A PERSON IS NAMED IF THIS CONSOLE CAN NAME THEM. The
                        row carries a uuid; the id survives as the fallback in
                        mono, `title`d with itself so it is readable in full,
                        because an unresolvable account (no `users.read`, or a
                        deleted row) still approved this update and printing
                        nothing would be the worse answer. */}
                    {approval.approved_by == null ? (
                      t("patch.approvals.policyAuto")
                    ) : (
                      <ActorName id={approval.approved_by} users={users} />
                    )}
                  </Td>
                  <Td>
                    {approval.revoked ? (
                      <>
                        <StatusPill value="revoked" kind="crit" label={t("patch.approvals.revoked")} />
                        <div className="cell-sub mono">
                          {formatDateTime(approval.revoked_at, zone)}
                        </div>
                      </>
                    ) : (
                      <StatusPill value="in_force" kind="ok" label={t("patch.approvals.inForce")} />
                    )}
                  </Td>
                  <Td>
                    {canApprove && !approval.revoked && (
                      <div className="flex justify-end">
                        <RowMenu
                          subject={approvalSubject(approval)}
                          items={[
                            {
                              label: t("patch.approvals.revoke"),
                              destructive: true,
                              onSelect: () => {
                                revoke.reset();
                                setPendingRevoke(approval);
                              },
                            },
                          ]}
                        />
                      </div>
                    )}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </QueryState>
      </Panel>

      {/* REVOKING IS DESTRUCTIVE and takes the console's destructive treatment:
          it withdraws the clearance for an update to reach machines, and
          re-approving is a new decision by a named actor rather than an undo.
          No `requireTyping`: the blast radius is one (patch, ring) pair that can
          be approved again, and typing a name here would teach the
          copy-without-reading habit that makes the confirmations which DO need
          it worthless. */}
      <ConfirmDialog
        open={pendingRevoke != null}
        title={t("patch.approvals.confirmTitle")}
        subject={pendingRevoke ? approvalSubject(pendingRevoke) : ""}
        body={
          pendingRevoke
            ? t("patch.approvals.confirmBody", { ring: pendingRevoke.ring_name })
            : ""
        }
        confirmLabel={t("patch.approvals.confirmLabel")}
        destructive
        onCancel={() => setPendingRevoke(null)}
        onConfirm={() => {
          const approval = pendingRevoke;
          setPendingRevoke(null);
          if (approval) revoke.mutate(approval.id);
        }}
      />
    </>
  );
}
