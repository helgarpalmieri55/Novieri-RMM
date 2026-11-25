import { useQuery } from "@tanstack/react-query";
import { useLayoutEffect, useState } from "react";
import { Form, FormActions, attemptProblems } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Button, Dialog, Field, inputClass, Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { Icon } from "../../shell/icons";
import {
  useCreateTicketCategoryMutation,
  useCreateTicketQueueMutation,
  useTicketCategories,
  useTicketQueues,
  useUpdateTicketCategoryMutation,
  useUpdateTicketQueueMutation,
} from "./api";
import { SavedRepliesPanel } from "./SavedRepliesPanel";
import { SectionLead } from "./SectionLead";
import { ServiceDeskNav } from "./ServiceDeskNav";
import { ticketTypeLabel } from "./serviceDeskState";
import type { TicketType } from "./types";
import { Btn, FormSection, PageHead, QueryState, StatusPill, useNewParam } from "../../shell/components";
import { useEverywherePermissions } from "../../lib/mePermissions";

// Auditor B, cross-cutting: "Same silent-no-op failure repeats on every form
// in the module." `disabled={create.isPending || !key || !name}` on both
// dialogs below was the last surviving instance in this module -- the click
// moved nothing and named neither field. `Key` and `Name` are what the form
// calls them, so the refusal reads back to the control being looked at.
const queueRules = (): readonly FieldRule[] => [
  { name: "key", label: translate("serviceDesk.common.key") },
  { name: "name", label: translate("serviceDesk.common.name") },
];

const categoryRules = (): readonly FieldRule[] => [
  { name: "key", label: translate("serviceDesk.common.key") },
  { name: "name", label: translate("serviceDesk.common.name") },
];

// Queues and categories -- spec §11's two admin surfaces. Reads are
// `ticket.read`-gated (App.tsx's nav entry and the route itself already
// require it); every write below is `ticket.admin`-gated, presentationally
// only -- api.py's require_permission is the actual authority on every one
// of these routes.
//
// The design (`P['service-desk/admin']`) draws one bare panel of rows led by
// the list-check glyph. Two of its four tabs (Priorities, Automation) name
// surfaces this API does not serve, so the two that exist are drawn as two
// sections rather than a tab strip that is half dead.
//
// ON THE FORM KIT (redesign plan, Task 7, 2.5; Receta FO): a section per
// kind of row (queues, categories, saved replies), each a table with its own
// "New ..." in a dialog that opens from the URL (`?new=queue`,
// `?new=category`, decision D8), and the edit of a row in a dialog too. So no
// row turns into a form in place, and the body holds no primary button: the
// one primary is the open dialog's Save. No save bar: every change is one
// row, saved when its dialog is confirmed.
export function ServiceDeskAdminPage() {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("ticket.read");
  const canAdmin = permissions.includes("ticket.admin");
  // Configuration every client shares (GLOBAL_WRITES): the code on EVERY client (phase 3 design §2).
  const canConfigure = useEverywherePermissions().has("ticket.admin");

  return (
    <div>
      <PageHead
        title={t("serviceDesk.admin.title")}
        description={t("serviceDesk.admin.description")}
      />
      <ServiceDeskNav canRead={canRead} canAdmin={canAdmin} />

      <QueuesSection canAdmin={canConfigure} />
      <CategoriesSection canAdmin={canConfigure} />
      <SavedRepliesPanel canAdmin={canConfigure} />
    </div>
  );
}

/**
 * The design's on/off pill, with its glyph, for a queue or category.
 *
 * `StatusPill`, NOT THE LEGACY `Badge`. The badge printed whatever string it
 * was handed, so fourteen rows read "active" and "inactive" in the wire's own
 * lowercase while every sibling list in the console reads "Active",
 * "Disabled", "Complete". `StatusPill` humanises the value through the one
 * table that does it (`humaniseEnum`) and picks the kind through the one
 * table that maps a wire word to a severity (`kindOf`: active -> ok,
 * inactive -> mut), so the glyph carries the state as well as the colour.
 */
function ActivePill({ active }: { active: boolean }) {
  return <StatusPill value={active ? "active" : "inactive"} />;
}

/** A row being edited: its name and whether it is active, the two things the server lets a row change. */
interface RowEdit {
  readonly id: string;
  readonly name: string;
  readonly active: boolean;
}

/**
 * Edit one queue or category in a dialog (redesign plan, Task 7, 2.5): the
 * same two fields the row used to turn into in place, and the same call.
 */
