/**
 * Budget, Continuity and Changes, switched on and off per client
 * (`PATCH /clients/{id}` with `enabled_modules`, `clients.manage` on this
 * client). A module that is off is a 404 on every write for this client
 * (`require_module`, phase 2), and the menu follows `/me.clients` (Task 8),
 * which this refreshes.
 */
import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import type { ClientOut } from "../../lib/api";
import { useClientScope } from "../../lib/clientScope";
import { Panel, Toggle } from "../../shell/components";
import { useUpdateClientMutation } from "./api";
import { OPTIONAL_MODULES, moduleHint, moduleLabel } from "./labels";

export function ModulesTab({ client }: { client: ClientOut }) {
  const t = useT();
  const { clients } = useClientScope();
  const mine = clients.find((item) => item.id === client.id);
  const canManage =
    client.status === "active" &&
    client.kind !== "unassigned" &&
    (mine?.permissions.includes("clients.manage") ?? false);
  const update = useUpdateClientMutation(client.id);
  const enabled = new Set(client.enabled_modules);

  return (
    <Panel title={t("clients.modules.title")} subtitle={t("clients.modules.subtitle")}>
      {client.kind === "unassigned" ? <p className="note">{t("clients.modules.unassigned")}</p> : null}
      <div className="flex flex-col gap-3">
        {OPTIONAL_MODULES.map((module) => (
          <div key={module}>
            <Toggle
              checked={enabled.has(module)}
              disabled={!canManage || update.isPending}
              label={moduleLabel(module)}
              onChange={(next) => {
                update.reset();
                update.mutate({
                  enabled_modules: OPTIONAL_MODULES.filter((item) => (item === module ? next : enabled.has(item))),
                });
              }}
            />
            <p className="note">{moduleHint(module)}</p>
          </div>
        ))}
      </div>
      {update.isError ? (
        <p role="alert" className="form-error">
          {t("clients.modules.saveFailed")} {describeSubmitError(update.error)}
        </p>
      ) : null}
    </Panel>
  );
}
