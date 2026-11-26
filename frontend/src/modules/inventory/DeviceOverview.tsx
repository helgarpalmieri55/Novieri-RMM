/**
 * The device page's default tab (redesign plan Task 6, 1.2; the "atera 8"
 * capture): the record's information on the left -- what the machine is and
 * what it belongs to -- and what it reports and what happened to it on the
 * right: the latest reading of each check, recent jobs and inventory changes,
 * its backup and its open tickets, each linking to the tab or page that holds
 * the rest. Its alerts and updates are the two cards above the tabs
 * (`DeviceSummaryCards.tsx`).
 *
 * LATEST READINGS, NO TIME SERIES (operator decision 2026-11-24, Appendix E,
 * B1): the capture draws CPU and memory over 24 hours, and no route serves a
 * history; the series are another project (the agent's idle budget). So the
 * Checks panel says what each check last read and when, and nothing is drawn
 * that would have to be invented. A panel whose module this operator cannot
 * read is left out.
 */

import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { NoValue } from "../../components/emptystate";
import { RelTime } from "../../components/reltime";
import { useT } from "../../i18n/context";
import { useApi } from "../../lib/ApiContext";
import { humaniseEnum } from "../../lib/labels";
import { DetailGrid, FieldGroup, Panel, QueryState, StatusPill } from "../../shell/components";
import { KV } from "../../shell/viz";
import { CHECK_STATE_PRESENTATION, checkLabel } from "../health/healthState";
import { jobStatusKind } from "../jobs/jobState";
import { jobTypeLabel } from "../jobs/jobTypes";
import { ticketStatusLabel } from "../service-desk/serviceDeskState";
import { openTickets, type useDeviceSummary } from "./deviceSummary";

const SHOWN = 5;

/** A check's state as a pill kind: the alert console's colours, with the state's own words. */
const CHECK_KIND = { ok: "ok", breaching: "crit", unavailable: "warn", unknown: "mut" } as const;

