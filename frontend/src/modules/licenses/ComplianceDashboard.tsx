import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { NoValue } from "../../components/emptystate";
import { Table, Td, Th } from "../../components/ui";
import type { Me } from "../../lib/api";
import { useT } from "../../i18n/context";
import { useApi } from "../../lib/ApiContext";
import { useClientScope } from "../../lib/clientScope";
import { useFleetQuery } from "../../lib/detectors";
import { useClientName } from "../settings/api";
import { useCompliance, useProducts } from "./api";
import { LicenseReadiness } from "./LicenseReadiness";
import { LicensePageNav } from "./licenseNav";
import { complianceKind } from "./licenseState";
import {
  ClientFilter,
  ListEmpty,
  ListToolbar,
  PageHead,
  Panel,
  Progress,
  QueryState,
  ShowingCount,
  StatusPill,
  useUrlFilters,
} from "../../shell/components";
import { Icon } from "../../shell/icons";

export function ComplianceDashboard() {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const canRead = (me.data?.permissions ?? []).includes("licenses.read");

  // THE CLIENT CHIP (decision D6): the register's compliance tiles link here
  // with their client (`?client=`), so the page opens exactly the positions
  // the tile counted, and a person with every client shown can narrow it.
  const filters = useUrlFilters(["client"] as const);
  const compliance = useCompliance(filters.values.client);
  const clientName = useClientName();
  // WHOSE EACH ROW IS, with every client chosen (design §3.4); with one
  // chosen every row is that client's, and the column would only repeat it.
  const { selected, clients } = useClientScope();
  const showClient = selected === null;
  const items = compliance.data ?? [];

  // NO USAGE DATA IS NOT ZERO INSTALLED (The Absent Zero Rule; Task 10, 5.1).
  // An installed count is a measurement only where something could measure
  // it: a computer running the agent reports its software (the readiness
  // panel's own fleet read, cached), and a product has a match rule to count
  // installs by (the catalog this module already reads). Without either, the
  // report's 0 is "nothing looked", and a row saying "5 free" over it would
  // be a compliance claim nobody measured.
  const fleet = useFleetQuery();
  const products = useProducts();
  const noAgent = fleet.isSuccess && fleet.data.managed === 0;
  const unmatchable = new Set((products.data ?? []).filter((p) => p.match_rules.length === 0).map((p) => p.id));
  const noUsage = (productId: string) => noAgent || unmatchable.has(productId);

  // EACH FIGURE OPENS THE LIST THAT EXPLAINS IT: the register searched for
  // that product, and narrowed to the row's client when every client is
  // shown (the Client chip, `?client=`; only a client of this person's, so
  // the chip never drops it on arrival).
  const licensesOf = (row: (typeof items)[number]): string => {
    const query = new URLSearchParams({ q: row.name });
    if (showClient && clients.some((client) => client.id === row.client_id)) query.set("client", row.client_id);
    return `/licenses?${query.toString()}`;
  };

  return (
    <div>
      <PageHead
        title={t("licenses.compliance.title")}
        description={t("licenses.compliance.description")}
      />

      <LicensePageNav canRead={canRead} />

      <LicenseReadiness />

      <Panel bare>
        <ListToolbar
          chips={<ClientFilter filters={filters} />}
          end={compliance.data ? <ShowingCount shown={items.length} total={items.length} /> : null}
        />
        <QueryState loading={compliance.isPending} error={compliance.error} empty={false} emptyLabel="">
          {items.length === 0 ? (
            <ListEmpty
              filtered={filters.active}
              onClear={filters.clear}
              nothingYet={t("licenses.compliance.empty")}
              icon="license"
            />
          ) : (
          <Table label={t("licenses.compliance.title")}>
            <thead>
              <tr>
                <Th>{t("licenses.compliance.colProduct")}</Th>
                {showClient && <Th>{t("licenses.compliance.colClient")}</Th>}
                <Th>{t("licenses.compliance.colUsedOwned")}</Th>
                <Th>{t("licenses.compliance.colEntitled")}</Th>
                <Th>{t("licenses.compliance.colInstalled")}</Th>
                <Th>{t("licenses.compliance.colDifference")}</Th>
                <Th>{t("licenses.compliance.colStatus")}</Th>
              </tr>
            </thead>
            <tbody>
              {items.map((row) => {
                const unmeasured = noUsage(row.product_id);
                const kind = unmeasured ? "mut" : complianceKind(row.breach);
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
                    {showClient && <Td>{clientName(row.client_id)}</Td>}
                    <Td>
                      <div style={{ display: "grid", gap: 4, minWidth: 140 }}>
                        {/* The figure is the link, named for what it opens. */}
                        <Link
                          className={unmeasured ? undefined : "mono"}
                          to={licensesOf(row)}
                          title={t("licenses.compliance.figureLink", { name: row.name })}
                        >
                          {unmeasured
                            ? t("licenses.compliance.noUsageOwned", { count: row.entitled })
                            : `${row.installed} / ${row.entitled}`}
                        </Link>
                        {pct === null || unmeasured ? null : (
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
                      {unmeasured ? (
                        <NoValue label={t("licenses.compliance.noUsage")} />
                      ) : (
                        <span className="mono">{row.installed}</span>
                      )}
                    </Td>
                    <Td>
                      {unmeasured ? (
                        <NoValue label={t("licenses.compliance.noUsage")} />
                      ) : (
                        <span className="mono">{row.delta}</span>
                      )}
                    </Td>
                    <Td>
                      <StatusPill
                        value={unmeasured ? "no_usage_data" : row.breach ? "over_deployed" : "compliant"}
                        kind={kind}
                        label={
                          unmeasured
                            ? t("licenses.compliance.noUsageStatus")
                            : row.breach
                              ? t("licenses.compliance.over", { count: over })
                              : t("licenses.compliance.free", { count: -over })
                        }
                      />
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
          )}
        </QueryState>
      </Panel>
    </div>
  );
}
