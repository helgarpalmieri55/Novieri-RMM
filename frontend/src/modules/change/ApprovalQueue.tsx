import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { describeSubmitError } from "../../components/form";
import { Button, Table, Td, Th, inputClass } from "../../components/ui";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDateTime } from "../../lib/datetime";
import { useT } from "../../i18n/context";
import { useApproveChangeMutation, useChanges, useRejectChangeMutation, useUserTimezone } from "./api";
import { changeStateKind, changeStateLabel, changeTypeLabel, riskKind, riskLabel } from "./changeState";
import type { ChangeRequest } from "./types";
import { PageHead, Panel, QueryState, StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";

// Submitted changes awaiting an approve/reject decision. The approve/reject
// controls are HIDDEN entirely without change.approve (server-side RBAC is
// authoritative; hiding here is purely cosmetic). The approve control is
// additionally disabled on the caller's own change as a cosmetic nod to the
// segregation-of-duties guard the backend enforces regardless
// (app/modules/change_mgmt/state.py next_state: "approver cannot be the
// change requester").
export function ApprovalQueue() {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const canApprove = (me.data?.permissions ?? []).includes("change.approve");
  const zone = useUserTimezone();

  const changes = useChanges();
  const submitted = (changes.data ?? []).filter((change) => change.state === "submitted");

  return (
    <div>
      <PageHead
        title={t("change.approvals.title")}
        description={t("change.approvals.description")}
        actions={
          <Link className="btn" to="/changes">
            <Icon name="git-branch" size={14} />
            {t("change.common.allChanges")}
          </Link>
        }
      />
      <Panel bare>
        <QueryState
          loading={changes.isLoading}
          error={changes.error}
          empty={submitted.length === 0}
          emptyLabel={t("change.approvals.empty")}
          emptyIcon="circle-check"
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("change.common.name")}</Th>
                <Th>{t("change.common.risk")}</Th>
                <Th>{t("change.common.state")}</Th>
                {canApprove && <Th>{t("change.approvals.decisionHeader")}</Th>}
              </tr>
            </thead>
            <tbody>
              {submitted.map((change) => (
                <ApprovalRow
                  key={change.id}
                  change={change}
                  zone={zone}
                  canApprove={canApprove}
                  isOwnChange={change.created_by === me.data?.user_id}
                />
              ))}
            </tbody>
          </Table>
        </QueryState>
      </Panel>
    </div>
  );
}

function ApprovalRow({
  change,
  zone,
  canApprove,
  isOwnChange,
}: {
  change: ChangeRequest;
  zone: string | null;
  canApprove: boolean;
  isOwnChange: boolean;
}) {
  const t = useT();
  const [comment, setComment] = useState("");
  const approve = useApproveChangeMutation();
  const reject = useRejectChangeMutation();

  return (
    <tr>
      {/* The review measured this cell at ~65px on a 390px screen:
          `CR-118 Upgrade core switch firmware` as six stacked one-word lines,
          in a row 200px tall. The column had no measure and the three columns
          beside it took the width. 260px, clipped, with the full title on the
          cell and on the link to the change. */}
      <Td lead={<Icon name="git-branch" size={18} />} gutter={riskKind(change.risk)} clip={260}>
        <div className="cell-title">
          <Link to={`/changes/${change.id}`}>{change.title}</Link>
        </div>
        <div className="cell-sub">
          {t("change.approvals.windowSub", { type: changeTypeLabel(change.type), start: formatDateTime(change.window_start, zone) })}
        </div>
      </Td>
      <Td>
        <StatusPill value={change.risk} label={riskLabel(change.risk)} kind={riskKind(change.risk)} />
      </Td>
      <Td>
        <StatusPill value={change.state} label={changeStateLabel(change.state)} kind={changeStateKind(change.state)} />
      </Td>
      {canApprove && (
        <Td>
          <div className="flex items-center gap-2">
            <input
              className={inputClass}
              placeholder={t("change.approvals.commentPlaceholder")}
              aria-label={t("change.approvals.commentAria", { title: change.title })}
              value={comment}
              onChange={(e) => setComment(e.target.value)}
            />
            <Button
              disabled={approve.isPending || isOwnChange}
              title={isOwnChange ? t("change.approvals.ownChangeTitle") : undefined}
              onClick={() => approve.mutate({ id: change.id, body: { comment: comment || null } })}
            >
              {t("change.common.approve")}
            </Button>
            <Button
              variant="outline"
              disabled={reject.isPending}
              onClick={() => reject.mutate({ id: change.id, body: { comment: comment || null } })}
            >
              {t("change.common.reject")}
            </Button>
          </div>
          {/* `{String(approve.error ?? reject.error)}` rendered
              "[object Object]", and for the bodyless 503 a proxy returns it
              rendered an EMPTY STRING -- an approval decision that never
              landed, on a page whose whole job is that decision. The status
              drives the sentence now, and `role="alert"` announces it to a
              screen reader that has no reason to look back at this row. */}
          {(approve.isError || reject.isError) && (
            <p role="alert" className="form-error mt-1">
              {describeSubmitError(approve.error ?? reject.error)}
            </p>
          )}
        </Td>
      )}
    </tr>
  );
}
