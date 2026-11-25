// Roles and the permission matrix — `GET /api/v1/permissions`,
// `GET|POST /api/v1/roles`, `PATCH|DELETE /api/v1/roles/{id}`,
// `PUT /api/v1/roles/{id}/permissions`. Read `roles.read`, write `roles.manage`.
//
// THE MATRIX IS THE PRODUCT (spec §1.1). "What modules, at what level" is a
// grid: 32 modules down, actions across, 92 cells occupied. There is no new
// vocabulary here and no summary of it — the grid is the thing.
//
// THE CATALOGUE IS NEVER RE-LISTED IN THIS FILE. Every code, module and action
// comes from `GET /api/v1/permissions`, which the server builds from
// `app.core.rbac.PERMISSIONS` — the same dict `has_permission` validates
// against. A hardcoded list here would mean a code added to the platform stops
// appearing in the console until somebody remembers this file, and the failure
// is silent: the grid still renders, just without the new capability. So the
// module groups are built from the `module` field the SERVER sent, not from a
// prefix this console re-derives, and `roles.test.tsx` feeds it a module name
// that exists nowhere in this repository to prove it.
//
// EDITING A SEEDED ROLE CLONES IT, AND THE SCREEN SAYS SO (spec §1.3). The
// five seeded roles are GLOBAL — one `technician` row shared by every tenant —
// so the server takes an org-scoped copy before applying an edit and returns
// the copy's id. Pretending nothing happened would leave an operator with a
// mental model that is wrong in exactly the way that matters: they would
// believe they had edited the row another tenant is using.
//
// `super-admin` IS NEVER RENDERED. The server never lists it; this file
// filters it anyway (`assignableRoles`). Two independent refusals, because the
// server's is the one that holds and this one is the one an operator sees — an
// "Edit permissions for super-admin" button on a tenant-scoped page is an
// invitation to try a tenant escalation, even when the attempt would 4xx.
//
// THE AFFECTED-USER COUNT IS THE SERVER'S NUMBER. `assigned_users` comes from
// `GET /roles`, computed over `user_roles` in the tenant session. Spec §1.4
// requires it BEFORE applying, because "edit role" reads local and is not: it
// redefines what some number of real people can do to the fleet.
//
// CREATING, CHANGING THE PERMISSIONS OF AND DELETING A ROLE NEED A RECENT
// SECOND FACTOR (design §2.5, widened by the operator on 2026-11-09, S5). The
// server answers 403 `step_up_required` unless the operator confirmed their
// own second factor in the last 10 minutes; `useStepUp` asks for it -- over
// the delete confirmation, for a delete -- and sends the same act once more
// only after the server accepted it. Renaming a role is never asked.

import { useMemo, useState, type CSSProperties } from "react";