function EditRowDialog({
  title,
  nameLabel,
  activeLabel,
  row,
  pending,
  error,
  onSave,
  onClose,
}: {
  title: string;
  nameLabel: string;
  activeLabel: string;
  row: RowEdit;
  pending: boolean;
  error: unknown;
  onSave: (name: string, active: boolean) => void;
  onClose: () => void;
}) {
  const t = useT();
  const [name, setName] = useState(row.name);
  const [active, setActive] = useState(row.active);
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  function submit() {
    const next: Attempt = { rules: nameRules(nameLabel), values: { name } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    onSave(name, active);
  }
  return (
    <Dialog open onClose={onClose} title={title}>
      <Form onSubmit={submit}>
        <div className="form-grid">
          <Field label={nameLabel}>
            <input className={inputClass} required value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              aria-label={activeLabel}
              checked={active}
              onChange={(e) => setActive(e.target.checked)}
            />
            {t("serviceDesk.common.active")}
          </label>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          {/* `type="button"`: inside a real <form> an untyped button submits. */}
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("common.actions.cancel")}
          </Button>
          <FormActions label={t("common.actions.save")} pending={pending} error={error} problems={attemptProblems(attempt)} />
        </div>
      </Form>
    </Dialog>
  );
}

/** The one field the edit dialog insists on, named as the dialog names it. */
const nameRules =
  (label: string) => (): readonly FieldRule[] => [{ name: "name", label }];

function QueuesSection({ canAdmin }: { canAdmin: boolean }) {
  const t = useT();
  const queues = useTicketQueues(true);
  const create = useCreateTicketQueueMutation();
  const update = useUpdateTicketQueueMutation();
  // New → Queue (decision D8): `?new=queue` opens it, so it can be linked.
  const created = useNewParam("queue");
  const [key, setKey] = useState("");
  const [name, setName] = useState("");
  const [editing, setEditing] = useState<RowEdit | null>(null);
  // Set ON SUBMIT, never while typing; held as rules and values so the
  // sentences are worded at render.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  function submit() {
    const next: Attempt = { rules: queueRules, values: { key, name } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    create.mutate(
      // Trimmed to match what was validated: `requiredProblems` trims before
      // testing emptiness, so an untrimmed send could carry a name the checks
      // never actually saw.
      { key: key.trim(), name: name.trim() },
      { onSuccess: () => { setKey(""); setName(""); setAttempt(null); created.close(); } },
    );
  }

  // THE FORM STARTS EMPTY WHENEVER THE DIALOG OPENS, whatever opened it (the
  // button, Back, a link to `?new=queue`; fix round 1: Back closes it without
  // `closeCreate`). The section stays mounted with the dialog shut, so
  // mutation state OUTLIVES the dialog: without `create.reset()` a failed Save
  // leaves `create.error` set and the NEXT open announces the refusal of a
  // request nobody has made yet.
  const opened = canAdmin && created.open;
  useLayoutEffect(() => {
    if (!opened) return;
    setKey("");
    setName("");
    setAttempt(null);
    create.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opened]);

  function closeCreate() {
    created.close();
  }

  function closeEdit() {
    update.reset();
    setEditing(null);
  }

  return (
    <>
    <FormSection title={t("serviceDesk.admin.queues")} icon="list-check">
      <SectionLead
        text={t("serviceDesk.admin.queuesSubtitle")}
        action={canAdmin ? <Btn label={t("serviceDesk.admin.newQueue")} icon="plus" onClick={created.start} /> : undefined}
      />
      <QueryState
        loading={queues.isLoading}
        error={queues.error}
        empty={queues.data != null && queues.data.length === 0}
        emptyIcon="list-check"
        emptyLabel={t("serviceDesk.admin.noQueues")}
      >
        <Table label={t("serviceDesk.admin.queues")}>
          <thead>
            <tr>
              <Th>{t("serviceDesk.common.name")}</Th>
              <Th>{t("serviceDesk.admin.activeColumn")}</Th>
              {canAdmin && <Th>{""}</Th>}
            </tr>
          </thead>
          <tbody>
            {queues.data?.map((q) => (
              <tr key={q.id}>
                <Td lead={<Icon name="list-check" size={18} />}>
                  <div className="cell-title">{q.name}</div>
                  {/* The key is what the rest of the product addresses a queue
                      by, so it stays under the name in mono rather than in a
                      column of its own. */}
                  <div className="cell-sub mono">{q.key}</div>
                </Td>
                <Td>
                  <ActivePill active={q.active} />
                </Td>
                {canAdmin && (
                  <Td>
                    <Btn
                      small
                      label={t("serviceDesk.common.edit")}
                      onClick={() => setEditing({ id: q.id, name: q.name, active: q.active })}
                    />
                  </Td>
                )}
              </tr>
            ))}
          </tbody>
        </Table>
      </QueryState>

      {editing ? (
        <EditRowDialog
          title={t("serviceDesk.admin.editQueue")}
          nameLabel={t("serviceDesk.admin.queueName")}
          activeLabel={t("serviceDesk.admin.queueActive")}
          row={editing}
          pending={update.isPending}
          error={update.error}
          onSave={(nextName, active) =>
            update.mutate({ id: editing.id, body: { name: nextName, active } }, { onSuccess: () => setEditing(null) })
          }
          onClose={closeEdit}
        />
      ) : null}

      {/* Open only for someone who may create one, whatever the URL asks. */}
    </FormSection>
      <Dialog open={canAdmin && created.open} onClose={closeCreate} title={t("serviceDesk.admin.newQueue")}>
        {/* A real <form>, so Enter in Name saves instead of doing nothing --
            ui.tsx's Button sets no type, so the submit control was an orphan
            and the keyboard path did not exist. */}
        <Form onSubmit={submit}>
          <div className="form-grid">
            <Field label={t("serviceDesk.common.key")}>
              <input
                className={inputClass}
                required
                value={key}
                onChange={(e) => setKey(e.target.value)}
              />
            </Field>
            <Field label={t("serviceDesk.common.name")}>
              <input
                className={inputClass}
                required
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </Field>
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2">
            {/* `type="button"` is load-bearing inside a real <form>: an
                untyped button in a form IS a submit button, so Cancel would
                create the queue it was cancelling. */}
            <Button type="button" variant="ghost" onClick={closeCreate}>
              {t("common.actions.cancel")}
            </Button>
            <FormActions
              label={t("common.actions.save")}
              pending={create.isPending}
              error={create.error}
              problems={attemptProblems(attempt)}
            />
          </div>
        </Form>
      </Dialog>
    </>
  );
}

