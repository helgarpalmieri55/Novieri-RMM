/**
 * Which devices are protected (spec-server-backup.md §8.3).
 *
 * Reads the `backup-coverage` report, whose verdicts are decided server-side:
 * a device with no enabled policy is never "Protected", a failed or missed
 * last run is "Failing", and with off-site copying on a device is only
 * protected once a recent backup reached the off-site copy. The totals are the
 * server's, over every device; this page only filters the rows it shows.
 */

import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { formatDateTime } from "../../lib/datetime";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import {
  FilterBar,
  PageHead,
  Panel,
  QueryState,
  StatCard,
  StatsGrid,
  StatusPill,
} from "../../shell/components";
import { useBackupCoverage } from "./api";
import {
  COVERAGE_LABEL,
  OFFSITE_LABEL,
  coverageKind,
  notCrashConsistent,
} from "./backupState";
import { BackupPageNav } from "./backupNav";
import { BackupReadiness } from "./BackupReadiness";

const VERDICTS = ["covered", "stale", "failing", "unprotected"] as const;

function text(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

export function CoveragePage() {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const canRead = (me.data?.permissions ?? []).includes("backup.read");
  const zone = me.data?.timezone;
  const [params, setParams] = useSearchParams();
  const filter = params.get("coverage") ?? "";
  const coverage = useBackupCoverage(filter);
  const totals = coverage.data?.totals ?? {};
  const rows = coverage.data?.rows ?? [];

  const when = (value: unknown) => {
    const stamp = text(value);
    return stamp ? formatDateTime(stamp, zone) : t("backup.coverage.never");
  };

  return (
    <div>
      <PageHead
        title={t("backup.coverage.title")}
        description={t("backup.coverage.description")}
      />
      <BackupPageNav canRead={canRead} />
      <BackupReadiness />

      <StatsGrid columns={4}>
        <StatCard
          label={t("backup.coverage.tileProtected")}
          value={String(totals.covered ?? 0)}
          icon="circle-check"
          to="/backup/coverage?coverage=covered"
          tone="green"
          zeroLabel={t("backup.coverage.noneRecent")}
        />
        <StatCard
          label={t("backup.coverage.tileStale")}
          value={String(totals.stale ?? 0)}
          icon="clock"
          to="/backup/coverage?coverage=stale"
          tone="gold"
          zeroLabel={t("backup.coverage.noneBehind")}
        />
        <StatCard
          label={t("backup.coverage.tileFailing")}
          value={String(totals.failing ?? 0)}
          icon="alert-triangle"
          to="/backup/coverage?coverage=failing"
          tone="red"
          urgent={(totals.failing ?? 0) > 0}
          zeroLabel={t("backup.coverage.noneFailing")}
        />
        <StatCard
          label={t("backup.coverage.tileUnprotected")}
          value={String(totals.unprotected ?? 0)}
          icon="minus"
          to="/backup/coverage?coverage=unprotected"
          tone="red"
          zeroLabel={t("backup.coverage.everyPolicy")}
        />
      </StatsGrid>

      <Panel bare>
        <FilterBar>
          <select
            className="select-field"
            value={filter}
            aria-label={t("backup.coverage.filterAria")}
            onChange={(e) => {
              const next = new URLSearchParams(params);
              if (e.target.value === "") next.delete("coverage");
              else next.set("coverage", e.target.value);
              setParams(next);
            }}
          >
            <option value="">{t("backup.coverage.allDevices")}</option>
            {VERDICTS.map((v) => (
              <option key={v} value={v}>
                {COVERAGE_LABEL[v]}
              </option>
            ))}
          </select>
          <span className="filter-spacer" />
        </FilterBar>
        <QueryState
          loading={coverage.isLoading}
          error={coverage.error}
          empty={rows.length === 0}
          emptyLabel={filter ? t("backup.coverage.emptyFiltered") : t("backup.coverage.emptyNone")}
          emptyIcon="backup"
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("backup.coverage.colDevice")}</Th>
                <Th>{t("backup.coverage.colProtection")}</Th>
                <Th>{t("backup.coverage.colLastBackup")}</Th>
                <Th>{t("backup.coverage.colOffsite")}</Th>
                <Th>{t("backup.coverage.colLastRestore")}</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const verdict = String(row.coverage ?? "");
                const offsite = String(row.offsite ?? "off");
                return (
                  <tr key={String(row.device_id)}>
                    <Td>
                      <div className="cell-title">{String(row.hostname ?? "")}</div>
                      <div className="cell-sub">
                        {text(row.repository_name) ?? t("backup.coverage.noPolicy")}
                      </div>
                    </Td>
                    <Td>
                      <StatusPill
                        value={verdict}
                        kind={coverageKind(verdict)}
                        label={COVERAGE_LABEL[verdict] ?? verdict}
                      />
                      {row.crash_consistent === false ? (
                        <div className="cell-sub">{notCrashConsistent()}</div>
                      ) : null}
                    </Td>
                    <Td>{when(row.last_point_taken_at)}</Td>
                    <Td>
                      {OFFSITE_LABEL[offsite] ?? offsite}
                      {offsite !== "off" ? (
                        <div className="cell-sub">{when(row.last_offsite_at)}</div>
                      ) : null}
                    </Td>
                    <Td>{when(row.last_restore_tested_at)}</Td>
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
