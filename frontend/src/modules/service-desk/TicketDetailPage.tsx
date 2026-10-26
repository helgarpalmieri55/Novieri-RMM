import { useQuery } from "@tanstack/react-query";
import { useRef, useState } from "react";
import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { ConfirmDialog } from "../../components/confirm";
import { EmptyState, NoValue } from "../../components/emptystate";
import { describeSubmitError } from "../../components/form";
import { EntityPicker } from "../../components/picker";
import type { PickerOption } from "../../components/picker";
import { Badge, Button, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import { ApiError } from "../../lib/api";
import type { DeviceInventory, Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDateTime } from "../../lib/datetime";
import { formatBytes } from "../../lib/format";
import { humaniseEnum, humaniseStatus } from "../../lib/labels";
import { patchStateLabel } from "../patch/patchState";
import { initialsOf, personIndex } from "../../lib/people";
import { Btn, PageHead, Panel, QueryState } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { Facts, KV, Person } from "../../shell/viz";
import type { DeviceStatus } from "../patch/types";
import { useCan, usePersonForUser } from "../people/api";
import { StartSessionDialog } from "../remote/StartSessionDialog";
import { useClients } from "../settings/api";
import { TicketSlaPanel } from "../sla/TicketSlaPanel";
import { TicketCopilotPanel } from "./TicketCopilotPanel";
import {
  useApproveTicketMutation,
  useArticleSuggestions,
  useAssignTicketMutation,
  useCannedResponses,
  useChangeTicketStatusMutation,
  useCreateTicketCommentMutation,
  useDirectoryDevices,
  useDirectoryUsers,
  useMoveTicketMutation,
  useRejectTicketMutation,
  useResolveTicketMutation,
  useTicket,
  useTicketAttachments,
  useTicketCategories,
  useTicketComments,
  useTicketQueues,
  useTicketStatusHistory,
  useUpdateTicketMutation,
  useUploadTicketAttachmentMutation,
} from "./api";
import {
  devicePickerEmptyHint,
  devicePickerOptions,
  nameForOption,
  userPickerEmptyHint,
  userPickerOptions,
} from "./pickerOptions";
import {
  FINAL_STATUSES,
  finalStatusWord,
  TERMINAL_STATUSES,
  commentVisibilityVariant,
  defaultReplyVisibility,
  describeTarget,
  canResolve,
  statusChangeWarning,
  statusesForType,
  ticketPriorityLabel,
  ticketPriorityVariant,
  ticketStatusLabel,
  ticketStatusVariant,
  ticketTypeLabel,
} from "./serviceDeskState";
import type {
  CommentVisibility,
  TicketAssign,
  TicketOut,
  TicketPriority,
  TicketUpdate,
} from "./types";

/**
 * The ticket page: a compact header (state, deadlines, requester, device and
 * the quick actions), then the conversation with its reply box on the left
 * and one edit form plus the SLA, device and copilot panels on the right.
 */
export function TicketDetailPage() {
  const t = useT();
  const { ticketId } = useParams<{ ticketId: string }>();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canWrite = permissions.includes("ticket.write");
  const canAssign = permissions.includes("ticket.assign");
  const canApprove = permissions.includes("ticket.approve");
  const canReadDocs = permissions.includes("docs.read");
  // The move panel lists `GET /clients`, which needs `clients.read`.
  const canReadClients = permissions.includes("clients.read");
  const zone = me.data?.timezone;

  const ticket = useTicket(ticketId);
  const queues = useTicketQueues();
  const queueName = (id: string) => queues.data?.find((q) => q.id === id)?.name ?? id;
  const users = useDirectoryUsers();
  // Not memoised: the sublabels are worded, and a memo would keep them in the
  // language the page opened in.
  const userOptions = userPickerOptions(users.data ?? []);

  // The reply box, opened and focused from the header's quick actions.
  const [replyVisibility, setReplyVisibility] = useState<CommentVisibility | null>(null);
  const replyBox = useRef<HTMLTextAreaElement>(null);
  function openReply(visibility: CommentVisibility) {
    setReplyVisibility(visibility);
    replyBox.current?.focus();
    replyBox.current?.scrollIntoView?.({ block: "center" });
  }

  if (!ticketId) return null;

  const data = ticket.data;
  const isTerminal = data ? TERMINAL_STATUSES.has(data.status) : false;
  const requester = data ? nameForOption(userOptions, data.requester_id) : null;

  return (
    <div>
      <QueryState
        loading={ticket.isLoading}
        error={ticket.error}
        empty={!ticket.isLoading && !ticket.error && data == null}
        emptyIcon="ticket"
        emptyLabel={t("serviceDesk.detail.notFound")}
      >
        {data && (
          <>
            <TicketHeader
              ticket={data}
              requester={requester}
              assignee={nameForOption(userOptions, data.assigned_to)}
              queueName={queueName}
              zone={zone}
              myUserId={me.data?.user_id}
              canWrite={canWrite}
              canAssign={canAssign}
              onReply={openReply}
            />

            <div className="grid grid-2">
              <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}>
                <ConversationPanel
                  ticket={data}
                  requester={requester}
                  userOptions={userOptions}
                  canWrite={canWrite}
                  canReadDocs={canReadDocs}
                  isTerminal={isTerminal}
                  zone={zone}
                  visibility={replyVisibility ?? defaultReplyVisibility(data)}
                  onVisibilityChange={setReplyVisibility}
                  replyBox={replyBox}
                />
                <AttachmentsPanel ticketId={ticketId} canWrite={canWrite} isTerminal={isTerminal} zone={zone} />
                <StatusHistoryPanel ticketId={ticketId} zone={zone} />
              </div>
              <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}>
                {data.type === "request" && data.status === "pending_approval" && (
                  <ApprovalPanel ticketId={ticketId} canApprove={canApprove} />
                )}
                {(canWrite || canAssign) && (
                  <EditTicketPanel
                    // Remount on every server change so the form starts from
                    // what was saved, not from what was typed before.
                    key={`${data.updated_at}|${data.status}|${data.assigned_to}|${data.queue_id}`}
                    ticket={data}
                    canWrite={canWrite}
                    canAssign={canAssign}
                  />
                )}
                {canWrite && canReadClients && (
                  <ClientPanel key={`${data.id}|${data.client_id}`} ticket={data} />
                )}
                <TicketSlaPanel ticketId={ticketId} />
                {data.device_id && <DeviceSummaryPanel deviceId={data.device_id} />}
                <TicketCopilotPanel ticketId={ticketId} />
              </div>
            </div>
          </>
        )}
      </QueryState>
    </div>
  );
}

