import { useState } from "react";
import { ConfirmDialog } from "../../components/confirm";
import { attemptProblems, describeSubmitError, Form, FormActions } from "../../components/form";
import type { Attempt, FieldRule, Problem } from "../../components/form";
import { EntityPicker } from "../../components/picker";
import type { PickerOption } from "../../components/picker";
import { Badge, Button, Dialog, Field, inputClass, Table, Td, Th } from "../../components/ui";
import { Btn, Panel, StatusPill } from "../../shell/components";
import { useT } from "../../i18n/context";
import { translate, type MessageKey } from "../../i18n/core";
import { ApiError } from "../../lib/api";
import { humaniseStatus } from "../../lib/labels";
import {
  useCreateSlaPolicyMutation,
  useDeleteSlaPolicyMutation,
  useSlaCalendars,
  useSlaPolicies,
  useSlaTicketQueues,
  useUpdateSlaPolicyMutation,
} from "./api";
import { ticketPriorityLabel, ticketPriorityVariant } from "../service-desk/serviceDeskState";
import { calendarHealthLabel, formatMinutes } from "./slaState";
import { SLA_PRIORITIES } from "./types";
import type { CalendarOut, PolicyOut, SlaPriority } from "./types";

// SLA policies (spec §2.4, §9.1). A policy is selected by `(queue,
// priority)`, and NULL in either column is a WILDCARD -- rendered here as the
// word "any", never as a blank cell, so a policy that deliberately matches
// everything stays distinguishable from an unset one.
//
// Deleting a policy any `ticket_sla` row still references is refused with a
// 400 naming the tickets; `active: false` is the retirement path, which is
// why the inactive state is a first-class column here rather than a hidden
// flag.
// A target in minutes, checked here rather than left to `min={1}`.
//
// THE BROWSER CONSTRAINT WAS NEVER ENFORCED. `min={1}` on a number input only
// bites during native validation, and the form kernel renders `<form
// noValidate>` -- as the old code had no <form> at all, it never bit either.
// Auditor: "number fields accept negative and zero SLA targets". A zero-minute
// target is breached the instant the ticket is created and a negative one asks
// for a deadline before it existed; both were accepted and sent.
//
// The label is baked into each message because `FieldRule.validate` sees only
// the value, and "must be a whole number" under a dialog with two number
// fields does not say which one.
function targetMinutesRule(name: string, label: string): FieldRule {
  return {
    name,
    label,
    validate: (value) => {
      if (/^-/.test(value)) return translate("sla.policies.targetNegative", { label });
      if (!/^\d+$/.test(value)) return translate("sla.policies.targetWhole", { label });
      if (Number(value) === 0) return translate("sla.policies.targetZero", { label });
      return null;
    },
  };
}

/** A priority as the policy form and its confirmation name it (the wire word in English). */
const PRIORITY_OPTION_KEYS: Readonly<Record<SlaPriority, MessageKey>> = {
  low: "sla.priorityOption.low",
  normal: "sla.priorityOption.normal",
  high: "sla.priorityOption.high",
  urgent: "sla.priorityOption.urgent",
};

function priorityOptionLabel(priority: string): string {
  return Object.hasOwn(PRIORITY_OPTION_KEYS, priority)
    ? translate(PRIORITY_OPTION_KEYS[priority as SlaPriority])
    : humaniseStatus(priority);
}

// Auditor, filed as a BLOCKER: "'Add policy' Save is a silent no-op when no
// calendar is selected". `disabled={create.isPending || !targetsValid}` folded
// three separate faults -- no calendar, a bad response target, a bad
// resolution target -- into one dead button that named none of them.
//
// A function so the words are looked up when the refusal is rendered.
const policyRules = (): readonly FieldRule[] => [
  // Every policy needs one: the calendar is what turns minutes into an
  // instant, so there is no deadline at all without it.
  { name: "calendarId", label: translate("sla.common.calendar") },
  targetMinutesRule("responseMinutes", translate("sla.policies.responseTarget")),
  targetMinutesRule("resolutionMinutes", translate("sla.policies.resolutionTarget")),
];

// The label used to read `formatMinutes(Number(draft.responseMinutes))`
// unconditionally, so "-5" came back as "(-5m)" and an empty field as "(—)":
// the interface agreeing that nonsense was a duration. Only a value that could
// actually be saved gets translated.
function describeTarget(value: string): string {
  if (!/^\d+$/.test(value) || Number(value) < 1) return "";
  return ` (${formatMinutes(Number(value))})`;
}

