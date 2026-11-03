import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Form, FormActions, attemptProblems, describeSubmitError } from "../../components/form";
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
import { ServiceDeskNav } from "./ServiceDeskNav";
import { ticketTypeLabel } from "./serviceDeskState";
import type { TicketType } from "./types";
import { Btn, PageHead, Panel, QueryState, StatusPill } from "../../shell/components";
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
// the list-check glyph, with the "New queue" primary in the panel's action
// slot. Two of its four tabs (Priorities, Automation) name surfaces this API
// does not serve, so the two that exist are drawn as two panels, each with
// its own action, rather than a tab strip that is half dead.
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

      <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}>
        <QueuesPanel canAdmin={canConfigure} />
        <CategoriesPanel canAdmin={canConfigure} />
        <SavedRepliesPanel canAdmin={canConfigure} />
      </div>
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

function QueuesPanel({ canAdmin }: { canAdmin: boolean }) {
  const t = useT();
  const queues = useTicketQueues(true);
  const create = useCreateTicketQueueMutation();
  const update = useUpdateTicketQueueMutation();
  const [creating, setCreating] = useState(false);
  const [key, setKey] = useState("");
  const [name, setName] = useState("");

  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const [editActive, setEditActive] = useState(true);
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
      { onSuccess: () => { setKey(""); setName(""); setAttempt(null); setCreating(false); } },
    );
  }

  // This panel stays mounted with the dialog shut (`Dialog` returns null when
  // `open` is false, but QueuesPanel does not unmount), so mutation state
  // OUTLIVES the dialog: without `create.reset()` a failed Save leaves
  // `create.error` set and the NEXT open announces the refusal of a request
  // nobody has made yet. TicketListPage's NewTicketDialog already fixed this
  // for itself and wrote down why; the same hole was open on both admin
  // dialogs.
  function closeCreate() {
    create.reset();
    setAttempt(null);
    setCreating(false);
  }

  function startEdit(q: { id: string; name: string; active: boolean }) {
    setEditingId(q.id);
    setEditName(q.name);
    setEditActive(q.active);
  }

  function saveEdit(id: string) {
    update.mutate(
      { id, body: { name: editName, active: editActive } },
      { onSuccess: () => setEditingId(null) },
    );
  }

  return (
    <Panel
      title={t("serviceDesk.admin.queues")}
      subtitle={t("serviceDesk.admin.queuesSubtitle")}
      bare
      actions={
        canAdmin ? (
          <Btn label={t("serviceDesk.admin.newQueue")} icon="plus" variant="primary" onClick={() => setCreating(true)} />
        ) : undefined
      }
    >
      <QueryState
        loading={queues.isLoading}
        error={queues.error}
        empty={queues.data != null && queues.data.length === 0}
        emptyIcon="list-check"
        emptyLabel={t("serviceDesk.admin.noQueues")}
      >
      <Table>
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
                {editingId === q.id ? (
                  <input
                    aria-label={t("serviceDesk.admin.queueName")}
                    className={inputClass}
                    value={editName}
                    onChange={(e) => setEditName(e.target.value)}
                  />
                ) : (
                  <div className="cell-title">{q.name}</div>
                )}
                {/* The key is what the rest of the product addresses a queue
                    by, so it stays under the name in mono rather than in a
                    column of its own. */}
                <div className="cell-sub mono">{q.key}</div>
              </Td>
              <Td>
                {editingId === q.id ? (
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      aria-label={t("serviceDesk.admin.queueActive")}
                      checked={editActive}
                      onChange={(e) => setEditActive(e.target.checked)}
                    />
                    {t("serviceDesk.common.active")}
                  </label>
                ) : (
                  <ActivePill active={q.active} />
                )}
              </Td>
              {canAdmin && (
                <Td>
                  {/* ROW ACTIONS ARE `Btn small`, the box every other row
                      action in the console draws (`/devices`, `/sla`,
                      `/email-intake`, `/notifications` after this wave). Edit
                      was a borderless ghost here and a bordered button one
                      route over, which is the same control at two weights on
                      two pages of one module. */}
                  {editingId === q.id ? (
                    <div className="flex gap-2">
                      <Btn
                        small
                        variant="primary"
                        label={update.isPending ? t("serviceDesk.common.saving") : t("common.actions.save")}
                        onClick={() => saveEdit(q.id)}
                        disabled={update.isPending || !editName}
                      />
                      <Btn small label={t("common.actions.cancel")} onClick={() => setEditingId(null)} />
                    </div>
                  ) : (
                    <Btn small label={t("serviceDesk.common.edit")} onClick={() => startEdit(q)} />
                  )}
                </Td>
              )}
            </tr>
          ))}
        </tbody>
      </Table>
      </QueryState>
      {/* Cycle 4: `String(update.error)` printed "ApiError: " plus the raw body
          -- a Pydantic payload for a 422, nothing at all for a bodyless 503 --
          and announced none of it. */}
      {update.isError && (
        <p role="alert" className="notice red" style={{ margin: 14 }}>
          {describeSubmitError(update.error)}
        </p>
      )}

      <Dialog open={creating} onClose={closeCreate} title={t("serviceDesk.admin.newQueue")}>
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
    </Panel>
  );
}