export function DeviceOverview({
  deviceId,
  summary,
  permissions,
  zone,
  now,
  information,
  relations,
}: {
  deviceId: string;
  summary: ReturnType<typeof useDeviceSummary>;
  permissions: readonly string[];
  zone?: string | null;
  now: number;
  /** "Device information": what the machine is (the page builds it from the row and its inventory). */
  information: ReactNode;
  /** "Relations": its client, site, group and the person who uses it. */
  relations: ReactNode;
}) {
  const t = useT();
  const api = useApi();
  const can = (code: string) => permissions.includes(code);
  const here = `/devices/${deviceId}`;
  const { managed, health, backup, tickets } = summary;

  // The job list has no device filter (`JobListParams` is limit, offset and
  // order), so this is the first page, narrowed here -- the same source the
  // Jobs tab reads, and read only with `jobs.read`.
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api.listJobs(),
    enabled: can("jobs.read"),
  });
  const deviceJobs = (jobs.data?.items ?? []).filter((job) => job.device_id === deviceId).slice(0, SHOWN);
  // The Changes tab's own read, under its key: the newest inventory changes.
  const changes = useQuery({
    queryKey: ["inventory-changes", deviceId],
    queryFn: () => api.getDeviceChanges(deviceId),
  });
  const recentChanges = (changes.data?.items ?? []).slice(0, SHOWN);
  const checks = health.data?.checks ?? [];
  const open = openTickets(tickets.data?.items);

  return (
    <DetailGrid
      main={
        <>
          <Panel title={t("inventory.overview.information")}>{information}</Panel>
          <Panel title={t("inventory.overview.relations")}>{relations}</Panel>
        </>
      }
      aside={
        <>
          {managed ? (
            <Panel
              title={t("inventory.overview.checks")}
              actions={<Link to={`${here}?tab=health`}>{t("inventory.overview.allChecks")}</Link>}
            >
              <QueryState
                loading={health.isLoading}
                error={health.error}
                empty={checks.length === 0}
                emptyLabel={t("inventory.overview.noChecks")}
                emptyIcon="health"
              >
                <ul className="mini-list">
                  {checks.map((check) => {
                    const state = CHECK_STATE_PRESENTATION[check.state];
                    return (
                      <li key={`${check.check_id}:${check.selector}`} className="mini-row">
                        <span className="mini-copy">
                          <span className="mini-title block">
                            {checkLabel(check.check_type)}
                            {check.selector ? ` (${check.selector})` : ""}
                          </span>
                          <span className="mini-sub block">
                            {check.reported_at ? (
                              <RelTime iso={check.reported_at} zone={zone} now={now} />
                            ) : (
                              t("inventory.overview.neverReported")
                            )}
                          </span>
                        </span>
                        {check.last_value ? (
                          <span className="mini-value mono">{check.last_value}</span>
                        ) : (
                          <NoValue label={t("inventory.overview.noReading")} />
                        )}
                        <StatusPill value={check.state} kind={CHECK_KIND[check.state]} label={state.label} />
                      </li>
                    );
                  })}
                </ul>
              </QueryState>
            </Panel>
          ) : null}

          <Panel title={t("inventory.overview.activity")}>
            {can("jobs.read") ? (
              <FieldGroup title={t("inventory.overview.recentJobs")} icon="jobs">
                <QueryState
                  loading={jobs.isLoading}
                  error={jobs.error}
                  empty={deviceJobs.length === 0}
                  emptyLabel={t("inventory.overview.noJobs")}
                  emptyIcon="jobs"
                >
                  <ul className="mini-list">
                    {deviceJobs.map((job) => (
                      <li key={job.id} className="mini-row">
                        <span className="mini-copy">
                          <Link to={`/jobs?job=${job.id}`}>{jobTypeLabel(job.type)}</Link>
                          <span className="mini-sub block">
                            <RelTime iso={job.issued_at} zone={zone} now={now} />
                          </span>
                        </span>
                        <StatusPill value={job.status} kind={jobStatusKind(job.status)} />
                      </li>
                    ))}
                  </ul>
                </QueryState>
                <p className="mt-2">
                  <Link to={`${here}?tab=jobs`}>{t("inventory.overview.allJobs")}</Link>
                </p>
              </FieldGroup>
            ) : null}
            <FieldGroup title={t("inventory.overview.changes")} icon="changes">
              <QueryState
                loading={changes.isLoading}
                error={changes.error}
                empty={recentChanges.length === 0}
                emptyLabel={t("inventory.detail.noChanges")}
                emptyIcon="changes"
              >
                <ul className="mini-list">
                  {recentChanges.map((change) => (
                    <li key={change.id} className="mini-row">
                      <span className="mini-copy">
                        <span className="mini-title block">{change.subject}</span>
                        <span className="mini-sub block">
                          {humaniseEnum(change.change_type, "InventoryChangeType")} ·{" "}
                          <RelTime iso={change.ts} zone={zone} now={now} />
                        </span>
                      </span>
                    </li>
                  ))}
                </ul>
              </QueryState>
              <p className="mt-2">
                <Link to={`${here}?tab=changes`}>{t("inventory.overview.allChanges")}</Link>
              </p>
            </FieldGroup>
          </Panel>

          {managed && can("backup.read") ? (
            <Panel title={t("inventory.overview.backup")} actions={<Link to={`${here}?tab=backup`}>{t("inventory.overview.backupDetails")}</Link>}>
              <QueryState loading={backup.isLoading} error={backup.error} empty={false} emptyLabel="" emptyIcon="backup">
                <KV
                  items={[
                    [
                      t("inventory.overview.lastGood"),
                      backup.data?.last_good_point ? (
                        <RelTime iso={backup.data.last_good_point.taken_at} zone={zone} now={now} />
                      ) : (
                        <StatusPill value="none" kind="warn" label={t("inventory.overview.neverBackedUp")} />
                      ),
                    ],
                    [
                      t("inventory.overview.lastRun"),
                      backup.data?.last_run_state ? (
                        <StatusPill value={backup.data.last_run_state} />
                      ) : (
                        <NoValue label={t("inventory.overview.noBackupRun")} />
                      ),
                    ],
                    [t("inventory.overview.storedIn"), backup.data?.repository?.name ?? <NoValue label={t("inventory.overview.noRepository")} />],
                  ]}
                />
              </QueryState>
            </Panel>
          ) : null}

          {can("ticket.read") ? (
            <Panel
              title={t("inventory.overview.openTickets")}
              actions={<Link to="/service-desk?all=1">{t("inventory.overview.allTickets")}</Link>}
            >
              <QueryState
                loading={tickets.isLoading}
                error={tickets.error}
                empty={open.length === 0}
                emptyLabel={t("inventory.overview.noTickets")}
                emptyIcon="ticket"
              >
                <ul className="mini-list">
                  {open.slice(0, SHOWN).map((ticket) => (
                    <li key={ticket.id} className="mini-row">
                      <span className="mini-copy">
                        <Link to={`/service-desk/tickets/${ticket.id}`}>
                          {ticket.number} · {ticket.subject}
                        </Link>
                      </span>
                      <StatusPill value={ticket.status} label={ticketStatusLabel(ticket.status)} />
                    </li>
                  ))}
                </ul>
              </QueryState>
            </Panel>
          ) : null}
        </>
      }
    />
  );
}
