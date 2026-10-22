import { useState } from "react";
import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { Button, Field, inputClass, Table, Td, Th } from "../../components/ui";
import { Btn, Panel, QueryState, Toggle } from "../../shell/components";
import {
  useChangeGates,
  useGateGroups,
  useRemoveChangeGateMutation,
  useSetChangeGateMutation,
} from "./api";
import type { ChangeGate } from "./types";

const EVERY_DEVICE = "";

/** What a gate holds back, in words. */
export function gateCoverage(gate: Pick<ChangeGate, "applies_to_patches" | "applies_to_deployments">): string {
  if (gate.applies_to_patches && gate.applies_to_deployments) {
    return translate("change.gates.patchesAndDeployments");
  }
  return translate(gate.applies_to_patches ? "change.gates.patches" : "change.gates.deployments");
}

/**
 * Where patch approvals and deployments need an approved change first.
 *
 * With no row here, an approval or deployment made without a change still gets
 * a change record written for it automatically, so the evidence exists.
 */
export function GatesPanel({ canManage }: { canManage: boolean }) {
  const t = useT();
  const gates = useChangeGates();
  const groups = useGateGroups(canManage);
  const setGate = useSetChangeGateMutation();
  const remove = useRemoveChangeGateMutation();
  const [groupId, setGroupId] = useState(EVERY_DEVICE);
  const [patches, setPatches] = useState(true);
  const [deployments, setDeployments] = useState(true);
  const items = gates.data ?? [];

  return (
    <div className="mt-4">
      <Panel
        title={t("change.gates.title")}
        subtitle={t("change.gates.subtitle")}
        bare
      >
        <QueryState
          loading={gates.isLoading}
          error={gates.error}
          empty={gates.data != null && items.length === 0}
          emptyIcon="lock"
          emptyLabel={t("change.gates.empty")}
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("change.gates.machines")}</Th>
                <Th>{t("change.gates.whatNeeds")}</Th>
                <Th>{""}</Th>
              </tr>
            </thead>
            <tbody>
              {items.map((gate) => (
                <tr key={gate.id}>
                  <Td>{gate.device_group_name ?? t("change.gates.everyDevice")}</Td>
                  <Td>{gateCoverage(gate)}</Td>
                  <Td>
                    {canManage && (
                      <div className="flex justify-end">
                        <Btn
                          label={t("change.gates.remove")}
                          icon="trash"
                          small
                          onClick={() => remove.mutate(gate.id)}
                        />
                      </div>
                    )}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </QueryState>
        {canManage && (
          <form
            className="grid gap-3 px-4.5 py-3"
            onSubmit={(event) => {
              event.preventDefault();
              setGate.mutate({
                device_group_id: groupId === EVERY_DEVICE ? null : groupId,
                applies_to_patches: patches,
                applies_to_deployments: deployments,
              });
            }}
          >
            <Field label={t("change.gates.machines")}>
              <select className={inputClass} value={groupId} onChange={(e) => setGroupId(e.target.value)}>
                <option value={EVERY_DEVICE}>{t("change.gates.everyDevice")}</option>
                {(groups.data ?? []).map((group) => (
                  <option key={group.id} value={group.id}>
                    {group.name}
                  </option>
                ))}
              </select>
            </Field>
            <Toggle checked={patches} onChange={setPatches} label={t("change.gates.patchesToggle")} />
            <Toggle checked={deployments} onChange={setDeployments} label={t("change.gates.deploymentsToggle")} />
            {setGate.error ? (
              <p role="alert" className="form-error">
                {describeSubmitError(setGate.error)}
              </p>
            ) : null}
            <div className="flex justify-end">
              <Button type="submit" disabled={setGate.isPending || !(patches || deployments)}>
                {t("change.gates.require")}
              </Button>
            </div>
          </form>
        )}
      </Panel>
    </div>
  );
}