function CategoriesPanel({ canAdmin }: { canAdmin: boolean }) {
  const t = useT();
  const categories = useTicketCategories(true);
  const queues = useTicketQueues(true);
  const create = useCreateTicketCategoryMutation();
  const update = useUpdateTicketCategoryMutation();
  const [creating, setCreating] = useState(false);
  const [key, setKey] = useState("");
  const [name, setName] = useState("");
  const [type, setType] = useState<TicketType>("incident");
  const [requiresApproval, setRequiresApproval] = useState(false);
  const [defaultQueueId, setDefaultQueueId] = useState("");

  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const [editActive, setEditActive] = useState(true);
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
          setCreating(false);
        },
      },
    );
  }

  // See QueuesPanel.closeCreate -- the same stale-error-on-reopen hole.
  function closeCreate() {
    create.reset();
    setAttempt(null);
    setCreating(false);
  }

  function startEdit(c: { id: string; name: string; active: boolean }) {
    setEditingId(c.id);
    setEditName(c.name);
    setEditActive(c.active);
  }

  function saveEdit(id: string) {
    update.mutate(
      { id, body: { name: editName, active: editActive } },
      { onSuccess: () => setEditingId(null) },
    );
  }

  return (
    <Panel
      title={t("serviceDesk.admin.categories")}
      subtitle={t("serviceDesk.admin.categoriesSubtitle")}
      bare
      actions={
        canAdmin ? (
          <Btn label={t("serviceDesk.admin.newCategory")} icon="plus" variant="primary" onClick={() => setCreating(true)} />
        ) : undefined
      }
    >
      <QueryState
        loading={categories.isLoading}
        error={categories.error}
        empty={categories.data != null && categories.data.length === 0}
        emptyIcon="tag"
        emptyLabel={t("serviceDesk.admin.noCategories")}
      >
      <Table>
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
                {editingId === c.id ? (
                  <input
                    aria-label={t("serviceDesk.admin.categoryName")}
                    className={inputClass}
                    value={editName}
                    onChange={(e) => setEditName(e.target.value)}
                  />
                ) : (
                  <div className="cell-title">{c.name}</div>
                )}
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
                {editingId === c.id ? (
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      aria-label={t("serviceDesk.admin.categoryActive")}
                      checked={editActive}
                      onChange={(e) => setEditActive(e.target.checked)}
                    />
                    {t("serviceDesk.common.active")}
                  </label>
                ) : (
                  <ActivePill active={c.active} />
                )}
              </Td>
              {canAdmin && (
                <Td>
                  {/* ROW ACTIONS ARE `Btn small`, the box every other row
                      action in the console draws (`/devices`, `/sla`,
                      `/email-intake`, `/notifications` after this wave). Edit
                      was a borderless ghost here and a bordered button one
                      route over, which is the same control at two weights on
                      two pages of one module. */}
                  {editingId === c.id ? (
                    <div className="flex gap-2">
                      <Btn
                        small
                        variant="primary"
                        label={update.isPending ? t("serviceDesk.common.saving") : t("common.actions.save")}
                        onClick={() => saveEdit(c.id)}
                        disabled={update.isPending || !editName}
                      />
                      <Btn small label={t("common.actions.cancel")} onClick={() => setEditingId(null)} />
                    </div>
                  ) : (
                    <Btn small label={t("serviceDesk.common.edit")} onClick={() => startEdit(c)} />
                  )}
                </Td>
              )}
            </tr>
          ))}
        </tbody>
      </Table>
      </QueryState>
      {update.isError && (
        <p role="alert" className="notice red" style={{ margin: 14 }}>
          {describeSubmitError(update.error)}
        </p>
      )}

      <Dialog open={creating} onClose={closeCreate} title={t("serviceDesk.admin.newCategory")}>
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
    </Panel>
  );
}