export function PoliciesPanel({
  canAdmin,
  queueName,
  onCreateCalendar,
}: {
  canAdmin: boolean;
  queueName: (id: string | null) => string;
  /**
   * Open the Business-calendars create dialog (UX remediation cycle 7).
   *
   * A policy cannot be saved without a calendar and a fresh install ships with
   * none, so this form is dead on arrival for a new tenant -- the audit's
   * BLOCKER. The picker's `emptyHint` says WHAT would fill the list; this prop
   * is the other half, the way to go and do it without hunting down the panel
   * that owns it. Optional because the panel must still render for a caller
   * that has no calendar form to offer (a read-only embed).
   */
  onCreateCalendar?: () => void;
}) {
  const t = useT();
  const policies = useSlaPolicies();
  const calendars = useSlaCalendars();
  const create = useCreateSlaPolicyMutation();
  const update = useUpdateSlaPolicyMutation();
  const del = useDeleteSlaPolicyMutation();

  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<PolicyOut | null>(null);
  // The policy a Delete click has named but not yet destroyed (cycle 8).
  // Auditor B, HIGH: "Delete sits directly beside Edit in the same row with
  // identical size and no destructive colour, so a mis-click is one pixel of
  // travel away from irreversible data loss" -- on a record that encodes a
  // contractual promise.
  const [pendingDelete, setPendingDelete] = useState<PolicyOut | null>(null);
  const [draft, setDraft] = useState<PolicyDraft>(emptyDraft());
  // On submit only -- see CalendarsPanel for why not while typing. Held as the
  // rules and the values they were checked against, worded at render.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  const forbidden = policies.error instanceof ApiError && policies.error.status === 403;
  const failed = policies.error != null && !forbidden;

  function calendarName(id: string): string {
    return calendars.data?.find((calendar) => calendar.id === id)?.name ?? id;
  }

  // `reset()` because mutation state outlives the dialog: a failed Save would
  // otherwise have `FormActions` announce that refusal again the next time the
  // dialog is opened, against a request nobody has made yet.
  function openCreate() {
    setDraft(emptyDraft());
    setAttempt(null);
    create.reset();
    setCreating(true);
  }

  function openEdit(policy: PolicyOut) {
    setAttempt(null);
    update.reset();
    setDraft({
      queueId: policy.queue_id ?? "",
      priority: policy.priority ?? "",
      calendarId: policy.calendar_id,
      responseMinutes: String(policy.response_target_minutes),
      resolutionMinutes: String(policy.resolution_target_minutes),
      escalatePriority: policy.escalate_priority,
      escalateToQueueId: policy.escalate_to_queue_id ?? "",
      active: policy.active,
    });
    setEditing(policy);
  }

  // "" in the two wildcard-capable selects means NULL -- "applies to any" --
  // and is sent as an explicit null, which `policies.update_policy` reads as
  // a real edit rather than an omission.
  function body() {
    return {
      queue_id: draft.queueId === "" ? null : draft.queueId,
      priority: draft.priority === "" ? null : (draft.priority as SlaPriority),
      calendar_id: draft.calendarId,
      response_target_minutes: Number(draft.responseMinutes),
      resolution_target_minutes: Number(draft.resolutionMinutes),
      escalate_priority: draft.escalatePriority,
      escalate_to_queue_id: draft.escalateToQueueId === "" ? null : draft.escalateToQueueId,
      active: draft.active,
    };
  }

  // Returns the problems so each caller can stop on a non-empty list; the
  // rules are shared because create and edit refuse for the same reasons.
  function check(): readonly Problem[] {
    const next: Attempt = {
      rules: policyRules,
      values: {
        calendarId: draft.calendarId,
        responseMinutes: draft.responseMinutes,
        resolutionMinutes: draft.resolutionMinutes,
      },
    };
    setAttempt(next);
    return attemptProblems(next);
  }

  function submitCreate() {
    if (check().length > 0) return;
    create.mutate(body(), { onSuccess: () => setCreating(false) });
  }

  function submitEdit() {
    if (!editing) return;
    if (check().length > 0) return;
    update.mutate({ id: editing.id, body: body() }, { onSuccess: () => setEditing(null) });
  }

  return (
    <Panel
      title={t("sla.policies.title")}
      subtitle={t("sla.policies.subtitle")}
      bare
      actions={
        canAdmin ? <Btn label={t("sla.policies.add")} icon="plus" variant="primary" onClick={openCreate} /> : undefined
      }
    >
      {policies.isLoading ? (
        <p className="note" style={{ padding: "16px 18px" }}>{t("sla.common.loading")}</p>
      ) : forbidden ? (
        <p className="note" style={{ padding: "16px 18px" }}>{t("sla.policies.forbidden")}</p>
      ) : failed ? (
        <p role="alert" className="notice red" style={{ margin: 14 }}>
          {t("sla.policies.failed", { reason: describeSubmitError(policies.error) })}
        </p>
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>{t("sla.common.priority")}</Th>
              <Th>{t("sla.common.calendar")}</Th>
              <Th>{t("sla.common.response")}</Th>
              <Th>{t("sla.common.resolution")}</Th>
              <Th>{t("sla.policies.colOnBreach")}</Th>
              <Th>{t("sla.policies.colActive")}</Th>
              {canAdmin && <Th>{""}</Th>}
            </tr>
          </thead>
          <tbody>
            {policies.data?.map((policy) => (
              <tr key={policy.id}>
                {/* v3 leads each policy row with its priority as a kind pill
                    (P1 crit, P2 warn, P3 mut); the queue it is scoped to sits
                    beside it. NULL in either is a WILDCARD and
                    is rendered as the word "any", never as a blank. */}
                <Td
                  lead={
                    <Badge variant={ticketPriorityVariant(policy.priority ?? "normal")}>
                      {policy.priority === null ? t("sla.common.any") : ticketPriorityLabel(policy.priority)}
                    </Badge>
                  }
                >
                  {/* THE PILL ALREADY SAID IT. The title beside it read
                      "Urgent priority" next to a pill reading "Urgent", and
                      the echo repeated on all six rows. A policy's identity
                      is the (priority, queue) PAIR -- the pill carries the
                      priority, so the line beside it carries the other half
                      rather than saying the same word twice. */}
                  <div className="cell-title">
                    {policy.queue_id === null ? t("sla.common.anyQueue") : queueName(policy.queue_id)}
                  </div>
                </Td>
                <Td>{calendarName(policy.calendar_id)}</Td>
                <Td>
                  <span className="mono">{formatMinutes(policy.response_target_minutes)}</span>
                </Td>
                <Td>
                  <span className="mono">{formatMinutes(policy.resolution_target_minutes)}</span>
                </Td>
                <Td>
                  <span className="cell-sub">{describeEscalation(policy, queueName)}</span>
                </Td>
                <Td>
                  {/* `StatusPill`, so the state is humanised by the one table
                      that does it: this column printed the wire's "active" and
                      "inactive" in lower case beside a HEALTH column doing the
                      same, in a console whose every other pill is sentence
                      case. */}
                  <StatusPill value={policy.active ? "active" : "inactive"} />
                </Td>
                {canAdmin && (
                  <Td>
                    {/* ONE WEIGHT FOR THE WHOLE CLUSTER. Edit was a borderless
                        ghost and Delete a bordered danger, so the row's least
                        reversible action was the only one that looked like a
                        control. Both are `Btn small` now -- the same box the
                        rest of the console's row actions use -- and Delete
                        differs only in the ink and edge its `danger` variant
                        paints. The `.btn` box is also what the coarse-pointer
                        rule raises to 44px; a borderless text link is not. */}
                    <div className="flex gap-2">
                      <Btn
                        small
                        label={t("sla.common.edit")}
                        onClick={() => openEdit(policy)}
                      />
                      <Btn
                        small
                        variant="danger"
                        label={t("sla.common.delete")}
                        onClick={() => {
                          // A refusal left over from a previous row would
                          // otherwise sit under this dialog reading as a
                          // refusal of the delete being asked about now.
                          del.reset();
                          setPendingDelete(policy);
                        }}
                      />
                    </div>
                  </Td>
                )}
              </tr>
            ))}
            {policies.data && policies.data.length === 0 && (
              <tr>
                <Td colSpan={canAdmin ? 7 : 6}>
                  <span className="note">
                    {t("sla.policies.empty")}
                  </span>
                </Td>
              </tr>
            )}
          </tbody>
        </Table>
      )}

      {/* Delete is a row action, not a form, so it has nowhere else to speak.
          Create and update were also funnelled through here and are not any
          more: `FormActions` renders their refusal inside the dialog the user
          is looking at, and repeating it on the panel behind the modal was one
          message in two places, only one of them visible. */}
      {del.isError && (
        <p role="alert" className="notice red" style={{ margin: 14 }}>
          {/* `describeSubmitError`, not the module-local `describeError`: a
              policy delete refused with a bodyless 503 announced an alert
              containing only the words "Delete failed:" and no reason, and a
              policy delete refused with FastAPI's `{"detail": [...]}` printed
              the raw payload. Both are the same call site. */}
          {t("sla.common.deleteFailed", { reason: describeSubmitError(del.error) })}
        </p>
      )}

      {/* NOT `requireTyping`: one policy, restorable by re-entering four
          fields, and a typed name here would be ceremony that teaches people to
          copy words. The body carries the two things a name cannot -- which
          (queue, priority) pair stops being matched, and what happens to the
          tickets currently measured against it. */}
      <ConfirmDialog
        open={pendingDelete !== null}
        title={t("sla.policies.deleteTitle")}
        subject={pendingDelete ? describePolicySubject(pendingDelete, queueName) : ""}
        body={t("sla.policies.deleteBody")}
        confirmLabel={t("sla.policies.deleteConfirm")}
        destructive
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => {
          const policy = pendingDelete;
          setPendingDelete(null);
          if (policy) del.mutate(policy.id);
        }}
      />

      {/* A real <form>, so Enter submits from any field in the dialog. */}
      <Dialog open={creating} onClose={() => setCreating(false)} title={t("sla.policies.addTitle")}>
        <Form onSubmit={submitCreate}>
          <PolicyFields
            draft={draft}
            onChange={setDraft}
            calendars={calendars.data ?? []}
            calendarsLoading={calendars.isLoading}
            calendarsFailed={calendars.error != null}
            onCreateCalendar={
              canAdmin && onCreateCalendar
                ? () => {
                    setCreating(false);
                    onCreateCalendar();
                  }
                : undefined
            }
          />
          <div className="flex items-center justify-end gap-2">
            {/* `type="button"`: inside a <form>, an untyped button submits, so
                Cancel would save the policy it was meant to abandon. */}
            <Button type="button" variant="ghost" onClick={() => setCreating(false)}>
              {t("common.actions.cancel")}
            </Button>
            {/* Enabled with no calendar chosen ON PURPOSE -- the press is what
                produces "Calendar is required". A dead button was the BLOCKER. */}
            <FormActions
              label={t("common.actions.save")}
              pending={create.isPending}
              error={create.error}
              problems={attemptProblems(attempt)}
            />
          </div>
        </Form>
      </Dialog>

      <Dialog open={editing !== null} onClose={() => setEditing(null)} title={t("sla.policies.editTitle")}>
        <Form onSubmit={submitEdit}>
          <PolicyFields
            draft={draft}
            onChange={setDraft}
            calendars={calendars.data ?? []}
            calendarsLoading={calendars.isLoading}
            calendarsFailed={calendars.error != null}
          />
          <p className="form-hint" style={{ marginBottom: 8 }}>
            {t("sla.policies.editHint")}
          </p>
          <div className="flex items-center justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setEditing(null)}>
              {t("common.actions.cancel")}
            </Button>
            <FormActions
              label={t("common.actions.save")}
              pending={update.isPending}
              error={update.error}
              problems={attemptProblems(attempt)}
            />
          </div>
        </Form>
      </Dialog>
    </Panel>
  );
}

