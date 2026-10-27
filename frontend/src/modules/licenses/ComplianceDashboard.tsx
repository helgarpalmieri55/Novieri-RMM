import { useQuery } from "@tanstack/react-query";
import { Table, Td, Th } from "../../components/ui";
import type { Me } from "../../lib/api";
import { useT } from "../../i18n/context";
import { useApi } from "../../lib/ApiContext";
import { useClientName } from "../settings/api";
import { useCompliance } from "./api";
import { LicenseReadiness } from "./LicenseReadiness";
import { LicensePageNav } from "./licenseNav";
import { complianceKind } from "./licenseState";
import { PageHead, Panel, Progress, QueryState, StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";

export function ComplianceDashboard() {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const canRead = (me.data?.permissions ?? []).includes("licenses.read");

  const compliance = useCompliance();
  const clientName = useClientName();
  const items = compliance.data ?? [];

  return (
    <div>
      <PageHead
        title={t("licenses.compliance.title")}
        description={t("licenses.compliance.description")}
      />

      <LicensePageNav canRead={canRead} />

      <LicenseReadiness />

      <Panel bare>
        <QueryState
          loading={compliance.isLoading}
          error={compliance.error}
          empty={items.length === 0}
          emptyLabel={t("licenses.compliance.empty")}
          emptyIcon="license"
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("licenses.compliance.colProduct")}</Th>
                <Th>{t("licenses.compliance.colClient")}</Th>
                <Th>{t("licenses.compliance.colUsedOwned")}</Th>
                <Th>{t("licenses.compliance.colEntitled")}</Th>
                <Th>{t("licenses.compliance.colInstalled")}</Th>
                <Th>{t("licenses.compliance.colDifference")}</Th>
                <Th>{t("licenses.compliance.colStatus")}</Th>
              </tr>
            </thead>
            <tbody>
              {items.map((row) => {
                const kind = complianceKind(row.breach);
                // The bar is the fraction of the entitlement in use, capped at
                // full so an over-deployment reads as a full red bar and the
                // pill beside it says by how much. Nothing entitled is not a
                // fraction, so the bar is absent rather than at zero.
                const pct = row.entitled > 0 ? Math.min(100, (row.installed / row.entitled) * 100) : null;
                const over = row.installed - row.entitled;
                // One row per (client, product): a client's licenses against
                // its own computers, never summed with another client's.
                return (
                  <tr key={`${row.client_id}-${row.product_id}`}>
                    <Td lead={<Icon name="license" size={18} />} gutter={kind}>
                      <div className="cell-title">{row.name}</div>
                    </Td>
                    <Td>{clientName(row.client_id)}</Td>
                    <Td>
                      <div style={{ display: "grid", gap: 4, minWidth: 140 }}>
                        <span className="mono">{`${row.installed} / ${row.entitled}`}</span>
                        {pct === null ? null : (
                          <Progress
                            value={pct}
                            tone={row.breach ? "red" : "green"}
                            label={t("licenses.compliance.barLabel", { name: row.name, installed: row.installed, entitled: row.entitled })}
                          />
                        )}
                      </div>
                    </Td>
                    <Td>
                      <span className="mono">{row.entitled}</span>
                    </Td>
                    <Td>
                      <span className="mono">{row.installed}</span>
                    </Td>
                    <Td>
                      <span className="mono">{row.delta}</span>
                    </Td>
                    <Td>
                      <StatusPill
                        value={row.breach ? "over_deployed" : "compliant"}
                        kind={kind}
                        label={row.breach ? t("licenses.compliance.over", { count: over }) : t("licenses.compliance.free", { count: -over })}
                      />
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        </QueryState>
      </Panel>
    </div>
  );
}