import { attemptProblems, describeSubmitError, type Attempt } from "../../components/form";
import { useStepUp } from "../../components/stepUp";
import { useT } from "../../i18n/context";
import { Button, Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
import {
  Btn,
  FormSection,
  KIND_ICON,
  Panel,
  QueryState,
  SaveBar,
  UnsavedGuard,
  useDraft,
} from "../../shell/components";
import { Icon } from "../../shell/icons";
import {
  useCreateRoleMutation,
  useDeleteRoleMutation,
  usePermissionCatalogue,
  useRoles,
  useSetRolePermissionsMutation,
  useGlobalWriteReason,
} from "./api";
import { FOLDING_FORM_GRID } from "./formGrid";
import { ReadOnlyNotice } from "./ReadOnlyNotice";
import { isForbidden } from "./settingsState";
import { assignableRoles, groupPermissionsByModule, roleDisplayName } from "./types";
import type { RoleOut } from "./types";

export const ROLES_READ = "roles.read";
export const ROLES_MANAGE = "roles.manage";

/**
 * One module's cell of the matrix: a fieldset on the control edge and radius,
 * with the module name as its legend. There is no kit piece for a grouped
 * checkbox set, so the two declarations are the design's own control tokens.
 */
const GROUP: CSSProperties = { border: "1px solid var(--line)", borderRadius: "var(--r-ctl)", padding: "8px 12px 10px", margin: 0 };
const GROUP_LEGEND: CSSProperties = { fontWeight: 600, padding: "0 4px" };

export function RolesTab({ canRead, canManage }: { canRead: boolean; canManage: boolean }) {
  const t = useT();
  const roles = useRoles(canRead);
  const catalogue = usePermissionCatalogue(canRead);
  const stepUp = useStepUp();
  const createRole = useCreateRoleMutation(stepUp.run);
  const setPermissions = useSetRolePermissionsMutation(stepUp.run);
  const deleteRole = useDeleteRoleMutation(stepUp.run);

  const [editingId, setEditingId] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [newDescription, setNewDescription] = useState("");
  const [pendingDelete, setPendingDelete] = useState<RoleOut | null>(null);
  /** Set when a save came back under a DIFFERENT id — the clone in spec §1.3. */
  const [clonedFrom, setClonedFrom] = useState<string | null>(null);

  const listed = useMemo(() => assignableRoles(roles.data?.roles), [roles.data]);
  const groups = useMemo(
    () => groupPermissionsByModule(catalogue.data?.permissions),
    [catalogue.data],
  );

  const editing = listed.find((role) => role.id === editingId) ?? null;
  // THE MATRIX IS THE KIT'S DRAFT (redesign, Receta FO): the role's codes as
  // the server holds them are the saved state, sorted because the body is a
  // set; a save's answer (the role re-read, or its copy) becomes it.
  const { draft, setDraft, dirty, discard, markSent } = useDraft<string[]>(
    editing ? [...editing.codes].sort() : undefined,
  );
  const checked: ReadonlySet<string> = new Set(draft ?? []);
  // A create refused for a missing name, held as data and worded at render.
  const [createAttempt, setCreateAttempt] = useState<Attempt | null>(null);
  const createProblems = attemptProblems(createAttempt);
  const forbidden = isForbidden(roles.error);
  const loading = roles.isLoading || catalogue.isLoading;
  const reason = useGlobalWriteReason(ROLES_MANAGE);

  function openEditor(role: RoleOut) {
    setEditingId(role.id);
    // Another role's codes, not the last role's edits: opening a role starts
    // from what the server holds for it.
    setDraft([...role.codes].sort());
    setClonedFrom(null);
    setPermissions.reset();
  }

  function toggle(code: string) {
    const next = new Set(checked);
    if (next.has(code)) next.delete(code);
    else next.add(code);
    setDraft([...next].sort());
  }

  function saveMatrix(role: RoleOut) {
    // Sorted, because the body is a SET and a stable order makes the audit
    // record and the diff between two saves readable.
    const codes = [...checked].sort();
    markSent(codes);
    setPermissions.mutate(
      { roleId: role.id, codes },
      {
        onSuccess: (result) => {
          // The server copied a platform-wide role into this org and applied
          // the edit to the copy. Say so, and follow the id.
          if (result.role_id !== role.id) {
            setClonedFrom(role.name);
            setEditingId(result.role_id);
          }
        },
        onError: () => markSent(null),
      },
    );
  }

  function createNewRole() {
    // A missing name is said on submit, never by a dead button (the kit's rule).
    const found: Attempt = {
      rules: () => [{ name: "name", label: t("settings.roles.create.name") }],
      values: { name: newName },
    };
    setCreateAttempt(found);
    if (attemptProblems(found).length > 0) return;
    createRole.mutate(
      { name: newName.trim(), description: newDescription.trim(), codes: [] },
      {
        onSuccess: () => {
          setNewName("");
          setNewDescription("");
          setCreateAttempt(null);
        },
      },
    );
  }

  // ONE GUARD FOR THE TAB (redesign, Receta FO; decision D16): the matrix's
  // change, or a role half typed below it, asks before the tab is left.
  const guardDirty = dirty || newName.trim() !== "" || newDescription.trim() !== "";

  if (forbidden) {
    return (
      <Panel title={t("settings.roles.title")}>
        <p className="note">
          {t("settings.roles.forbidden", { code: ROLES_READ })}
        </p>
      </Panel>
    );
  }

  return (
    <>
      <UnsavedGuard dirty={guardDirty} search={["tab"]} />
      {!canManage ? <ReadOnlyNotice reason={reason} /> : null}
      {clonedFrom ? (
        <p role="status" className="notice" style={{ marginBottom: 16 }}>
          <Icon name={KIND_ICON.info} size={16} />
          <span>
            {t("settings.roles.copied", { name: clonedFrom })}
          </span>
        </p>
      ) : null}

      <Panel
        title={t("settings.roles.title")}
        subtitle={t("settings.roles.subtitle")}
        bare
      >
        <QueryState
          loading={loading}
          error={roles.error ?? catalogue.error}
          empty={listed.length === 0}
          emptyLabel={t("settings.roles.empty")}
          emptyIcon="certificate"
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("settings.roles.columns.role")}</Th>
                {/* Not "Scope" (language pass, 2026-09-11,
                    `docs/design/settings-language.md`): "scope" is the
                    correct word inside the system and the wrong word on
                    screen. The column answers "does editing this touch
                    another organization" -- "Belongs to" names that
                    decision; the two values are unchanged. */}
                <Th>{t("settings.roles.columns.belongsTo")}</Th>
                <Th>{t("settings.roles.columns.users")}</Th>
                <Th>{t("settings.roles.columns.permissions")}</Th>
                <Th>{""}</Th>
              </tr>
            </thead>
            <tbody>
              {listed.map((role) => (
                <tr key={role.id}>
                  <Td lead={<Icon name="certificate" size={18} />}>
                    {/* Humanized, same transform as the directory chip
                        (`roleDisplayName`, `./types.ts`) -- "org-admin" reads
                        "Org admin" here too, not the raw wire value. The
                        wire value is unchanged: `role.name` still goes out
                        on every mutation, this only touches what is drawn. */}
                    <div className="cell-title">{roleDisplayName(role.name)}</div>
                    <div className="cell-sub">{role.description}</div>
                  </Td>
                  <Td>{role.is_global ? t("settings.roles.platformWide") : t("settings.roles.thisOrganization")}</Td>
                  <Td>
                    {/* A count, not an identifier (language pass, 2026-09-11):
                        tabular figures, not the code font. */}
                    <span className="count">{role.assigned_users}</span>
                  </Td>
                  <Td>
                    <span className="count">{role.codes.length}</span>
                  </Td>
                  <Td>
                    <div className="flex flex-wrap gap-2">
                      <Button
                        type="button"
                        variant="outline"
                        aria-label={t("settings.roles.editAria", { name: role.name })}
                        onClick={() => openEditor(role)}
                      >
                        {t("settings.roles.editPermissions")}
                      </Button>
                      <Button
                        type="button"
                        variant="outline"
                        className="danger"
                        aria-label={t("settings.roles.deleteAria", { name: role.name })}
                        disabled={!canManage || role.is_global}
                        title={
                          role.is_global
                            ? t("settings.roles.globalNoDelete")
                            : canManage
                              ? undefined
                              : reason
                        }
                        onClick={() => setPendingDelete(role)}
                      >
                        {t("settings.roles.delete")}
                      </Button>
                    </div>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </QueryState>
      </Panel>

      {editing ? (
        <FormSection title={t("settings.roles.permissionsFor", { name: editing.name })} icon="shield-lock">
          <p className="note">{t("settings.roles.permissionsSubtitle")}</p>
          <p className="note" style={{ marginTop: 6 }}>
            {t("settings.roles.holders", { count: editing.assigned_users, name: editing.name })}
          </p>
          {editing.is_global ? (
            <p className="note" style={{ marginTop: 6 }}>
              {t("settings.roles.globalNote", { name: editing.name })}
            </p>
          ) : null}
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3" style={{ marginTop: 14 }}>
            {groups.map((group) => (
              <fieldset key={group.module} style={GROUP}>
                <legend style={GROUP_LEGEND}>{group.module}</legend>
                {group.permissions.map((permission) => (
                  <div key={permission.code} style={{ marginTop: 6 }}>
                    <label className="flex items-start gap-2">
                      <input
                        type="checkbox"
                        checked={checked.has(permission.code)}
                        disabled={!canManage}
                        aria-describedby={`describe-${permission.code}`}
                        onChange={() => toggle(permission.code)}
                      />
                      <span className="mono">{permission.code}</span>
                    </label>
                    <div id={`describe-${permission.code}`} className="note" style={{ marginLeft: 24 }}>
                      {permission.description}
                    </div>
                  </div>
                ))}
              </fieldset>
            ))}
          </div>
        </FormSection>
      ) : null}

      {/* CREATING A ROLE IS AN ACT, NOT A SETTING: its own section and its
          own button, outside the matrix's draft and bar. */}
      <FormSection title={t("settings.roles.create.title")} icon="plus">
        <p className="note" style={{ marginBottom: 12 }}>
          {t("settings.roles.create.subtitle")}
        </p>
        <div className="form-grid" style={FOLDING_FORM_GRID}>
          <Field label={t("settings.roles.create.name")}>
            <input
              className={inputClass}
              value={newName}
              disabled={!canManage}
              onChange={(event) => setNewName(event.target.value)}
            />
          </Field>
          <Field label={t("settings.roles.create.description")}>
            <input
              className={inputClass}
              value={newDescription}
              disabled={!canManage}
              onChange={(event) => setNewDescription(event.target.value)}
            />
          </Field>
        </div>
        <div className="form-actions">
          <Btn
            label={createRole.isPending ? t("settings.roles.create.creating") : t("settings.roles.create.submit")}
            icon="plus"
            // Disabled only for "you may not" and "not right now", with the
            // reason beside it; a missing name is said on submit.
            disabled={!canManage || createRole.isPending}
            onClick={createNewRole}
          />
          {!canManage ? <span className="form-hint">{reason}</span> : null}
          {createRole.error ? (
            <span role="alert" className="form-error">
              <Icon name={KIND_ICON.crit} size={13} />
              {describeSubmitError(createRole.error)}
            </span>
          ) : createProblems.length > 0 ? (
            <span role="alert" className="form-error">
              <Icon name={KIND_ICON.crit} size={13} />
              {createProblems.map((problem) => problem.message).join(". ")}.
            </span>
          ) : null}
        </div>
      </FormSection>

      {/* THE MATRIX'S ONE BAR (decision D22), drawn while a role's
          permissions hold a change. */}
      {editing ? (
        <SaveBar
          dirty={dirty}
          saving={setPermissions.isPending}
          error={setPermissions.error}
          saved={setPermissions.isSuccess && !dirty}
          onDiscard={() => {
            discard();
            setPermissions.reset();
          }}
          onSave={() => saveMatrix(editing)}
        />
      ) : null}

      <Dialog
        open={pendingDelete != null}
        onClose={() => setPendingDelete(null)}
        title={t("settings.roles.deleteDialog.title")}
        dismissible={!stepUp.open}
      >
        <p>
          {t("settings.roles.deleteDialog.body", {
            name: pendingDelete?.name ?? "",
            count: pendingDelete?.assigned_users ?? 0,
          })}
        </p>
        {deleteRole.error ? (
          <p role="alert" className="notice red" style={{ marginTop: 12 }}>
            <Icon name={KIND_ICON.crit} size={16} />
            <span>{describeSubmitError(deleteRole.error)}</span>
          </p>
        ) : null}
        <div className="form-actions">
          <Button
            type="button"
            variant="outline"
            className="danger"
            disabled={deleteRole.isPending}
            onClick={() => {
              const target = pendingDelete;
              if (!target) return;
              deleteRole.mutate(target.id, {
                onSuccess: () => {
                  if (editingId === target.id) setEditingId(null);
                  setPendingDelete(null);
                },
              });
            }}
          >
            {t("settings.roles.deleteDialog.confirm", { name: pendingDelete?.name ?? "" })}
          </Button>
          <Button type="button" variant="outline" onClick={() => setPendingDelete(null)}>
            {t("common.actions.cancel")}
          </Button>
        </div>
      </Dialog>

      {/* Outside every form: the recent confirmation holds forms of its own. */}
      {stepUp.dialog}
    </>
  );
}
