import { useQuery } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { EmptyState, NoValue } from "../../components/emptystate";
import { describeSubmitError } from "../../components/form";
import { Badge, Button, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDateTime } from "../../lib/datetime";
import { formatBytes } from "../../lib/format";
import { Icon } from "../../shell/icons";
import { KV } from "../../shell/viz";
import {
  useCreatePortalCommentMutation,
  usePortalTicket,
  useUploadPortalAttachmentMutation,
} from "./api";
import { PortalAssistantPanel } from "./PortalAssistantPanel";
import { PortalNav } from "./PortalNav";
import {
  portalApprovalLabel,
  portalApprovalVariant,
  portalTicketPriorityLabel,
  portalTicketPriorityVariant,
  portalTicketStatusLabel,
  portalTicketStatusVariant,
  portalTicketTypeLabel,
} from "./portalState";
import type { PortalAttachmentOut } from "./types";
import { Btn, PageHead, Panel, QueryState } from "../../shell/components";

// Ticket detail (spec §7): status, history, the public comment thread,
// attachments, approval outcome when present, and a reply box.
//
// This page renders NOTHING about a queue, an assignee, or an internal
// note -- not because a runtime check filters them out, but because
// `PortalTicketDetail` (types.ts) has no such fields to read in the first
// place. There is no `ticket.queue_id`, no `ticket.assigned_to`, and
// `ticket.comments` only ever contains public comments (the server's own
// query and RLS policy both enforce that before this ever reaches the
// client).
//
// The design's page (`P['portal/tickets/:ticketId']`): the subject as the
// title, the number and when it was opened under it, the state pill, then
// an "Updates" panel of the public conversation with the reply box at the
// bottom. The details, attachments and status history the console also
// serves sit beside it.
export function PortalTicketDetailPage() {
  const t = useT();
  const { ticketId } = useParams<{ ticketId: string }>();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("portal.ticket.read");
  const canWrite = permissions.includes("portal.ticket.write");
  // Cycle 5: one zone for every time on this page, read off the profile
  // (`/me.timezone`) rather than each render site guessing from the browser.
  const zone = me.data?.timezone;

  const ticket = usePortalTicket(ticketId);

  if (!ticketId) return null;

  return (
    <div>
      <QueryState
        loading={ticket.isLoading}
        error={ticket.error}
        empty={!ticket.data}
        emptyLabel={t("portal.detail.notFound")}
        emptyIcon="ticket"
      >
        {ticket.data && (
          <>
            <PageHead
              title={`${ticket.data.number}: ${ticket.data.subject}`}
              description={t("portal.detail.description", {
                opened: formatDateTime(ticket.data.created_at, zone),
                updated: formatDateTime(ticket.data.updated_at, zone),
              })}
            />
            <PortalNav canRead={canRead} />

            <div className="flex flex-wrap items-center gap-2" style={{ margin: "-8px 0 14px" }}>
              <Badge variant={portalTicketStatusVariant(ticket.data.status)}>
                {portalTicketStatusLabel(ticket.data.status)}
              </Badge>
              <Badge variant={portalTicketPriorityVariant(ticket.data.priority)}>
                {portalTicketPriorityLabel(ticket.data.priority)}
              </Badge>
              <Badge variant="muted">{portalTicketTypeLabel(ticket.data.type)}</Badge>
            </div>

            <div className="grid grid-2">
              <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}>
                <CommentsPanel
                  ticketId={ticketId}
                  description={ticket.data.description}
                  openedAt={ticket.data.created_at}
                  comments={ticket.data.comments}
                  canWrite={canWrite}
                  zone={zone}
                />
                <AttachmentsPanel
                  ticketId={ticketId}
                  attachments={ticket.data.attachments}
                  canWrite={canWrite}
                  zone={zone}
                />
              </div>
              <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}>
                <Panel title={t("portal.detail.details")}>
                  <KV
                    items={[
                      [t("portal.detail.number"), <span className="mono">{ticket.data.number}</span>],
                      [t("portal.detail.type"), portalTicketTypeLabel(ticket.data.type)],
                      [t("portal.detail.opened"), formatDateTime(ticket.data.created_at, zone)],
                      [t("portal.detail.lastUpdate"), formatDateTime(ticket.data.updated_at, zone)],
                    ]}
                  />
                </Panel>
                {ticket.data.approval && <ApprovalOutcomeCard approval={ticket.data.approval} zone={zone} />}
                <StatusHistoryPanel history={ticket.data.status_history} zone={zone} />
                {/* The resolver's one placement (in-ticket only); its routes
                    are gated on `portal.ticket.read`. */}
                {canRead && <PortalAssistantPanel />}
              </div>
            </div>
          </>
        )}
      </QueryState>
    </div>
  );
}