/** A named person, or the id when the directory cannot name them. */
function Who({ id, name }: { id: string; name: string | null }) {
  if (!name) return <span className="mono">{id}</span>;
  return <Person initials={initialsOf(name)} index={personIndex(id)} name={name} />;
}

/** The requester's name, linked to their Person page when they have one. */
function RequesterName({ userId, name }: { userId: string; name: string | null }) {
  const t = useT();
  const canReadPeople = useCan("people.read");
  const person = usePersonForUser(userId, canReadPeople);
  const label = <Who id={userId} name={name} />;
  if (!canReadPeople || !person.data) return label;
  return (
    <Link to={`/people/${person.data.id}`} title={t("serviceDesk.detail.openPerson")}>
      {label}
    </Link>
  );
}

function TicketHeader({
  ticket,
  requester,
  assignee,
  queueName,
  zone,
  myUserId,
  canWrite,
  canAssign,
  onReply,
}: {
  ticket: TicketOut;
  requester: string | null;
  assignee: string | null;
  queueName: (id: string) => string;
  zone?: string | null;
  myUserId?: string;
  canWrite: boolean;
  canAssign: boolean;
  onReply: (visibility: CommentVisibility) => void;
}) {
  const t = useT();
  const assign = useAssignTicketMutation();
  const resolve = useResolveTicketMutation();
  const [remoteOpen, setRemoteOpen] = useState(false);
  const now = Date.now();

  const isTerminal = TERMINAL_STATUSES.has(ticket.status);
  const resolvable = canResolve(ticket.type, ticket.status);
  const canTakeIt =
    canAssign && myUserId != null && ticket.assigned_to !== myUserId && !FINAL_STATUSES.has(ticket.status);

  const actions = (
    <>
      {canTakeIt && (
        <Btn
          label={t("serviceDesk.detail.assignToMe")}
          icon="user"
          disabled={assign.isPending}
          onClick={() => assign.mutate({ id: ticket.id, body: { assigned_to: myUserId } })}
        />
      )}
      {canWrite && !isTerminal && (
        <>
          <Btn label={t("serviceDesk.detail.reply")} icon="mail" onClick={() => onReply(defaultReplyVisibility(ticket))} />
          <Btn label={t("serviceDesk.detail.addInternalNote")} icon="lock" onClick={() => onReply("internal")} />
        </>
      )}
      {canWrite && resolvable && (
        <Btn
          label={resolve.isPending ? t("serviceDesk.detail.resolving") : t("serviceDesk.detail.resolve")}
          icon="check"
          variant="primary"
          disabled={resolve.isPending}
          onClick={() => resolve.mutate({ id: ticket.id })}
        />
      )}
    </>
  );

  const sla = ticket.sla;
  const clock = (state: Parameters<typeof describeTarget>[0], dueAt: string) => {
    const due = describeTarget(state, dueAt, now);
    return (
      <span title={formatDateTime(dueAt, zone)}>
        <Badge variant={due.variant}>{due.label}</Badge>
      </span>
    );
  };

  return (
    <>
      <PageHead
        title={`${ticket.number}: ${ticket.subject}`}
        description={
          requester
            ? t("serviceDesk.detail.openedBy", {
                when: formatDateTime(ticket.created_at, zone),
                requester,
                queue: queueName(ticket.queue_id),
              })
            : t("serviceDesk.detail.opened", {
                when: formatDateTime(ticket.created_at, zone),
                queue: queueName(ticket.queue_id),
              })
        }
        actions={actions}
      />
      <div className="flex flex-wrap items-center gap-2" style={{ margin: "-8px 0 14px" }}>
        <Badge variant={ticketStatusVariant(ticket.status)}>{ticketStatusLabel(ticket.status)}</Badge>
        <Badge variant={ticketPriorityVariant(ticket.priority)}>
          {ticketPriorityLabel(ticket.priority)}
        </Badge>
        {/* Type and queue are not states, so they are plain chips. */}
        <span className="chip">{ticketTypeLabel(ticket.type)}</span>
        <span className="chip">{queueName(ticket.queue_id)}</span>
      </div>
      {(assign.isError || resolve.isError) && (
        <p role="alert" className="notice red" style={{ marginBottom: 12 }}>
          {describeSubmitError(assign.error ?? resolve.error)}
        </p>
      )}
      <div style={{ marginBottom: 16 }}>
        <Facts
          items={[
            {
              k: t("serviceDesk.common.assignedTo"),
              v: ticket.assigned_to ? (
                <Who id={ticket.assigned_to} name={assignee} />
              ) : (
                <span className="cell-sub">{t("serviceDesk.common.nobodyYet")}</span>
              ),
            },
            {
              k: t("serviceDesk.common.requester"),
              v: ticket.requester_id ? (
                <RequesterName userId={ticket.requester_id} name={requester} />
              ) : (
                <NoValue
                  label={
                    ticket.origin === "alert"
                      ? t("serviceDesk.detail.nobodyRaised")
                      : t("serviceDesk.detail.noRequesterRecorded")
                  }
                />
              ),
            },
            {
              k: t("serviceDesk.common.device"),
              v: ticket.device_id ? (
                <DeviceLink deviceId={ticket.device_id} onRemote={() => setRemoteOpen(true)} />
              ) : (
                <NoValue label={t("serviceDesk.detail.noDeviceLinked")} />
              ),
            },
            {
              k: t("serviceDesk.common.firstResponse"),
              v: sla ? (
                clock(sla.response_state, sla.response_due_at)
              ) : (
                <span className="cell-sub">{t("serviceDesk.common.noSla")}</span>
              ),
            },
            {
              k: t("serviceDesk.common.resolution"),
              v: sla ? (
                clock(sla.resolution_state, sla.resolution_due_at)
              ) : (
                <span className="cell-sub">{t("serviceDesk.common.noSla")}</span>
              ),
            },
          ]}
        />
      </div>
      {ticket.device_id ? (
        <StartSessionDialog
          open={remoteOpen}
          onClose={() => setRemoteOpen(false)}
          initialDeviceId={ticket.device_id}
          initialReason={`Ticket ${ticket.number}: ${ticket.subject}`.slice(0, 500)}
        />
      ) : null}
    </>
  );
}