function describeEscalation(policy: PolicyOut, queueName: (id: string | null) => string): string {
  const parts: string[] = [];
  if (policy.escalate_priority) parts.push(translate("sla.policies.raisePriority"));
  if (policy.escalate_to_queue_id) {
    parts.push(translate("sla.policies.moveTo", { queue: queueName(policy.escalate_to_queue_id) }));
  }
  return parts.length > 0 ? parts.join(", ") : translate("sla.policies.notifyOnly");
}

interface PolicyDraft {
  queueId: string;
  priority: string;
  calendarId: string;
  responseMinutes: string;
  resolutionMinutes: string;
  escalatePriority: boolean;
  escalateToQueueId: string;
  active: boolean;
}

function emptyDraft(): PolicyDraft {
  return {
    queueId: "",
    priority: "",
    calendarId: "",
    responseMinutes: "60",
    resolutionMinutes: "480",
    escalatePriority: false,
    escalateToQueueId: "",
    active: true,
  };
}

/**
 * The queue pickers' options, with the wildcard AS AN OPTION.
 *
 * A blank queue means "any queue", and that is a real choice a policy makes --
 * not the absence of one. Left as an empty text box it was indistinguishable
 * from a field the user had not reached yet, and with a picker there would be
 * no way back to it once a queue had been chosen. So it is the first row of
 * the list, with a sublabel saying what it does.
 *
 * `key` is the sublabel because two queues can carry the same display name;
 * the key is what the rest of the product addresses them by.
 */