// The one genuinely new read this module closes (spec §0.3): a requester
// whose request was rejected can now see that it was, and why. Renders when
// present; renders nothing when absent -- there is no placeholder "no
// approval" card, because most tickets never have one at all.
function ApprovalOutcomeCard({
  approval,
  zone,
}: {
  approval: { state: string; reason: string; decided_at: string | null };
  zone?: string | null;
}) {
  const t = useT();
  return (
    <Panel title={t("portal.detail.approval")}>
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={portalApprovalVariant(approval.state)}>
          {portalApprovalLabel(approval.state)}
        </Badge>
        {/* `formatDateTime` renders NO_DATE for a null decision time, so the
            guard is only about not printing a bare em dash beside a state that
            has not been decided at all. */}
        {approval.decided_at && (
          <span className="note">{formatDateTime(approval.decided_at, zone)}</span>
        )}
      </div>
      {approval.reason && <p style={{ marginTop: 8, whiteSpace: "pre-wrap" }}>{approval.reason}</p>}
    </Panel>
  );
}

/**
 * v3's "Updates": the requester's own opening message first, then every
 * public comment as a time and a paragraph, then the reply box. Staff names
 * are not on `PortalCommentOut` (the portal cannot resolve them), so each
 * update is drawn as "Support" rather than as a person the API never named.
 */
function CommentsPanel({
  ticketId,
  description,
  openedAt,
  comments,
  canWrite,
  zone,
}: {
  ticketId: string;
  description: string;
  openedAt: string;
  comments: ReadonlyArray<{ id: string; body: string; created_at: string }>;
  canWrite: boolean;
  zone?: string | null;
}) {
  const t = useT();
  const createComment = useCreatePortalCommentMutation();
  const [body, setBody] = useState("");

  function submit() {
    createComment.mutate({ id: ticketId, body: { body } }, { onSuccess: () => setBody("") });
  }

  return (
    <Panel title={t("portal.detail.updates")}>
      <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1fr)", gap: 14 }}>
        {description ? (
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <span className="cell-title">{t("portal.detail.you")}</span>
              <span className="note">{t("portal.detail.openedAt", { when: formatDateTime(openedAt, zone) })}</span>
            </div>
            <p style={{ marginTop: 6, whiteSpace: "pre-wrap" }}>{description}</p>
          </div>
        ) : null}
        {comments.map((c) => (
          <div key={c.id}>
            <div className="flex flex-wrap items-center gap-2">
              <span className="cell-title" style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                <Icon name="message-2" size={14} /> {t("portal.detail.update")}
              </span>
              <span className="note">{formatDateTime(c.created_at, zone)}</span>
            </div>
            <p style={{ marginTop: 6, whiteSpace: "pre-wrap" }}>{c.body}</p>
          </div>
        ))}
        {comments.length === 0 && <p className="note">{t("portal.detail.noComments")}</p>}
      </div>

      {canWrite && (
        <div style={{ marginTop: 14 }}>
          {/* The same single-line defect Auditor A found on the Description
              field of /portal/new, on the control a requester answers a
              technician's question with. A reply is where an error message
              gets pasted. */}
          <Field label={t("portal.detail.reply")}>
            <textarea
              className={inputClass}
              rows={4}
              placeholder={t("portal.detail.replyPlaceholder")}
              value={body}
              onChange={(e) => setBody(e.target.value)}
            />
          </Field>
          {/* Cycle 4: `{String(createComment.error)}` rendered "ApiError: " for
              the bodyless 503 -- a class name and nothing a requester could act
              on -- and was not announced at all. */}
          {createComment.isError && (
            <p role="alert" className="notice red" style={{ marginBottom: 12 }}>
              {describeSubmitError(createComment.error)}
            </p>
          )}
          <Button onClick={submit} disabled={createComment.isPending || !body}>
            {createComment.isPending ? t("portal.detail.posting") : t("portal.detail.postReply")}
          </Button>
        </div>
      )}
    </Panel>
  );
}

