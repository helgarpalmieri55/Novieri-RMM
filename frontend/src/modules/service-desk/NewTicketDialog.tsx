/**
 * The technician's "New ticket" form. Shared by the ticket list and the device
 * page, which opens it with the device (and a subject) already filled in.
 */

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Form, FormActions, attemptProblems } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { EntityPicker } from "../../components/picker";
import { Button, Dialog, Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { internalClientId, requireKnown } from "../../lib/internalClient";
import {
  useCreateTicketMutation,
  useDirectoryDevices,
  useDirectoryUsers,
  useTicketCategories,
  useTicketQueues,
} from "./api";
import {
  devicePickerEmptyHint,
  devicePickerOptions,
  userPickerEmptyHint,
  userPickerOptions,
} from "./pickerOptions";
import { ticketTypeLabel } from "./serviceDeskState";
import type { TicketCreate, TicketOut, TicketType } from "./types";

// The words ON SCREEN, so "Requester is required" reads back to the field
// being looked at. `description` is absent: TicketCreate defaults it to "".
const ticketRules = (): readonly FieldRule[] => [
  { name: "categoryId", label: translate("serviceDesk.common.category") },
  { name: "subject", label: translate("serviceDesk.common.subject") },
  { name: "requesterId", label: translate("serviceDesk.common.requester") },
];

export function NewTicketDialog({
  open,
  onClose,
  myUserId,
  initialDeviceId,
  initialSubject,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  myUserId?: string;
  /** Prefills the Device picker; the technician can still change it. */
  initialDeviceId?: string;
  initialSubject?: string;
  onCreated?: (ticket: TicketOut) => void;
}) {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const create = useCreateTicketMutation();
  const categories = useTicketCategories();
  const queues = useTicketQueues();
  const users = useDirectoryUsers();
  const devices = useDirectoryDevices();
  // Not memoised: the sublabels are worded (account status, agent state), and a
  // memo would keep them in the language the page opened in.
  const userOptions = userPickerOptions(users.data ?? []);
  const deviceOptions = devicePickerOptions(devices.data ?? []);

  const [type, setType] = useState<TicketType>("incident");
  const [categoryId, setCategoryId] = useState("");
  const [queueId, setQueueId] = useState("");
  const [subject, setSubject] = useState(initialSubject ?? "");
  const [description, setDescription] = useState("");
  // DEFAULTED TO THE SIGNED-IN USER, which is the audit's own instruction:
  // "Default Requester ID to the signed-in user (the app already knows their
  // ID -- it passes assigned_to=06a8438d-... in its own queries)." The
  // technician can still pick somebody else, and because the control is a
  // picker they READ the name they are filing on behalf of rather than
  // inheriting an invisible id.
  const [requesterId, setRequesterId] = useState(myUserId ?? "");
  const [assignedTo, setAssignedTo] = useState("");
  const [deviceId, setDeviceId] = useState(initialDeviceId ?? "");
  // Set ON SUBMIT, never while typing -- a complaint about a field the
  // technician has not reached yet is its own kind of unhelpful. Held as the
  // rules and the values they were checked against, so the sentences are
  // worded at render and follow a language switch.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  // `/me` resolves after this component first renders, so the default above
  // would be "" forever without this. Only ever fills a field the technician
  // has not touched: once `requesterId` is set, their choice stands.
  useEffect(() => {
    if (myUserId) setRequesterId((current) => current || myUserId);
  }, [myUserId]);

  const categoriesForType = categories.data?.filter((c) => c.type === type) ?? [];

  function reset() {
    setType("incident");
    setCategoryId("");
    setQueueId("");
    setSubject(initialSubject ?? "");
    setDescription("");
    setRequesterId(myUserId ?? "");
    setAssignedTo("");
    setDeviceId(initialDeviceId ?? "");
    setAttempt(null);
  }

  // This component stays mounted with the dialog shut (TicketListPage renders
  // it unconditionally and toggles `open`), so mutation state OUTLIVES the
  // dialog: without these resets a failed Save leaves `create.error` set and
  // the next open announces the refusal of a request nobody has made yet.
  function close() {
    setAttempt(null);
    create.reset();
    onClose();
  }

  function submit() {
    const next: Attempt = { rules: ticketRules, values: { categoryId, subject, requesterId } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;

    const base: TicketCreate = {
      type,
      category_id: categoryId,
      queue_id: queueId || null,
      // Trimmed to match what was validated -- `requiredProblems` trims
      // before testing emptiness, so an untrimmed send could carry a subject
      // the checks never actually saw.
      subject: subject.trim(),
      description: description.trim(),
      // requester_id is required by the service layer for every
      // technician-origin ticket (tickets.py's create_ticket) even though
      // TicketCreate types it as optional to match the schema exactly.
      requester_id: requesterId.trim(),
      assigned_to: assignedTo || null,
      device_id: deviceId || null,
    };
    // A ticket with a device is the device's client's: the server takes it
    // from the device, so nothing is sent. Without one it names its client.
    // PHASE 3: the client/site selector. Until it exists that is the
    // organisation's internal client, whose id is the organisation's --
    // `/me`'s `org_id` (`lib/internalClient.ts`).
    const internalClient = internalClientId(me.data);
    const build = (): TicketCreate =>
      deviceId ? base : { ...base, client_id: requireKnown(internalClient) };
    create.mutate(build, {
      onSuccess: (ticket) => {
        reset();
        onClose();
        onCreated?.(ticket);
      },
    });
  }

  return (
    <Dialog open={open} onClose={close} title={t("serviceDesk.newTicket.title")}>
      {/* A real <form>, so Enter in Subject saves instead of doing nothing --
          `ui.tsx`'s Button sets no type, so before this the submit control was
          an orphan and the keyboard path did not exist. */}
      <Form onSubmit={submit}>
        <div className="form-grid">
          <Field label={t("serviceDesk.common.type")}>
            <select
              className={inputClass}
              value={type}
              onChange={(e) => { setType(e.target.value as TicketType); setCategoryId(""); }}
            >
              <option value="incident">{ticketTypeLabel("incident")}</option>
              <option value="request">{ticketTypeLabel("request")}</option>
            </select>
          </Field>
          {/* `required` states the same fact for assistive tech; it blocks
              nothing, because <Form> is noValidate -- a native bubble that
              vanishes on the next click is the silent no-op wearing a hat. No
              "*" markers: this dialog already declares optionality the other way
              round ("Queue (optional...)", "Assign to (optional)", "Device ID
              (optional)"), and two conventions for one fact in one form is worse
              than either. */}
          <Field label={t("serviceDesk.common.category")}>
            <select
              className={inputClass}
              required
              value={categoryId}
              onChange={(e) => setCategoryId(e.target.value)}
            >
              <option value="">{t("serviceDesk.newTicket.selectCategory")}</option>
              {categoriesForType.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("serviceDesk.newTicket.queue")} full>
            <select className={inputClass} value={queueId} onChange={(e) => setQueueId(e.target.value)}>
              <option value="">{t("serviceDesk.newTicket.useDefault")}</option>
              {queues.data?.map((q) => (
                <option key={q.id} value={q.id}>
                  {q.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("serviceDesk.common.subject")} full>
            <input
              className={inputClass}
              required
              value={subject}
              onChange={(e) => setSubject(e.target.value)}
            />
          </Field>
          {/* Auditor A (HIGH): "Ticket Description is a single-line
              <input type="text">, not a textarea ... A ticket description is
              the primary free-text field of the entire module -- users paste
              error messages, steps to reproduce and log excerpts into it. It
              also visually signals 'short value expected', so users will
              under-describe their issues." */}
          <Field label={t("serviceDesk.common.description")} full>
            <textarea
              className={inputClass}
              rows={5}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </Field>
          {/* Auditor A (HIGH): "Three raw ID fields in one dialog -- Requester
              ID, Assign to, Device ID -- all free text with no picker." All
              three are pickers now; the labels lose the "ID" because there is
              no longer an id to type. */}
          <EntityPicker
            label={t("serviceDesk.common.requester")}
            options={userOptions}
            loading={users.isLoading}
            emptyHint={userPickerEmptyHint()}
            placeholder={t("serviceDesk.newTicket.requesterPlaceholder")}
            value={requesterId}
            onChange={setRequesterId}
          />
          <EntityPicker
            label={t("serviceDesk.newTicket.assignTo")}
            options={userOptions}
            loading={users.isLoading}
            emptyHint={userPickerEmptyHint()}
            placeholder={t("serviceDesk.newTicket.leaveUnassigned")}
            value={assignedTo}
            onChange={setAssignedTo}
          />
          <EntityPicker
            label={t("serviceDesk.newTicket.deviceOptional")}
            options={deviceOptions}
            loading={devices.isLoading}
            emptyHint={devicePickerEmptyHint()}
            placeholder={t("serviceDesk.common.searchHostnames")}
            value={deviceId}
            onChange={setDeviceId}
          />
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          {/* `type="button"` is load-bearing now that these controls live in a
              real <form>: a button with no type inside a form IS a submit
              button, so Cancel would create the ticket it was cancelling. */}
          <Button type="button" variant="ghost" onClick={close}>
            {t("common.actions.cancel")}
          </Button>
          {/* Enabled with the fields empty ON PURPOSE. `disabled={... ||
              !requesterId}` is exactly the finding: Requester ID gated the
              save and the dead button never said so, so the technician clicked
              and nothing moved. The refusal is now what they get to read, and
              the server's own failure comes from here too -- replacing the
              `{String(create.error)}` line that rendered a raw object and, for
              the bodyless 503, nothing at all. */}
          <FormActions
            label={t("common.actions.save")}
            pending={create.isPending}
            error={create.error}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
}