function queuePickerOptions(
  queues: ReadonlyArray<{ id: string; key: string; name: string; active: boolean }>,
  wildcard: PickerOption,
): PickerOption[] {
  return [
    wildcard,
    ...queues.map((queue) => ({
      id: queue.id,
      label: queue.name,
      sublabel: translate(queue.active ? "sla.policies.queueSublabel" : "sla.policies.queueSublabelInactive", {
        key: queue.key,
      }),
    })),
  ];
}

/**
 * A calendar as something you can tell apart from the calendar beside it.
 *
 * The TIMEZONE is the sublabel because it is the single fact that changes what
 * a deadline means, and `health` is appended when it is not `ok`: a calendar
 * with no working windows cannot produce a deadline at all, and choosing one
 * here is how a policy ends up silently giving its tickets no SLA.
 */
function calendarPickerOptions(calendars: readonly CalendarOut[]): PickerOption[] {
  return calendars.map((calendar) => ({
    id: calendar.id,
    label: calendar.name,
    sublabel: [
      calendar.timezone,
      calendar.health === "ok" ? null : calendarHealthLabel(calendar.health),
      calendar.active ? null : translate("sla.policies.inactive"),
    ]
      .filter((part): part is string => part != null)
      .join(" · "),
  }));
}

function PolicyFields({
  draft,
  onChange,
  calendars,
  calendarsLoading = false,
  calendarsFailed = false,
  onCreateCalendar,
}: {
  draft: PolicyDraft;
  onChange: (draft: PolicyDraft) => void;
  calendars: readonly CalendarOut[];
  calendarsLoading?: boolean;
  calendarsFailed?: boolean;
  onCreateCalendar?: () => void;
}) {
  const t = useT();
  const queueOptions = useQueueOptions();
  const queuesForbidden = queueOptions === null;
  return (
    <>
      {/* THE UUID FIELDS ARE GONE (cycle 6). Both queue fields fell back to a
          free-text "Queue ID" box whenever `ticket.read` was missing, which is
          the failure mode the picker exists to end: a mistyped id is not a
          validation message, it is the right policy against the wrong queue.
          Without the permission there are no queues to offer, and the hint
          says which permission would produce them -- an honest empty list
          beats a box that accepts anything and resolves nothing. */}
      {/* THE HINT HAD NOWHERE TO APPEAR. Both pickers below carry an
          `emptyHint` explaining that queue names need `ticket.read` — but
          `emptyHint` renders only when the option list is EMPTY, and each
          picker prepends its own "Any queue" / "Stay in the same queue"
          fallback, so the list is never empty and the explanation was
          unreachable. A promise made in a prop that cannot render is the same
          silence the picker was meant to end, so the sentence is stated here
          instead, once, above both controls. */}
      {queuesForbidden ? (
        <p className="form-hint mb-3">
          {t("sla.policies.queuesForbiddenHint")}
        </p>
      ) : null}
      <EntityPicker
        label={t("sla.policies.queueLabel")}
        options={queuePickerOptions(queueOptions ?? [], {
          id: "",
          label: t("sla.common.anyQueue"),
          sublabel: t("sla.policies.anyQueueSublabel"),
        })}
        value={draft.queueId}
        onChange={(id) => onChange({ ...draft, queueId: id })}
        placeholder={t("sla.common.anyQueue")}
        emptyHint={
          queuesForbidden
            ? t("sla.policies.queueEmptyForbidden")
            : t("sla.policies.queueEmpty")
        }
      />
      <Field label={t("sla.policies.priorityLabel")}>
        <select
          className={inputClass}
          value={draft.priority}
          onChange={(e) => onChange({ ...draft, priority: e.target.value })}
        >
          <option value="">{t("sla.common.anyPriority")}</option>
          {SLA_PRIORITIES.map((priority) => (
            <option key={priority} value={priority}>
              {priorityOptionLabel(priority)}
            </option>
          ))}
        </select>
      </Field>
      {/* THE BLOCKER'S OTHER HALF. Cycle 3 made the empty calendar say so on
          submit ("Calendar is required"); it still did not say what would fill
          the list, and on a fresh install the list is ALWAYS empty. A form
          that cannot be completed and does not say why is the finding. */}
      <EntityPicker
        label={t("sla.common.calendar")}
        options={calendarPickerOptions(calendars)}
        value={draft.calendarId}
        onChange={(id) => onChange({ ...draft, calendarId: id })}
        loading={calendarsLoading}
        placeholder={t("sla.policies.calendarPlaceholder")}
        emptyHint={
          calendarsFailed
            ? t("sla.policies.calendarsFailed")
            : t("sla.policies.calendarsEmpty")
        }
      />
      {calendars.length === 0 && !calendarsLoading && !calendarsFailed && (
        <p className="form-hint" style={{ marginBottom: 12 }}>
          {`${t("sla.policies.noCalendars")} `}
          {onCreateCalendar ? (
            /* A button inside a sentence, so it stays a text button -- but a
               borderless one draws no ring of its own, and this is the only
               way out of a first-run dead end. */
            <button
              type="button"
              className="underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]"
              onClick={onCreateCalendar}
            >
              {t("sla.policies.addCalendar")}
            </button>
          ) : (
            t("sla.policies.askAdmin")
          )}
        </p>
      )}
      <Field label={`${t("sla.policies.responseTarget")}${describeTarget(draft.responseMinutes)}`}>
        <input
          type="number"
          min={1}
          className={inputClass}
          value={draft.responseMinutes}
          onChange={(e) => onChange({ ...draft, responseMinutes: e.target.value })}
        />
      </Field>
      <Field label={`${t("sla.policies.resolutionTarget")}${describeTarget(draft.resolutionMinutes)}`}>
        <input
          type="number"
          min={1}
          className={inputClass}
          value={draft.resolutionMinutes}
          onChange={(e) => onChange({ ...draft, resolutionMinutes: e.target.value })}
        />
      </Field>
      <label className="mb-3 flex items-center gap-2">
        <input
          type="checkbox"
          checked={draft.escalatePriority}
          onChange={(e) => onChange({ ...draft, escalatePriority: e.target.checked })}
        />
        {t("sla.policies.raiseOnBreach")}
      </label>
      <EntityPicker
        label={t("sla.policies.escalateQueueLabel")}
        options={queuePickerOptions(queueOptions ?? [], {
          id: "",
          label: t("sla.policies.stayInQueue"),
          sublabel: t("sla.policies.stayInQueueSublabel"),
        })}
        value={draft.escalateToQueueId}
        onChange={(id) => onChange({ ...draft, escalateToQueueId: id })}
        placeholder={t("sla.policies.stayInQueue")}
        emptyHint={
          queuesForbidden
            ? t("sla.policies.escalateEmptyForbidden")
            : t("sla.policies.escalateEmpty")
        }
      />
      <p className="form-hint" style={{ marginBottom: 12 }}>
        {t("sla.policies.noRecompute")}
      </p>
      <label className="mb-3 flex items-center gap-2">
        <input
          type="checkbox"
          checked={draft.active}
          onChange={(e) => onChange({ ...draft, active: e.target.checked })}
        />
        {t("sla.policies.activeLabel")}
      </label>
    </>
  );
}