// I2 fix: attachments now come from `PortalTicketDetail.attachments`
// (server-scoped to the requester's OWN uploads, I1) rather than local
// component state, so an uploaded file survives a page reload -- the
// upload mutation invalidates the ticket-detail query (api.ts) and this
// panel just renders whatever that query returns.
function AttachmentsPanel({
  ticketId,
  attachments,
  canWrite,
  zone,
}: {
  ticketId: string;
  attachments: ReadonlyArray<PortalAttachmentOut>;
  canWrite: boolean;
  zone?: string | null;
}) {
  const t = useT();
  const api = useApi();
  const upload = useUploadPortalAttachmentMutation();
  const fileInput = useRef<HTMLInputElement>(null);
  // Cycle 4/C: `void download(...)` had NO rejection path. A 403 or a 503 on
  // the download route produced an unhandled promise rejection in the console
  // and absolutely nothing on screen -- the requester clicked Download, no
  // file arrived, and the page looked exactly as it had before.
  const [downloadError, setDownloadError] = useState<unknown>(null);

  async function download(attachmentId: string, filename: string) {
    setDownloadError(null);
    try {
      const blob = await api.downloadPortalAttachment(ticketId, attachmentId);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (caught) {
      setDownloadError(caught);
    }
  }

  return (
    <Panel
      title={t("portal.detail.attachments")}
      bare
      actions={
        canWrite ? (
          <>
            <input
              ref={fileInput}
              type="file"
              className="hidden"
              aria-label={t("portal.detail.attachFile")}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (!file) return;
                upload.mutate({ id: ticketId, file });
              }}
            />
            <Btn label={t("portal.detail.attachFile")} icon="paperclip" onClick={() => fileInput.current?.click()} />
          </>
        ) : undefined
      }
    >
      {attachments.length === 0 ? (
        <EmptyState
          icon="paperclip"
          title={t("portal.detail.noAttachments")}
          body={t("portal.detail.attachmentsHelp")}
        />
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>{t("portal.detail.filename")}</Th>
              <Th>{t("portal.detail.size")}</Th>
              <Th>{t("portal.detail.uploaded")}</Th>
              <Th>{""}</Th>
            </tr>
          </thead>
          <tbody>
            {attachments.map((a) => (
              <tr key={a.id}>
                <Td lead={<Icon name="paperclip" size={18} />}>
                  <div className="cell-title">{a.filename}</div>
                </Td>
                <Td>
                  <span className="mono">{formatBytes(a.byte_size)}</span>
                </Td>
                <Td>
                  <span className="mono">{formatDateTime(a.created_at, zone)}</span>
                </Td>
                <Td>
                  <Btn label={t("portal.detail.download")} icon="download" small onClick={() => void download(a.id, a.filename)} />
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      {upload.isError && (
        <p role="alert" className="notice red" style={{ margin: 14 }}>
          {describeSubmitError(upload.error)}
        </p>
      )}
      {downloadError != null && (
        <p role="alert" className="notice red" style={{ margin: 14 }}>
          {describeSubmitError(downloadError)}
        </p>
      )}
    </Panel>
  );
}

// No "Reason" column here (C1 fix): `PortalStatusHistoryOut` no longer
// carries `reason` at all -- staff-authored, unbounded free text with no
// visibility column must never reach the requester verbatim (spec §5). A
// requester sees THAT and WHEN a status changed, never WHY in staff's own
// words.
function StatusHistoryPanel({
  history,
  zone,
}: {
  history: ReadonlyArray<{ from_status: string | null; to_status: string; created_at: string }>;
  zone?: string | null;
}) {
  const t = useT();
  return (
    <Panel title={t("portal.detail.history")} bare>
      {history.length === 0 ? (
        <EmptyState
          icon="history"
          title={t("portal.detail.noHistory")}
          body={t("portal.detail.historyHelp")}
        />
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>{t("portal.detail.from")}</Th>
              <Th>{t("portal.detail.to")}</Th>
              <Th>{t("portal.detail.changed")}</Th>
            </tr>
          </thead>
          <tbody>
            {history.map((h, index) => (
              <tr key={`${h.to_status}-${h.created_at}-${index}`}>
                <Td>
                  {h.from_status ? portalTicketStatusLabel(h.from_status) : <NoValue label={t("portal.detail.noPreviousStatus")} />}
                </Td>
                <Td>
                  <Badge variant={portalTicketStatusVariant(h.to_status)}>
                    {portalTicketStatusLabel(h.to_status)}
                  </Badge>
                </Td>
                <Td>
                  <span className="mono">{formatDateTime(h.created_at, zone)}</span>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Panel>
  );
}