/** The device's hostname (it needs `devices.read`; the id stands in without it) and Remote in. */
function DeviceLink({ deviceId, onRemote }: { deviceId: string; onRemote: () => void }) {
  const t = useT();
  const api = useApi();
  const inventory = useQuery<DeviceInventory, ApiError>({
    queryKey: ["service-desk-device-inventory", deviceId],
    queryFn: () => api.getDeviceInventory(deviceId),
    retry: false,
  });
  const hostname = inventory.data?.summary.hostname;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
      <Link to={`/devices/${deviceId}`} title={deviceId}>
        {typeof hostname === "string" ? hostname : deviceId}
      </Link>
      <Button type="button" variant="ghost" onClick={onRemote}>
        {t("serviceDesk.detail.remoteIn")}
      </Button>
    </span>
  );
}

// ---------------------------------------------------------------------------
// Conversation and reply box
// ---------------------------------------------------------------------------

function ConversationPanel({
  ticket,
  requester,
  userOptions,
  canWrite,
  canReadDocs,
  isTerminal,
  zone,
  visibility,
  onVisibilityChange,
  replyBox,
}: {
  ticket: TicketOut;
  requester: string | null;
  userOptions: readonly PickerOption[];
  canWrite: boolean;
  canReadDocs: boolean;
  isTerminal: boolean;
  zone?: string | null;
  visibility: CommentVisibility;
  onVisibilityChange: (visibility: CommentVisibility) => void;
  replyBox: React.RefObject<HTMLTextAreaElement | null>;
}) {
  const t = useT();
  const [includeInternal, setIncludeInternal] = useState(true);
  const comments = useTicketComments(ticket.id, includeInternal);

  // A comment with no author id was written by the system (or, for a type the
  // console has not met, a word read through the shared status vocabulary).
  const authorTypeLabel = (authorType: string) =>
    authorType === "user" || authorType === "system"
      ? humaniseEnum(authorType, "CommentAuthorType")
      : humaniseStatus(authorType);
  const authorName = (authorId: string | null, authorType: string) =>
    nameForOption(userOptions, authorId) ?? authorTypeLabel(authorType);

  return (
    <Panel
      title={t("serviceDesk.detail.conversation")}
      actions={
        <label className="note flex items-center gap-2">
          <input
            type="checkbox"
            checked={includeInternal}
            onChange={(e) => setIncludeInternal(e.target.checked)}
          />
          {t("serviceDesk.detail.showInternal")}
        </label>
      }
    >
      <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1fr)", gap: 14 }}>
        {ticket.description ? (
          <div>
            <div className="flex flex-wrap items-center gap-2">
              {ticket.requester_id && requester ? (
                <Who id={ticket.requester_id} name={requester} />
              ) : (
                <span className="cell-title">
                  {ticket.origin === "alert" ? t("serviceDesk.detail.alert") : t("serviceDesk.common.requester")}
                </span>
              )}
              <span className="note">
                {t("serviceDesk.detail.openedAt", { when: formatDateTime(ticket.created_at, zone) })}
              </span>
            </div>
            <p style={{ marginTop: 6, whiteSpace: "pre-wrap" }}>{ticket.description}</p>
          </div>
        ) : null}
        {comments.data?.map((c) => (
          <div key={c.id}>
            <div className="flex flex-wrap items-center gap-2">
              {c.author_id ? (
                <Who id={c.author_id} name={authorName(c.author_id, c.author_type)} />
              ) : (
                <span className="cell-title">{authorTypeLabel(c.author_type)}</span>
              )}
              <span className="note">{formatDateTime(c.created_at, zone)}</span>
              <Badge variant={commentVisibilityVariant(c.visibility)}>
                {c.visibility === "internal" ? t("serviceDesk.common.internalNote") : t("serviceDesk.detail.public")}
              </Badge>
            </div>
            <p style={{ marginTop: 6, whiteSpace: "pre-wrap" }}>{c.body}</p>
          </div>
        ))}
        {comments.data && comments.data.length === 0 && <p className="note">{t("serviceDesk.detail.noReplies")}</p>}
      </div>

      {canWrite && !isTerminal && (
        <ReplyBox
          ticket={ticket}
          canReadDocs={canReadDocs}
          visibility={visibility}
          onVisibilityChange={onVisibilityChange}
          replyBox={replyBox}
        />
      )}
      {canWrite && isTerminal && (
        <p className="note" style={{ marginTop: 14 }}>
          {t("serviceDesk.detail.closedNoReplies")}
        </p>
      )}
    </Panel>
  );
}

