/**
 * Where one health profile applies, and the console's way to apply it: as a
 * client's default or as the organisation's (MSP clients phase 3, design
 * §5.1; spec §0.2's levels: device, then group, then client, then
 * organisation).
 *
 * A CLIENT DEFAULT needs `health.admin` IN THAT CLIENT. The client is the one
 * chosen above, or, with «Todos mis clientes», the one chosen here from the
 * clients where the person holds the code (`useNewRowClient`, design §3.1).
 * «Sin asignar» is never offered, and the server refuses it (422).
 *
 * THE ORGANISATION DEFAULT is a global row: `health.admin` on every client,
 * and no client chosen above, or the request would be narrowed.
 *
 * Device and group assignments are not made here; they stay with the API and
 * MCP, as before this phase. Every assignment is listed, and removed where the
 * person may. Every check is the server's (invariant 6).
 */
import { useState } from "react";

import { ClientField } from "../../components/ClientField";
import { ConfirmDialog } from "../../components/confirm";
import { Form, FormActions, attemptProblems, describeSubmitError } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Field, Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { useClientScope } from "../../lib/clientScope";
import { useNewRowClient } from "../../lib/clientSelection";
import { useEverywherePermissions } from "../../lib/mePermissions";
import { Btn, Panel, QueryState } from "../../shell/components";
import { useClientName } from "../settings/api";
import { useCreateHealthAssignmentMutation, useDeleteHealthAssignmentMutation, useHealthAssignments } from "./api";
import type { AssignmentOut } from "./types";

export const HEALTH_ADMIN = "health.admin";

type Target = "client" | "org";

const clientRules = (): readonly FieldRule[] => [
  { name: "client", label: translate("health.assignments.client") },
];

/** Where an assignment applies, in words. */
export function describeAssignmentTarget(
  assignment: Pick<AssignmentOut, "level" | "client_id" | "device_id" | "device_group_id">,
  clientName: (clientId: string) => string,
): string {
  switch (assignment.level) {
    case "org":
      return translate("health.assignments.levelOrg");
    case "client":
      return translate("health.assignments.levelClient", { client: clientName(assignment.client_id ?? "") });
    case "group":
      return translate("health.assignments.levelGroup", { group: assignment.device_group_id ?? "" });
    case "device":
      return translate("health.assignments.levelDevice", { device: assignment.device_id ?? "" });
  }
}