// Ticket queues are a CROSS-MODULE read gated on `ticket.read`, independent
// of this page's `sla.read`/`sla.admin`: an SLA admin is not guaranteed
// either. `null` means the read is unavailable, and the pickers fall back to
// free-text id fields rather than blocking policy editing on a permission
// this module does not itself require.
function useQueueOptions():
  | ReadonlyArray<{ id: string; key: string; name: string; active: boolean }>
  | null {
  const queues = useSlaTicketQueues();
  if (queues.error instanceof ApiError && queues.error.status === 403) return null;
  return (queues.data ?? []).map((queue) => ({
    id: queue.id,
    key: queue.key,
    name: queue.name,
    active: queue.active,
  }));
}

/**
 * WHICH policy, in the two columns that select it.
 *
 * `(queue, priority)` is the whole identity of a policy -- there is no name --
 * so a confirmation that said "this policy" would name nothing at all.
 */
export function describePolicySubject(
  policy: Pick<PolicyOut, "queue_id" | "priority">,
  queueName: (id: string | null) => string,
): string {
  const queue = policy.queue_id === null ? translate("sla.common.anyQueue") : queueName(policy.queue_id);
  return `${queue} / ${policy.priority === null ? translate("sla.common.anyPriority") : priorityOptionLabel(policy.priority)}`;
}
