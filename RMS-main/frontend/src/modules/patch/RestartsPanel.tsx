import { Link } from "react-router-dom";
import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { formatDateTime } from "../../lib/datetime";
import { Panel, QueryState, StatusPill } from "../../shell/components";
import { usePatchRestarts } from "./api";
import { restartLabel, restartSentence } from "./restartCopy";

/**
 * Machines still owed a restart after patching, across every platform.
 *
 * A restart is requested only inside the device's maintenance window; a
 * signed-in user on a laptop gets the policy's deferral as warning. This list
 * is where an operator sees which machines are half-patched until then.
 */
export function RestartsPanel({ zone }: { zone: string | null }) {
  const t = useT();
  const restarts = usePatchRestarts();
  const items = restarts.data?.items ?? [];
  return (
    <Panel
      title={t("patch.restarts.title")}
      subtitle={items.length > 0 ? t("patch.restarts.waiting", { count: items.length }) : undefined}
      bare
    >
      <QueryState
        loading={restarts.isLoading}
        error={restarts.error}
        empty={restarts.data != null && items.length === 0}
        emptyIcon="refresh"
        emptyLabel={t("patch.restarts.empty")}
      >
        <Table>
          <thead>
            <tr>
              <Th>{t("patch.restarts.machine")}</Th>
              <Th>{t("patch.restarts.status")}</Th>
              <Th>{t("patch.restarts.next")}</Th>
              <Th>{t("patch.restarts.since")}</Th>
            </tr>
          </thead>
          <tbody>
            {items.map((row) => {
              const { label, kind } = restartLabel(row.state);
              return (
                <tr key={row.id}>
                  <Td>
                    <Link to={`/devices/${row.device_id}`}>{row.hostname ?? t("patch.restarts.unnamed")}</Link>
                  </Td>
                  <Td>
                    <StatusPill value={row.state} kind={kind} label={label} />
                  </Td>
                  <Td>{restartSentence(row, zone)}</Td>
                  <Td>{formatDateTime(row.created_at, zone)}</Td>
                </tr>
              );
            })}
          </tbody>
        </Table>
      </QueryState>
    </Panel>
  );
}
