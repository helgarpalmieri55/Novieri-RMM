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

import { useMemo, useState, type CSSProperties } from "react";

import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import { Button, Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { Btn, KIND_ICON, Panel, QueryState } from "../../shell/components";
import { Icon } from "../../shell/icons";
import {
  useCreateRoleMutation,
  useDeleteRoleMutation,
  usePermissionCatalogue,
  useRoles,
  useSetRolePermissionsMutation,
} from "./api";
import { FOLDING_FORM_GRID } from "./formGrid";
import { SaveBar } from "./SaveBar";
import { isForbidden, permissionReason } from "./settingsState";
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
  const createRole = useCreateRoleMutation();
  const setPermissions = useSetRolePermissionsMutation();
  const deleteRole = useDeleteRoleMutation();

  const [editingId, setEditingId] = useState<string | null>(null);
  const [checked, setChecked] = useState<ReadonlySet<string>>(new Set());
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
  const forbidden = isForbidden(roles.error);
  const loading = roles.isLoading || catalogue.isLoading;
  const reason = permissionReason(ROLES_MANAGE);

  function openEditor(role: RoleOut) {
    setEditingId(role.id);
    setChecked(new Set(role.codes));
    setClonedFrom(null);
    setPermissions.reset();
  }

  function toggle(code: string) {
    setChecked((current) => {
      const next = new Set(current);
      if (next.has(code)) next.delete(code);
      else next.add(code);
      return next;
    });
  }

  function saveMatrix(role: RoleOut) {
    // Sorted, because the body is a SET and a stable order makes the audit
    // record and the diff between two saves readable.
    const codes = [...checked].sort();
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
      },
    );
  }

  const dirty =
    editing != null &&
    (editing.codes.length !== checked.size || editing.codes.some((code) => !checked.has(code)));

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
        <Panel
          title={t("settings.roles.permissionsFor", { name: editing.name })}
          subtitle={t("settings.roles.permissionsSubtitle")}
        >
          <p className="note">
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

          <SaveBar
            label={t("settings.roles.savePermissions")}
            canEdit={canManage}
            disabledReason={reason}
            dirty={dirty}
            pending={setPermissions.isPending}
            error={setPermissions.error}
            saved={setPermissions.isSuccess && !dirty}
            onSave={() => saveMatrix(editing)}
          />
        </Panel>
      ) : null}

      <Panel
        title={t("settings.roles.create.title")}
        subtitle={t("settings.roles.create.subtitle")}
      >
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
            variant="primary"
            disabled={!canManage || newName.trim() === "" || createRole.isPending}
            onClick={() =>
              createRole.mutate(
                { name: newName.trim(), description: newDescription.trim(), codes: [] },
                {
                  onSuccess: () => {
                    setNewName("");
                    setNewDescription("");
                  },
                },
              )
            }
          />
          {!canManage ? <span className="form-hint">{reason}</span> : null}
          {createRole.error ? (
            <span role="alert" className="form-error">
              <Icon name={KIND_ICON.crit} size={13} />
              {describeSubmitError(createRole.error)}
            </span>
          ) : null}
        </div>
      </Panel>

      <Dialog
        open={pendingDelete != null}
        onClose={() => setPendingDelete(null)}
        title={t("settings.roles.deleteDialog.title")}
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
    </>
  );
}
