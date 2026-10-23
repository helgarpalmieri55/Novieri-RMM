/**
 * The device page's default tab: one screen that answers "what is wrong with
 * this machine and what has been done about it" -- health, updates, backup,
 * recent jobs and open tickets, each linking to the tab or page that holds
 * the rest. A panel whose module this operator cannot read is left out.
 */

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { NoValue } from "../../components/emptystate";
import { RelTime } from "../../components/reltime";
import { useT } from "../../i18n/context";
import { useApi } from "../../lib/ApiContext";
import { Panel, QueryState, StatusPill } from "../../shell/components";
import { KV } from "../../shell/viz";
import { checkLabel, severityLabel } from "../health/healthState";
import { jobStatusKind } from "../jobs/jobState";
import { jobTypeLabel } from "../jobs/jobTypes";
import { restartLabel } from "../patch/restartCopy";
import { ticketStatusLabel } from "../service-desk/serviceDeskState";
import { openTickets, rankAlerts, updatesLine, type useDeviceSummary } from "./deviceSummary";

const SHOWN = 5;

export function DeviceOverview({
  deviceId,
  summary,
  permissions,
  zone,
  now,
}: {
  deviceId: string;
  summary: ReturnType<typeof useDeviceSummary>;
  permissions: readonly string[];
  zone?: string | null;
  now: number;
}) {
  const t = useT();
  const api = useApi();
  const can = (code: string) => permissions.includes(code);
  const here = `/devices/${deviceId}`;
  const { managed, health, patch, backup, tickets } = summary;

  // The job list has no device filter, so this is the first page, narrowed --
  // the same source the Jobs tab reads.
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api.listJobs(),
    enabled: can("jobs.read"),
  });
  const deviceJobs = (jobs.data?.items ?? [])
    .filter((job) => job.device_id === deviceId)
    .slice(0, SHOWN);

  const alerts = rankAlerts(health.data?.open_alerts ?? []);
  // Opening one alert needs `health.read`; the device's own Health tab does not.
  const alertTo = (id: string) => (can("health.read") ? `/health?alert=${id}` : `${here}?tab=health`);
  const updates = updatesLine(patch.data);
  const restart = patch.data?.restart;
  const open = openTickets(tickets.data?.items);

  return (
    <div className="grid grid-2 p-4">
      {managed ? (
        <Panel
          title={t("inventory.overview.health")}
          subtitle={health.data ? t("inventory.overview.open", { count: alerts.length }) : undefined}
          actions={<Link to={`${here}?tab=health`}>{t("inventory.overview.allChecks")}</Link>}
        >
          <QueryState
            loading={health.isLoading}
            error={health.error}
            empty={alerts.length === 0}
            emptyLabel={t("inventory.overview.noAlerts")}
            emptyIcon="health"
          >
            <ul className="flex flex-col gap-2">
              {alerts.slice(0, SHOWN).map((alert) => (
                <li key={alert.id} className="flex flex-wrap items-center gap-2">
                  <StatusPill value={alert.severity} label={severityLabel(alert.severity)} />
                  <Link to={alertTo(alert.id)}>{checkLabel(alert.check_type)}</Link>
                  <span className="cell-sub">
                    {t("inventory.overview.since")} <RelTime iso={alert.opened_at} zone={zone} now={now} />
                  </span>
                </li>
              ))}
            </ul>
          </QueryState>
        </Panel>
      ) : null}

      {managed && can("patch.read") ? (
        <Panel title={t("inventory.overview.updates")} actions={<Link to={`${here}?tab=patch`}>{t("inventory.overview.reviewUpdates")}</Link>}>
          <QueryState
            loading={patch.isLoading}
            error={patch.error}
            empty={false}
            emptyLabel=""
            emptyIcon="patch"
          >
            <KV
              items={[
                [t("inventory.overview.state"), updates ? <StatusPill value={updates.text} kind={updates.kind} label={updates.text} /> : <NoValue label={t("inventory.overview.notKnown")} />],
                [
                  t("inventory.overview.restart"),
                  restart && ["pending", "dispatched", "scheduled"].includes(restart.state) ? (
                    <span>
                      {t("inventory.overview.inWindow", { label: restartLabel(restart.state).label })}
                    </span>
                  ) : patch.data?.pending_reboot ? (
                    t("inventory.overview.dueInWindow")
                  ) : (
                    t("inventory.overview.noneDue")
                  ),
                ],
                [t("inventory.overview.lastChecked"), <RelTime iso={patch.data?.last_scan_at} zone={zone} now={now} />],
              ]}
            />
          </QueryState>
        </Panel>
      ) : null}

      {managed && can("backup.read") ? (
        <Panel title={t("inventory.overview.backup")} actions={<Link to={`${here}?tab=backup`}>{t("inventory.overview.backupDetails")}</Link>}>
          <QueryState
            loading={backup.isLoading}
            error={backup.error}
            empty={false}
            emptyLabel=""
            emptyIcon="backup"
          >
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

      {can("jobs.read") ? (
        <Panel title={t("inventory.overview.recentJobs")} actions={<Link to={`${here}?tab=jobs`}>{t("inventory.overview.allJobs")}</Link>}>
          <QueryState
            loading={jobs.isLoading}
            error={jobs.error}
            empty={deviceJobs.length === 0}
            emptyLabel={t("inventory.overview.noJobs")}
            emptyIcon="jobs"
          >
            <ul className="flex flex-col gap-2">
              {deviceJobs.map((job) => (
                <li key={job.id} className="flex flex-wrap items-center gap-2">
                  <StatusPill value={job.status} kind={jobStatusKind(job.status)} />
                  <Link to={`/jobs?job=${job.id}`}>{jobTypeLabel(job.type)}</Link>
                  <span className="cell-sub">
                    <RelTime iso={job.issued_at} zone={zone} now={now} />
                  </span>
                </li>
              ))}
            </ul>
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
            <ul className="flex flex-col gap-2">
              {open.slice(0, SHOWN).map((ticket) => (
                <li key={ticket.id} className="flex flex-wrap items-center gap-2">
                  <StatusPill value={ticket.status} label={ticketStatusLabel(ticket.status)} />
                  <Link to={`/service-desk/tickets/${ticket.id}`}>
                    {ticket.number} · {ticket.subject}
                  </Link>
                </li>
              ))}
            </ul>
          </QueryState>
        </Panel>
      ) : null}
    </div>
  );
}
