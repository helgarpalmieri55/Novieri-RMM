import { useQuery } from "@tanstack/react-query";
import { RowMenu } from "../../components/rowmenu";
import { useState } from "react";
import { ConfirmDialog } from "../../components/confirm";
import { describeSubmitError } from "../../components/form";
import { EntityPicker } from "../../components/picker";
import { Button, Dialog, Field, inputClass, Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { ApiError } from "../../lib/api";
import { Icon } from "../../shell/icons";
import { Btn, Panel, StatusPill, Toggle } from "../../shell/components";
import { useApi } from "../../lib/ApiContext";
import {
  useCreateNotificationRuleMutation,
  useDeleteNotificationRuleMutation,
  useNotificationChannels,
  useNotificationRules,
  useUpdateNotificationRuleMutation,
} from "./api";
import { eventTypeLabel, recipientKindLabel } from "./labels";
import { channelKindLabel } from "./notificationState";
import { EVENT_TYPES } from "./types";
import type { RecipientKind, RuleOut } from "./types";

const RECIPIENT_KINDS: readonly RecipientKind[] = [
  "queue_members",
  "assignee",
  "requester",
  "channel_default",
];

// `EVENT_TYPES[0]` types as `string | undefined` under this project's
// `noUncheckedIndexedAccess` (tsconfig.json) even though the array is a
// non-empty literal -- a named constant sidesteps that without an `any` or a
// non-null assertion.
const DEFAULT_EVENT_TYPE = "ticket.assigned";

// Spec §11: "Rules -- event type -> channel -> recipient kind, optional
// queue scope." `canAdmin` gates every write, presentationally -- the server
// is the actual authority (invariant #6).
export function RulesPanel({ canAdmin }: { canAdmin: boolean }) {
  const t = useT();
  const rules = useNotificationRules();
  const channels = useNotificationChannels();
  const create = useCreateNotificationRuleMutation();
  const update = useUpdateNotificationRuleMutation();
  const del = useDeleteNotificationRuleMutation();

  // Ticket queues are a CROSS-MODULE read (service-desk's `ticket.read`),
  // independent of this page's `notification.read`/`notification.admin`
  // gate -- a notification admin is not guaranteed `ticket.read` (mirrors
  // TicketDetailPage's DeviceSummaryPanel pulling `devices.read`/
  // `patch.read` from inside a `ticket.read`-gated page, the reverse
  // direction). Degrades quietly to a free-text queue-id field on a 403
  // rather than blocking rule creation on a permission this module does not
  // itself require.
  const api = useApi();
  const queues = useQuery({
    queryKey: ["notification-rules-ticket-queues"],
    queryFn: () => api.listTicketQueues(),
    retry: false,
  });
  const queuesForbidden = queues.error instanceof ApiError && queues.error.status === 403;

  const [creating, setCreating] = useState(false);
  const [eventType, setEventType] = useState<string>(DEFAULT_EVENT_TYPE);
  const [channelId, setChannelId] = useState("");
  const [recipientKind, setRecipientKind] = useState<RecipientKind>("queue_members");
  const [queueId, setQueueId] = useState("");
  // The rule awaiting confirmation. The ROW, not its id, so the dialog can
  // name what is being deleted rather than asking "are you sure?" about
  // nothing in particular (components/confirm.tsx).
  const [pendingDelete, setPendingDelete] = useState<RuleOut | null>(null);

  const forbidden = rules.error instanceof ApiError && rules.error.status === 403;
  const failed = rules.error != null && !forbidden;

  // Spec §6.4: internal notes cannot be routed to the requester -- rejected
  // at write time server-side, but hidden here rather than offered and
  // refused (the same convention ServiceDeskAdminPage's category form
  // follows for requires_approval/type="incident").
  const recipientOptions =
    eventType === "ticket.internal_note_added"
      ? RECIPIENT_KINDS.filter((k) => k !== "requester")
      : RECIPIENT_KINDS;

  function submit() {
    create.mutate(
      {
        event_type: eventType,
        channel_id: channelId,
        recipient_kind: recipientKind,
        queue_id: queueId || null,
      },
      {
        onSuccess: () => {
          setEventType(DEFAULT_EVENT_TYPE);
          setChannelId("");
          setRecipientKind("queue_members");
          setQueueId("");
          setCreating(false);
        },
      },
    );
  }

  function toggleActive(id: string, active: boolean) {
    update.mutate({ id, body: { active: !active } });
  }

  function channelName(id: string): string {
    return channels.data?.find((c) => c.id === id)?.name ?? id;
  }

  // Auditor A/B (cycle 7's blocked-flow class): the Channel control was a
  // <select> whose only entry on a fresh install is "Select a channel", with
  // Save disabled and nothing on screen saying why. A picker states what would
  // fill it. The SUBLABEL is the channel's kind and whether it is switched on:
  // routing a rule through a disabled channel is a rule that silently delivers
  // nothing, and that is exactly the fact a name alone hides.
  //
  // Not memoised: the sublabel is worded, and a memo would keep the language
  // the page was in.
  const channelOptions = (channels.data ?? []).map((c) => ({
    id: c.id,
    label: c.name,
    sublabel: t(c.active ? "notification.rules.channelActive" : "notification.rules.channelDisabled", {
      kind: channelKindLabel(c.kind),
    }),
  }));

  function queueName(id: string | null): string {
    if (!id) return t("notification.common.anyQueue");
    return queues.data?.find((q) => q.id === id)?.name ?? id;
  }

  const channelKind = (id: string): string => channels.data?.find((c) => c.id === id)?.kind ?? "webhook";

  return (
    // The Rules stat tile links to `/notifications#rules`.
    <div id="rules">
      <Panel
        title={t("notification.rules.title")}
        // Same sentence, verbatim, as Settings → Notifications
        // (`NotificationsTab.tsx`): "reaches" here and "goes" there was one
        // job described in two verbs. Picked "goes" — the glossary's own
        // replacement for "channel" is "where it goes", so the family of
        // words this panel already needs to speak in is the "goes" one.
        subtitle={t("notification.rules.subtitle")}
        bare
        actions={canAdmin ? <Btn label={t("notification.rules.add")} icon="plus" variant="primary" onClick={() => setCreating(true)} /> : undefined}
      >
        {rules.isLoading ? (
          <p className="note" style={{ padding: "16px 18px" }}>{t("notification.common.loading")}</p>
        ) : forbidden ? (
          <p className="note" style={{ padding: "16px 18px" }}>{t("notification.rules.forbidden")}</p>
        ) : failed ? (
          <p role="alert" className="notice red" style={{ margin: 14 }}>
            {t("notification.rules.failed", { reason: describeSubmitError(rules.error) })}
          </p>
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>{t("notification.rules.colRule")}</Th>
                <Th>{t("notification.rules.colQueueScope")}</Th>
                {/* v3 draws a rule's on/off state as a SWITCH in an "On"
                    column (`P['notifications']`), not as a pill in an ACTIVE
                    column with a separate "Disable" text link three cells to
                    the right. One control, in one place: it says what the
                    state is and it is how the state changes. */}
                <Th>{t("notification.rules.colOn")}</Th>
                {canAdmin && <Th>{""}</Th>}
              </tr>
            </thead>
            <tbody>
              {rules.data?.map((r) => (
                <tr key={r.id}>
                  {/* v3's rule row: the channel's glyph, the event as the title,
                      the channel and the recipient under it. Auditor A (LOW), the
                      enum display-label finding, at the two render sites this
                      table owns; `title` keeps the wire value reachable for anyone
                      correlating with the API. */}
                  <Td lead={<Icon name={channelKind(r.channel_id) === "email" ? "mail" : "link"} size={18} />}>
                    <div className="cell-title">
                      <span title={r.event_type}>{eventTypeLabel(r.event_type)}</span>
                    </div>
                    <div className="cell-sub">
                      <span>{channelName(r.channel_id)}</span>
                      {" · "}
                      <span title={r.recipient_kind}>{recipientKindLabel(r.recipient_kind)}</span>
                    </div>
                  </Td>
                  <Td>{queueName(r.queue_id)}</Td>
                  <Td>
                    {/* A reader without `notification.admin` still has to see
                        whether a rule is live -- so the state is a pill for
                        them rather than a switch nobody may throw. The pill
                        reads "Active"/"Inactive" through `StatusPill`, which
                        humanises; the column used to print the wire's own
                        lower-case "active" beside a DELIVERY column reading
                        "Pending" and "Sent". */}
                    {canAdmin ? (
                      <Toggle
                        checked={r.active}
                        onChange={() => toggleActive(r.id, r.active)}
                        label={r.active ? t("notification.rules.on") : t("notification.rules.off")}
                        disabled={update.isPending}
                      />
                    ) : (
                      <StatusPill value={r.active ? "active" : "inactive"} />
                    )}
                  </Td>
                  {canAdmin && (
                    <Td>
                      {/* Auditor B: "Delete sits directly beside Edit in the
                          same row with identical size and no destructive
                          colour, so a mis-click is one pixel of travel away
                          from irreversible data loss." Both halves fixed: the
                          styling, and the missing confirmation. It is a
                          `Btn small` like every other row action in the
                          console, and `danger` is the only thing separating
                          it from them. */}
                      <RowMenu
                        subject={t("notification.rules.menuSubject", { event: eventTypeLabel(r.event_type) })}
                        items={[
                          {
                            label: t("notification.rules.delete"),
                            destructive: true,
                            disabled: del.isPending,
                            onSelect: () => setPendingDelete(r),
                          },
                        ]}
                      />
                    </Td>
                  )}
                </tr>
              ))}
              {rules.data && rules.data.length === 0 && (
                <tr>
                  <Td colSpan={canAdmin ? 4 : 3}>
                    <span className="note">{t("notification.rules.empty")}</span>
                  </Td>
                </tr>
              )}
            </tbody>
          </Table>
        )}
        {/* Enable/Disable and Delete are the two writes with no dialog around
            them, so this line IS their only failure channel. `String(error)`
            rendered "Error: " for a bodyless 503 -- the row kept its old badge
            and the operator read a label with nothing after it, which is the
            silently-swallowed destructive call Auditor A filed as a BLOCKER. */}
        {(create.isError || update.isError || del.isError) && (
          <p role="alert" className="notice red" style={{ margin: 14 }}>
            {t("notification.rules.notChanged", { reason: describeSubmitError(create.error ?? update.error ?? del.error) })}
          </p>
        )}

        <ConfirmDialog
          open={pendingDelete !== null}
          title={t("notification.rules.deleteTitle")}
          subject={
            pendingDelete
              ? `${eventTypeLabel(pendingDelete.event_type)} → ${channelName(pendingDelete.channel_id)}`
              : ""
          }
          body={t("notification.rules.deleteBody")}
          confirmLabel={t("notification.rules.deleteConfirm")}
          destructive
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => {
            if (!pendingDelete) return;
            del.mutate(pendingDelete.id);
            setPendingDelete(null);
          }}
        />

        <Dialog open={creating} onClose={() => setCreating(false)} title={t("notification.rules.addTitle")}>
          <div className="form-grid">
            <Field label={t("notification.rules.eventType")} full>
              <select className={inputClass} value={eventType} onChange={(e) => setEventType(e.target.value)}>
                {EVENT_TYPES.map((type) => (
                  <option key={type} value={type}>
                    {eventTypeLabel(type)}
                  </option>
                ))}
              </select>
            </Field>
            {/* `emptyHint` NAMES WHAT WOULD FILL THIS AND WHERE (cycle 7). A
                fresh install has no channels, so this form was previously dead
                with no explanation: a disabled Save and a dropdown containing one
                placeholder. The Channels panel that fills it is on this same
                page, which is why the hint can point at it by name. */}
            <EntityPicker
              label={t("notification.rules.channel")}
              options={channelOptions}
              value={channelId}
              onChange={setChannelId}
              loading={channels.isLoading}
              emptyHint={t("notification.rules.channelEmpty")}
              placeholder={t("notification.rules.channelPlaceholder")}
            />
            <Field label={t("notification.rules.recipient")}>
              <select
                className={inputClass}
                value={recipientKind}
                onChange={(e) => setRecipientKind(e.target.value as RecipientKind)}
              >
                {recipientOptions.map((k) => (
                  <option key={k} value={k}>
                    {recipientKindLabel(k)}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={t("notification.rules.queueScope")} full>
              {queuesForbidden ? (
                <input
                  className={inputClass}
                  placeholder={t("notification.common.queueId")}
                  value={queueId}
                  onChange={(e) => setQueueId(e.target.value)}
                />
              ) : (
                <select className={inputClass} value={queueId} onChange={(e) => setQueueId(e.target.value)}>
                  <option value="">{t("notification.common.anyQueue")}</option>
                  {queues.data?.map((q) => (
                    <option key={q.id} value={q.id}>
                      {q.name}
                    </option>
                  ))}
                </select>
              )}
            </Field>
          </div>
          {create.isError && (
            <p role="alert" className="notice red" style={{ marginBottom: 12 }}>
              {describeSubmitError(create.error)}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setCreating(false)}>
              {t("notification.rules.cancel")}
            </Button>
            <Button onClick={submit} disabled={create.isPending || !channelId}>
              {create.isPending ? t("notification.common.saving") : t("notification.rules.save")}
            </Button>
          </div>
        </Dialog>
      </Panel>
    </div>
  );
}