const VISIBILITY_CHOICES: ReadonlyArray<{ value: CommentVisibility; label: MessageKey; hint: MessageKey }> = [
  { value: "public", label: "serviceDesk.detail.publicReply", hint: "serviceDesk.detail.publicHint" },
  { value: "internal", label: "serviceDesk.common.internalNote", hint: "serviceDesk.detail.internalHint" },
];

function ReplyBox({
  ticket,
  canReadDocs,
  visibility,
  onVisibilityChange,
  replyBox,
}: {
  ticket: TicketOut;
  canReadDocs: boolean;
  visibility: CommentVisibility;
  onVisibilityChange: (visibility: CommentVisibility) => void;
  replyBox: React.RefObject<HTMLTextAreaElement | null>;
}) {
  const t = useT();
  const createComment = useCreateTicketCommentMutation();
  const resolve = useResolveTicketMutation();
  const canned = useCannedResponses();
  const suggestions = useArticleSuggestions(ticket.subject, canReadDocs);
  const [body, setBody] = useState("");
  const [cannedPick, setCannedPick] = useState("");
  const resolvable = canResolve(ticket.type, ticket.status);
  const pending = createComment.isPending || resolve.isPending;

  // Reply & resolve is one server call: if it fails, nothing was sent, so
  // the message stays in the box to try again.
  async function send(thenResolve: boolean) {
    const reply = { body, visibility };
    try {
      if (thenResolve) {
        createComment.reset();
        await resolve.mutateAsync({ id: ticket.id, reply });
      } else {
        resolve.reset();
        await createComment.mutateAsync({ id: ticket.id, body: reply });
      }
    } catch {
      return;
    }
    setBody("");
  }

  function insertCanned(id: string) {
    const chosen = canned.data?.find((c) => c.id === id);
    if (!chosen) return;
    setBody((current) => (current.trim() === "" ? chosen.body : `${current.trimEnd()}\n\n${chosen.body}`));
    setCannedPick("");
    replyBox.current?.focus();
  }

  const noRequester = !ticket.requester_id;
  const sendLabel = visibility === "internal" ? t("serviceDesk.detail.addNote") : t("serviceDesk.detail.sendReply");

  return (
    <div style={{ marginTop: 16, borderTop: "1px solid var(--line)", paddingTop: 14 }}>
      <fieldset style={{ border: 0, padding: 0, margin: "0 0 10px" }}>
        <legend className="cell-title" style={{ marginBottom: 6 }}>
          {t("serviceDesk.detail.whoSees")}
        </legend>
        <div className="flex flex-wrap gap-4">
          {VISIBILITY_CHOICES.map((choice) => (
            <label key={choice.value} className="flex items-center gap-2">
              <input
                type="radio"
                name={`visibility-${ticket.id}`}
                value={choice.value}
                checked={visibility === choice.value}
                onChange={() => onVisibilityChange(choice.value)}
              />
              <span>
                <b>{t(choice.label)}</b> <span className="note">{t(choice.hint)}</span>
              </span>
            </label>
          ))}
        </div>
        {noRequester && (
          <p className="note" style={{ marginTop: 6 }}>
            {t("serviceDesk.detail.noRequester")}
          </p>
        )}
      </fieldset>

      {canned.data && canned.data.length > 0 && (
        <Field label={t("serviceDesk.detail.insertSaved")}>
          <select
            className={inputClass}
            value={cannedPick}
            onChange={(e) => insertCanned(e.target.value)}
          >
            <option value="">{t("serviceDesk.detail.chooseSaved")}</option>
            {canned.data.map((c) => (
              <option key={c.id} value={c.id}>
                {c.title}
              </option>
            ))}
          </select>
        </Field>
      )}

      <Field label={t("serviceDesk.detail.message")}>
        <textarea
          ref={replyBox}
          className={inputClass}
          rows={5}
          placeholder={
            visibility === "internal"
              ? t("serviceDesk.detail.placeholderNote")
              : t("serviceDesk.detail.placeholderReply")
          }
          value={body}
          onChange={(e) => setBody(e.target.value)}
        />
      </Field>

      {suggestions.data && suggestions.data.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          <div className="cell-title" style={{ marginBottom: 4 }}>
            {t("serviceDesk.detail.articlesHelp")}
          </div>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {suggestions.data.map((article) => (
              <li key={article.id}>
                <Link to={`/docs?article=${article.id}`} target="_blank" rel="noreferrer">
                  {article.title}
                </Link>{" "}
                <span className="note">{article.category}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {(createComment.isError || resolve.isError) && (
        <p role="alert" className="notice red" style={{ marginBottom: 12 }}>
          {resolve.isError
            ? t("serviceDesk.detail.notSent", { reason: describeSubmitError(resolve.error) })
            : describeSubmitError(createComment.error)}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <Button onClick={() => void send(false)} disabled={pending || !body.trim()}>
          {createComment.isPending ? t("serviceDesk.detail.sending") : sendLabel}
        </Button>
        {resolvable && (
          <Button variant="outline" onClick={() => void send(true)} disabled={pending || !body.trim()}>
            {resolve.isPending
              ? t("serviceDesk.detail.resolving")
              : visibility === "internal"
                ? t("serviceDesk.detail.addNoteResolve")
                : t("serviceDesk.detail.replyResolve")}
          </Button>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// The ticket's client, and moving it to another (MSP clients spec §2.4)
// ---------------------------------------------------------------------------

// The server decides what may move (`ticket.write` in both clients; not a
// ticket with a device, nor one an alert or a change opened) and says why in
// a sentence, which is shown as it is. This only offers the clients a ticket
// could go to: never its own, never "Sin asignar", the inbox of what has no
// owner (plan 2026-10-26, C10), and never an archived one, which takes
// nothing new (the server's 409).
function ClientPanel({ ticket }: { ticket: TicketOut }) {
  const t = useT();
  const clients = useClients(true);
  const move = useMoveTicketMutation();
  const [destination, setDestination] = useState("");

  const current = clients.data?.find((client) => client.id === ticket.client_id);
  const destinations = (clients.data ?? []).filter(
    (client) =>
      client.id !== ticket.client_id && client.kind !== "unassigned" && client.status === "active",
  );

  return (
    <Panel title={t("serviceDesk.detail.clientTitle")}>
      <p className="note" style={{ marginBottom: 12 }}>
        {t("serviceDesk.detail.clientBelongs", { client: current?.name ?? ticket.client_id })}
      </p>
      {clients.isError ? (
        <p className="note">{describeSubmitError(clients.error)}</p>
      ) : (
        <>
          <Field label={t("serviceDesk.detail.moveToClient")}>
            <select
              className={inputClass}
              value={destination}
              disabled={clients.isLoading || move.isPending}
              onChange={(e) => setDestination(e.target.value)}
            >
              <option value="">{t("serviceDesk.detail.chooseClient")}</option>
              {destinations.map((client) => (
                <option key={client.id} value={client.id}>
                  {client.name}
                </option>
              ))}
            </select>
          </Field>
          {move.isError && (
            <p role="alert" className="notice red" style={{ marginBottom: 12 }}>
              {describeSubmitError(move.error)}
            </p>
          )}
          <Button
            onClick={() => move.mutate({ id: ticket.id, clientId: destination })}
            disabled={destination === "" || move.isPending}
          >
            {move.isPending ? t("serviceDesk.detail.moving") : t("serviceDesk.detail.moveTicket")}
          </Button>
        </>
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// One edit form: fields, status, queue and assignee, one Save
// ---------------------------------------------------------------------------

const PRIORITIES: readonly TicketPriority[] = ["low", "normal", "high", "urgent"];

function EditTicketPanel({
  ticket,
  canWrite,
  canAssign,
}: {
  ticket: TicketOut;
  canWrite: boolean;
  canAssign: boolean;
}) {
  const t = useT();
  const update = useUpdateTicketMutation();
  const assign = useAssignTicketMutation();
  const changeStatus = useChangeTicketStatusMutation();
  const categories = useTicketCategories();
  const queues = useTicketQueues();
  const devices = useDirectoryDevices();
  const users = useDirectoryUsers();
  // "Nobody" and "No device" are choices too: picking them clears the field.
  // Not memoised: every label here is worded, and a memo would keep it in the
  // language the page opened in.
  const deviceOptions = [
    { id: "", label: t("serviceDesk.detail.noDevice") },
    ...devicePickerOptions(devices.data ?? []),
  ];
  const userOptions = [
    { id: "", label: t("serviceDesk.detail.nobody"), sublabel: t("serviceDesk.detail.nobodyHint") },
    ...userPickerOptions(users.data ?? []),
  ];

  const [categoryId, setCategoryId] = useState(ticket.category_id);
  const [priority, setPriority] = useState(ticket.priority);
  const [subject, setSubject] = useState(ticket.subject);
  const [description, setDescription] = useState(ticket.description);
  const [deviceId, setDeviceId] = useState(ticket.device_id ?? "");
  const [status, setStatus] = useState(ticket.status);
  const [reason, setReason] = useState("");
  const [queueId, setQueueId] = useState(ticket.queue_id);
  const [assigneeId, setAssigneeId] = useState(ticket.assigned_to ?? "");
  const [confirming, setConfirming] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<unknown>(null);

  // A closed ticket's fields are frozen; its status can still reopen it.
  const frozen = TERMINAL_STATUSES.has(ticket.status);

  const fieldChanges: TicketUpdate = {};
  if (categoryId !== ticket.category_id) fieldChanges.category_id = categoryId;
  if (priority !== ticket.priority) fieldChanges.priority = priority as TicketPriority;
  if (subject !== ticket.subject) fieldChanges.subject = subject;
  if (description !== ticket.description) fieldChanges.description = description;
  if ((deviceId || null) !== ticket.device_id) fieldChanges.device_id = deviceId || null;
  const assignChanges: TicketAssign = {};
  if (queueId !== ticket.queue_id) assignChanges.queue_id = queueId;
  if ((assigneeId || null) !== ticket.assigned_to) assignChanges.assigned_to = assigneeId || null;
  const statusChanged = status !== ticket.status;
  const dirty =
    Object.keys(fieldChanges).length > 0 || Object.keys(assignChanges).length > 0 || statusChanged;

  async function save() {
    setConfirming(false);
    setSaving(true);
    setError(null);
    const saveFields = async () => {
      if (Object.keys(fieldChanges).length > 0) {
        await update.mutateAsync({ id: ticket.id, body: fieldChanges });
      }
    };
    const saveAssignment = async () => {
      if (Object.keys(assignChanges).length > 0) {
        await assign.mutateAsync({ id: ticket.id, body: assignChanges });
      }
    };
    const saveStatus = async () => {
      if (statusChanged) {
        await changeStatus.mutateAsync({ id: ticket.id, body: { to_status: status, reason } });
      }
    };
    try {
      // Reopen first so the fields thaw; close last so they are saved first.
      if (frozen && statusChanged) {
        await saveStatus();
        await saveAssignment();
        await saveFields();
      } else {
        await saveFields();
        await saveAssignment();
        await saveStatus();
      }
    } catch (caught) {
      setError(caught);
    } finally {
      setSaving(false);
    }
  }

  function submit() {
    if (statusChanged && statusChangeWarning(status)) {
      setConfirming(true);
      return;
    }
    void save();
  }

  const warning = confirming ? statusChangeWarning(status) : null;

  return (
    <Panel title={t("serviceDesk.detail.editTitle")}>
      {frozen && (
        <p className="note" style={{ marginBottom: 12 }}>
          {FINAL_STATUSES.has(ticket.status)
            ? t("serviceDesk.detail.finalFrozen", { status: ticketStatusLabel(ticket.status) })
            : t("serviceDesk.detail.closedFrozen")}
        </p>
      )}
      {canWrite && (
        <>
          <Field label={t("serviceDesk.common.status")}>
            <select className={inputClass} value={status} onChange={(e) => setStatus(e.target.value)}>
              {statusesForType(ticket.type).map((s) => (
                <option key={s} value={s}>
                  {ticketStatusLabel(s)}
                </option>
              ))}
            </select>
          </Field>
          {statusChanged && (
            <Field label={t("serviceDesk.detail.whyStatus")}>
              <input className={inputClass} value={reason} onChange={(e) => setReason(e.target.value)} />
            </Field>
          )}
        </>
      )}
      {canAssign && (
        <>
          <EntityPicker
            label={t("serviceDesk.common.assignedTo")}
            options={userOptions}
            loading={users.isLoading}
            emptyHint={userPickerEmptyHint()}
            placeholder={t("serviceDesk.common.nobodyYet")}
            value={assigneeId}
            onChange={setAssigneeId}
          />
          <Field label={t("serviceDesk.common.queue")}>
            <select className={inputClass} value={queueId} onChange={(e) => setQueueId(e.target.value)}>
              {queues.data?.map((q) => (
                <option key={q.id} value={q.id}>
                  {q.name}
                </option>
              ))}
            </select>
          </Field>
        </>
      )}
      {canWrite && (
        <>
          <Field label={t("serviceDesk.common.priority")}>
            <select
              className={inputClass}
              value={priority}
              disabled={frozen}
              onChange={(e) => setPriority(e.target.value)}
            >
              {PRIORITIES.map((p) => (
                <option key={p} value={p}>
                  {ticketPriorityLabel(p)}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("serviceDesk.common.category")}>
            <select
              className={inputClass}
              value={categoryId}
              disabled={frozen}
              onChange={(e) => setCategoryId(e.target.value)}
            >
              {categories.data
                ?.filter((c) => c.type === ticket.type)
                .map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
            </select>
          </Field>
          <Field label={t("serviceDesk.common.subject")}>
            <input
              className={inputClass}
              value={subject}
              disabled={frozen}
              onChange={(e) => setSubject(e.target.value)}
            />
          </Field>
          <Field label={t("serviceDesk.common.description")}>
            <textarea
              className={inputClass}
              rows={5}
              value={description}
              disabled={frozen}
              onChange={(e) => setDescription(e.target.value)}
            />
          </Field>
          <EntityPicker
            label={t("serviceDesk.common.device")}
            options={deviceOptions}
            loading={devices.isLoading}
            emptyHint={devicePickerEmptyHint()}
            placeholder={t("serviceDesk.common.searchHostnames")}
            value={deviceId}
            onChange={setDeviceId}
            disabled={frozen}
          />
        </>
      )}
      {error != null && (
        <p role="alert" className="notice red" style={{ marginBottom: 12 }}>
          {describeSubmitError(error)}
        </p>
      )}
      <Button onClick={submit} disabled={saving || !dirty || !subject.trim()}>
        {saving ? t("serviceDesk.common.saving") : t("serviceDesk.detail.saveChanges")}
      </Button>

      <ConfirmDialog
        open={warning != null}
        title={warning?.title ?? ""}
        body={warning?.body ?? ""}
        subject={`${ticket.number}: ${ticket.subject}`}
        confirmLabel={
          warning?.destructive
            ? t("serviceDesk.detail.moveTo", { status: finalStatusWord(status) })
            : t("serviceDesk.detail.closeTicket")
        }
        destructive={warning?.destructive ?? false}
        onConfirm={() => void save()}
        onCancel={() => setConfirming(false)}
      />
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Approval (requests awaiting a decision). The server checks the approver;
// hiding the controls without `ticket.approve` is presentation only.
// ---------------------------------------------------------------------------

function ApprovalPanel({ ticketId, canApprove }: { ticketId: string; canApprove: boolean }) {
  const t = useT();
  const approve = useApproveTicketMutation();
  const reject = useRejectTicketMutation();
  const [reason, setReason] = useState("");

  if (!canApprove) return null;

  return (
    <Panel title={t("serviceDesk.detail.approval")}>
      <Field label={t("serviceDesk.detail.approvalReason")}>
        <input className={inputClass} value={reason} onChange={(e) => setReason(e.target.value)} />
      </Field>
      <div className="flex gap-2">
        <Button
          variant="outline"
          disabled={approve.isPending}
          onClick={() => approve.mutate({ id: ticketId, body: { reason } })}
        >
          {t("serviceDesk.detail.approve")}
        </Button>
        <Button
          variant="outline"
          disabled={reject.isPending || !reason.trim()}
          onClick={() => reject.mutate({ id: ticketId, body: { reason } })}
        >
          {t("serviceDesk.detail.reject")}
        </Button>
      </div>
      {(approve.isError || reject.isError) && (
        <p role="alert" className="notice red" style={{ marginTop: 12 }}>
          {describeSubmitError(approve.error ?? reject.error)}
        </p>
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Linked device (spec §7.3). Both reads carry their own permissions
// (`devices.read`, `patch.read`), so a refusal leaves a quiet note rather than
// an error, and nothing is proxied through a service-desk route.
// ---------------------------------------------------------------------------

const PATCH_STATES: ReadonlySet<string> = new Set(["missing", "approved", "installed", "failed", "not_applicable"]);

/** A patch state as the device's Patch tab words it; one it does not know reads through the status words. */
const patchStateWord = (state: string): string =>
  PATCH_STATES.has(state) ? patchStateLabel(state) : humaniseStatus(state);

function DeviceSummaryPanel({ deviceId }: { deviceId: string }) {
  const t = useT();
  const api = useApi();

  const inventory = useQuery<DeviceInventory, ApiError>({
    queryKey: ["service-desk-device-inventory", deviceId],
    queryFn: () => api.getDeviceInventory(deviceId),
    retry: false,
  });
  const patchStatus = useQuery<DeviceStatus, ApiError>({
    queryKey: ["service-desk-device-patch-status", deviceId],
    queryFn: () => api.getDevicePatchStatus(deviceId),
    retry: false,
  });

  const inventoryForbidden = inventory.error instanceof ApiError && inventory.error.status === 403;
  const patchForbidden = patchStatus.error instanceof ApiError && patchStatus.error.status === 403;

  const summary = inventory.data?.summary;
  const osFamily = typeof summary?.os_family === "string" ? summary.os_family : null;
  const osVersion = typeof summary?.os_version === "string" ? summary.os_version : null;
  const os = osFamily ? [humaniseEnum(osFamily, "PatchFamily"), osVersion].filter(Boolean).join(" ") : null;

  const patchCounts = Object.entries(patchStatus.data?.counts_by_state ?? {});
  const patchItems: (readonly [ReactNode, ReactNode])[] = [
    [
      t("serviceDesk.detail.pendingReboot"),
      patchStatus.data?.pending_reboot ? t("serviceDesk.common.yes") : t("serviceDesk.common.no"),
    ],
    ...(patchCounts.length === 0
      ? [[t("serviceDesk.detail.patches"), <NoValue label={t("serviceDesk.detail.noPatchData")} />] as const]
      : patchCounts.map(([state, count]) => [patchStateWord(state), count] as const)),
  ];

  return (
    <Panel title={t("serviceDesk.detail.linkedDevice")}>
      <h3 className="cell-title" style={{ margin: "0 0 6px" }}>{t("serviceDesk.detail.inventorySummary")}</h3>
      {inventory.isSuccess && (
        <KV
          items={[
            [
              t("serviceDesk.detail.hostname"),
              String(summary?.hostname ?? "") || <NoValue label={t("serviceDesk.detail.notReported")} />,
            ],
            [t("serviceDesk.detail.os"), os ?? <NoValue label={t("serviceDesk.detail.notReported")} />],
            [t("serviceDesk.detail.hardware"), inventory.data.hardware.length],
            [t("serviceDesk.detail.software"), inventory.data.software.length],
          ]}
        />
      )}
      {inventoryForbidden && (
        <p className="note">{t("serviceDesk.detail.inventoryNoAccess")}</p>
      )}

      <h3 className="cell-title" style={{ margin: "14px 0 6px" }}>{t("serviceDesk.detail.patchState")}</h3>
      {patchStatus.isSuccess && <KV items={patchItems} />}
      {patchForbidden && (
        <p className="note">{t("serviceDesk.detail.patchNoAccess")}</p>
      )}
    </Panel>
  );
}

function AttachmentsPanel({
  ticketId,
  canWrite,
  isTerminal,
  zone,
}: {
  ticketId: string;
  canWrite: boolean;
  isTerminal: boolean;
  zone?: string | null;
}) {
  const t = useT();
  const api = useApi();
  const attachments = useTicketAttachments(ticketId);
  const upload = useUploadTicketAttachmentMutation();
  const fileInput = useRef<HTMLInputElement>(null);
  const [downloadError, setDownloadError] = useState<unknown>(null);

  async function download(attachmentId: string, filename: string) {
    setDownloadError(null);
    try {
      const blob = await api.downloadTicketAttachment(attachmentId);
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

  const rows = attachments.data ?? [];

  return (
    <Panel
      title={t("serviceDesk.detail.attachments")}
      bare
      actions={
        canWrite && !isTerminal ? (
          <>
            <input
              ref={fileInput}
              type="file"
              className="hidden"
              aria-label={t("serviceDesk.detail.attachFile")}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) upload.mutate({ id: ticketId, file });
              }}
            />
            <Btn label={t("serviceDesk.detail.attachFile")} icon="paperclip" onClick={() => fileInput.current?.click()} />
          </>
        ) : undefined
      }
    >
      {attachments.data && rows.length === 0 ? (
        <EmptyState
          icon="paperclip"
          title={t("serviceDesk.detail.noAttachments")}
          body={
            canWrite && !isTerminal
              ? t("serviceDesk.detail.attachmentsHelp")
              : t("serviceDesk.detail.attachmentsNone")
          }
        />
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>{t("serviceDesk.detail.filename")}</Th>
              <Th>{t("serviceDesk.detail.size")}</Th>
              <Th>{t("serviceDesk.detail.uploaded")}</Th>
              <Th>{""}</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((a) => (
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
                  <Btn
                    label={t("serviceDesk.detail.download")}
                    icon="download"
                    small
                    onClick={() => void download(a.id, a.filename)}
                  />
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      {canWrite && isTerminal && (
        <p className="note" style={{ padding: "12px 18px" }}>
          {t("serviceDesk.detail.closedNoAttachments")}
        </p>
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

/** Every status change, oldest first, as the server returns them. */
function StatusHistoryPanel({ ticketId, zone }: { ticketId: string; zone?: string | null }) {
  const t = useT();
  const history = useTicketStatusHistory(ticketId);
  const rows = history.data ?? [];

  return (
    <Panel title={t("serviceDesk.detail.history")} bare>
      {history.data && rows.length === 0 ? (
        <EmptyState
          icon="history"
          title={t("serviceDesk.detail.noHistory")}
          body={t("serviceDesk.detail.historyHelp")}
        />
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>{t("serviceDesk.detail.from")}</Th>
              <Th>{t("serviceDesk.detail.to")}</Th>
              <Th>{t("serviceDesk.detail.reason")}</Th>
              <Th>{t("serviceDesk.detail.changed")}</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((h) => (
              <tr key={h.id}>
                <Td lead={<Icon name="history" size={18} />}>
                  {h.from_status ? ticketStatusLabel(h.from_status) : <NoValue label={t("serviceDesk.detail.noPreviousStatus")} />}
                </Td>
                <Td>
                  <Badge variant={ticketStatusVariant(h.to_status)}>
                    {ticketStatusLabel(h.to_status)}
                  </Badge>
                </Td>
                <Td>{h.reason ? <span style={{ whiteSpace: "pre-wrap" }}>{h.reason}</span> : <NoValue label={t("serviceDesk.detail.noReason")} />}</Td>
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