function CategoriesSection({ canAdmin }: { canAdmin: boolean }) {
  const t = useT();
  const categories = useTicketCategories(true);
  const queues = useTicketQueues(true);
  const create = useCreateTicketCategoryMutation();
  const update = useUpdateTicketCategoryMutation();
  // New → Category (decision D8): `?new=category`.
  const created = useNewParam("category");
  const [key, setKey] = useState("");
  const [name, setName] = useState("");
  const [type, setType] = useState<TicketType>("incident");
  const [requiresApproval, setRequiresApproval] = useState(false);
  const [defaultQueueId, setDefaultQueueId] = useState("");
  const [editing, setEditing] = useState<RowEdit | null>(null);
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  const queueName = (id: string | null) =>
    id ? (queues.data?.find((q) => q.id === id)?.name ?? id) : null;

  function submit() {
    const next: Attempt = { rules: categoryRules, values: { key, name } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    create.mutate(
      {
        key: key.trim(),
        name: name.trim(),
        type,
        requires_approval: requiresApproval,
        default_queue_id: defaultQueueId || null,
      },
      {
        onSuccess: () => {
          setKey("");
          setName("");
          setType("incident");
          setRequiresApproval(false);
          setDefaultQueueId("");
          setAttempt(null);
          created.close();
        },
      },
    );
  }

  // See QueuesSection: the form starts empty whenever the dialog opens.
  const opened = canAdmin && created.open;
  useLayoutEffect(() => {
    if (!opened) return;
    setKey("");
    setName("");
    setType("incident");
    setRequiresApproval(false);
    setDefaultQueueId("");
    setAttempt(null);
    create.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opened]);

  function closeCreate() {
    created.close();
  }

  function closeEdit() {
    update.reset();
    setEditing(null);
  }

  return (
    <>
    <FormSection title={t("serviceDesk.admin.categories")} icon="tag">
      <SectionLead
        text={t("serviceDesk.admin.categoriesSubtitle")}
        action={canAdmin ? <Btn label={t("serviceDesk.admin.newCategory")} icon="plus" onClick={created.start} /> : undefined}
      />
      <QueryState
        loading={categories.isLoading}
        error={categories.error}
        empty={categories.data != null && categories.data.length === 0}
        emptyIcon="tag"
        emptyLabel={t("serviceDesk.admin.noCategories")}
      >
        <Table label={t("serviceDesk.admin.categories")}>
          <thead>
            <tr>
              <Th>{t("serviceDesk.common.name")}</Th>
              <Th>{t("serviceDesk.common.type")}</Th>
              <Th>{t("serviceDesk.admin.approval")}</Th>
              <Th>{t("serviceDesk.admin.activeColumn")}</Th>
              {canAdmin && <Th>{""}</Th>}
            </tr>
          </thead>
          <tbody>
            {categories.data?.map((c) => (
              <tr key={c.id}>
                <Td lead={<Icon name="tag" size={18} />}>
                  <div className="cell-title">{c.name}</div>
                  <div className="cell-sub">
                    <span className="mono">{c.key}</span>
                    {queueName(c.default_queue_id)
                      ? ` · ${t("serviceDesk.admin.defaultsTo", { queue: queueName(c.default_queue_id) ?? "" })}`
                      : ""}
                  </div>
                </Td>
                {/* Auditor A (MEDIUM): the category table printed `incident` /
                    `request` raw, beside a Type select that does not. */}
                <Td>{ticketTypeLabel(c.type)}</Td>
                <Td>
                  {c.requires_approval ? (
                    t("serviceDesk.admin.required")
                  ) : (
                    <span className="cell-sub">{t("serviceDesk.admin.notRequired")}</span>
                  )}
                </Td>
                <Td>
                  <ActivePill active={c.active} />
                </Td>
                {canAdmin && (
                  <Td>
                    <Btn
                      small
                      label={t("serviceDesk.common.edit")}
                      onClick={() => setEditing({ id: c.id, name: c.name, active: c.active })}
                    />
                  </Td>
                )}
              </tr>
            ))}
          </tbody>
        </Table>
      </QueryState>

      {editing ? (
        <EditRowDialog
          title={t("serviceDesk.admin.editCategory")}
          nameLabel={t("serviceDesk.admin.categoryName")}
          activeLabel={t("serviceDesk.admin.categoryActive")}
          row={editing}
          pending={update.isPending}
          error={update.error}
          onSave={(nextName, active) =>
            update.mutate({ id: editing.id, body: { name: nextName, active } }, { onSuccess: () => setEditing(null) })
          }
          onClose={closeEdit}
        />
      ) : null}

    </FormSection>
      <Dialog open={canAdmin && created.open} onClose={closeCreate} title={t("serviceDesk.admin.newCategory")}>
        <Form onSubmit={submit}>
          <div className="form-grid">
            <Field label={t("serviceDesk.common.key")}>
              <input
                className={inputClass}
                required
                value={key}
                onChange={(e) => setKey(e.target.value)}
              />
            </Field>
            <Field label={t("serviceDesk.common.name")}>
              <input
                className={inputClass}
                required
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </Field>
            <Field label={t("serviceDesk.common.type")}>
              <select
                className={inputClass}
                value={type}
                onChange={(e) => setType(e.target.value as TicketType)}
              >
                <option value="incident">{ticketTypeLabel("incident")}</option>
                <option value="request">{ticketTypeLabel("request")}</option>
              </select>
            </Field>
            {/* requires_approval is meaningful only for type="request" (spec §2)
                -- the service layer rejects it for "incident"; the control is
                hidden rather than merely disabled so a technician never sees a
                combination the server will refuse. */}
            {type === "request" && (
              <Field label={t("serviceDesk.admin.requiresApproval")}>
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={requiresApproval}
                    onChange={(e) => setRequiresApproval(e.target.checked)}
                  />
                  {t("serviceDesk.admin.requiresApproval")}
                </label>
              </Field>
            )}
            <Field label={t("serviceDesk.admin.defaultQueue")}>
              <select
                className={inputClass}
                value={defaultQueueId}
                onChange={(e) => setDefaultQueueId(e.target.value)}
              >
                <option value="">{t("serviceDesk.admin.none")}</option>
                {queues.data?.map((q) => (
                  <option key={q.id} value={q.id}>
                    {q.name}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2">
            <Button type="button" variant="ghost" onClick={closeCreate}>
              {t("common.actions.cancel")}
            </Button>
            <FormActions
              label={t("common.actions.save")}
              pending={create.isPending}
              error={create.error}
              problems={attemptProblems(attempt)}
            />
          </div>
        </Form>
      </Dialog>
    </>
  );
}