export function ProfileAssignments({ profileId, canAdmin }: { profileId: string; canAdmin: boolean }) {
  const t = useT();
  const assignments = useHealthAssignments(profileId);
  const create = useCreateHealthAssignmentMutation();
  const remove = useDeleteHealthAssignmentMutation();
  const everywhere = useEverywherePermissions();
  const { selected, clients } = useClientScope();
  const clientName = useClientName();
  // The clients where the person holds health.admin, from `/me.clients`.
  // `canAdmin` (health.admin on EVERY client, since Task 8) decides only the
  // organisation default: a client's default is a write in that client.
  const newRow = useNewRowClient(HEALTH_ADMIN);
  const administers = (clientId: string | null | undefined): boolean =>
    newRow.options.some((option) => option.id === clientId);
  const orgAllowed = canAdmin && selected === null && everywhere.has(HEALTH_ADMIN);
  // The client chosen above, where the person holds the code; or, with every
  // client, a Client field with at least one client to offer.
  const clientAllowed =
    newRow.clientId !== null ? administers(newRow.clientId) : newRow.needsField && newRow.options.length > 0;
  const [target, setTarget] = useState<Target>("client");
  // `ClientField`'s value: a client id, or `""` for none chosen yet.
  const [chosen, setChosen] = useState("");
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [removing, setRemoving] = useState<AssignmentOut | null>(null);
  // What Assign sends: the choice only while both are offered. A choice of the
  // organisation default made before `/me` stopped offering it is not sent.
  const effective: Target = !orgAllowed ? "client" : !clientAllowed ? "org" : target;
  const client = newRow.clientId ?? (chosen || null);
  const rows = assignments.data ?? [];

  // The organisation default with every client; a client's default where its
  // client is one the person administers; a device's or a group's where the
  // person holds health.admin in its client (the delete route's own check).
  const mayRemove = (assignment: AssignmentOut): boolean =>
    assignment.level === "org"
      ? orgAllowed
      : assignment.level === "client"
        ? administers(assignment.client_id)
        : clients.some((item) => item.id === assignment.client_id && item.permissions.includes(HEALTH_ADMIN));

  function submit() {
    create.reset();
    if (effective === "org") {
      setAttempt(null);
      create.mutate({ profile_id: profileId });
      return;
    }
    const next: Attempt = { rules: clientRules, values: { client: client ?? "" } };
    setAttempt(next);
    if (attemptProblems(next).length > 0 || client === null) return;
    create.mutate({ profile_id: profileId, client_id: client });
  }

  return (
    <Panel title={t("health.assignments.title")} subtitle={t("health.assignments.subtitle")}>
      <QueryState
        loading={assignments.isLoading}
        error={assignments.error}
        empty={rows.length === 0}
        emptyLabel={t("health.assignments.empty")}
        emptyIcon="health"
      >
        <Table label={t("health.assignments.tableLabel")}>
          <thead>
            <tr>
              <Th>{t("health.assignments.colTarget")}</Th>
              <Th />
            </tr>
          </thead>
          <tbody>
            {rows.map((assignment) => {
              const where = describeAssignmentTarget(assignment, clientName);
              return (
                <tr key={assignment.id}>
                  <Td>{where}</Td>
                  <Td>
                    {mayRemove(assignment) ? (
                      <Btn
                        small
                        variant="danger"
                        label={t("health.assignments.remove")}
                        ariaLabel={t("health.assignments.removeAria", { target: where })}
                        onClick={() => {
                          remove.reset();
                          setRemoving(assignment);
                        }}
                      />
                    ) : null}
                  </Td>
                </tr>
              );
            })}
          </tbody>
        </Table>
      </QueryState>
      {remove.isError ? (
        <p role="alert" className="form-error">
          {t("health.assignments.removeFailed")} {describeSubmitError(remove.error)}
        </p>
      ) : null}

      {clientAllowed || orgAllowed ? (
        <Form onSubmit={submit}>
          <div className="form-grid">
            {clientAllowed && orgAllowed ? (
              <Field label={t("health.assignments.target")}>
                <select
                  className="select-field"
                  value={target}
                  onChange={(event) => setTarget(event.target.value === "org" ? "org" : "client")}
                >
                  <option value="client">{t("health.assignments.targetClient")}</option>
                  <option value="org">{t("health.assignments.targetOrg")}</option>
                </select>
              </Field>
            ) : null}
            {effective === "client" ? (
              newRow.needsField ? (
                <ClientField code={HEALTH_ADMIN} value={chosen} onChange={setChosen} />
              ) : (
                <p className="note">
                  {t("health.assignments.forClient", { client: clientName(newRow.clientId ?? "") })}
                </p>
              )
            ) : null}
          </div>
          <FormActions
            label={t("health.assignments.assign")}
            pendingLabel={t("health.assignments.assigning")}
            pending={create.isPending}
            error={create.error}
            failedMessage={t("health.assignments.assignFailed")}
            problems={attemptProblems(attempt)}
          />
        </Form>
      ) : null}

      <ConfirmDialog
        open={removing !== null}
        title={t("health.assignments.removeTitle")}
        subject={removing ? describeAssignmentTarget(removing, clientName) : ""}
        body={t("health.assignments.removeBody")}
        confirmLabel={t("health.assignments.removeConfirm")}
        destructive
        onCancel={() => setRemoving(null)}
        onConfirm={() => {
          const assignment = removing;
          setRemoving(null);
          if (assignment) remove.mutate(assignment.id);
        }}
      />
    </Panel>
  );
}
