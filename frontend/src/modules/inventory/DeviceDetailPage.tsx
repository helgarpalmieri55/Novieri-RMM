import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { Attributes } from "../../components/attributes";
import { NoValue } from "../../components/emptystate";
import { Badge, Button } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import type { Device, Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDate, formatDateTime } from "../../lib/datetime";
import { formatBytes } from "../../lib/format";
import { humaniseEnum } from "../../lib/labels";
import { RESYNC_KIND } from "../../lib/resync";
import { useLiveEvents } from "../../lib/ws";
import { RelTime } from "../../components/reltime";
import { Icon } from "../../shell/icons";
import { KIND_ICON } from "../../shell/viz";
import { describeAge, isStuck, jobStatusKind } from "../jobs/jobState";
import { DeviceActions, outcomeText, type ActionOutcome } from "./DeviceActions";
import { DeviceOverview } from "./DeviceOverview";
import { alertLine, certificateLine, updatesLine, useDeviceSummary } from "./deviceSummary";
import { jobTypeLabel } from "../jobs/jobTypes";
import { DevicePatchTab } from "../patch/DevicePatchTab";
import { DeviceDeployTab } from "../deploy/DeviceDeployTab";
import { DeviceBackupTab } from "../backup/DeviceBackupTab";
import { DeviceProvisioningTab } from "../provisioning/DeviceProvisioningTab";
import { DeviceHealthPanel } from "../health/DeviceHealthPanel";
import { DeviceAssignedTo } from "../people/links";
import { osLabel } from "../devices/osLabel";
import { PushDetailsDialog } from "../devices/PushDetailsDialog";
import { ARCH_OPTIONS } from "../devices/PushDetailsFields";
import { factBadge } from "./factBadges";
import { interfaceName, presentAttributes, splitHardware } from "./hardware";
import { FilterBar, PageHead, Panel, QueryState, SearchField, StatusPill, TableWrap, Tabs } from "../../shell/components";
import { Facts } from "../../shell/viz";

// THE INCIDENT HUB (review 2026-09-23, reviewer 2 journey c and top-15 item
// 1). The header says whether the machine is all right -- status, open
// alerts, certificate, updates, backup, who uses it -- and carries the
// actions a technician takes on it (`DeviceActions`). The default tab is an
// Overview joining health, updates, backup, recent jobs and open tickets from
// each module's own per-device route; nothing here is computed that the
// server did not report.

type Tab =
  | "overview"
  | "hardware"
  | "software"
  | "changes"
  | "jobs"
  | "docs"
  | "patch"
  | "deploy"
  | "backup"
  | "provisioning"
  | "health";

/** Every key the union above admits, for validating a hand-typed `?tab=`. */
const ALL_TAB_KEYS: readonly Tab[] = [
  "overview",
  "hardware",
  "software",
  "changes",
  "jobs",
  "docs",
  "patch",
  "deploy",
  "backup",
  "provisioning",
  "health",
];

/** The tab a bare `/devices/:id` opens, and where an unknown `?tab=` falls. */
const DEFAULT_TAB: Tab = "overview";

/**
 * A byte count for a stat card.
 *
 * Delegates to `lib/format`, which is the SAME formatter the hardware table's
 * `<Attributes>` now uses. The audit's note on the old pair — "the Memory stat
 * card *does* format correctly to 15.0 GiB, proving the formatter exists and
 * simply is not applied here" — is only answered when both ends share one.
 */
/**
 * What a hardware component IS, in the words the audit asked for.
 *
 * "Group the tab into sections (Processor, Memory, Storage, Network)" — the
 * grouping is the ordering in `hardware.ts`; this is the vocabulary
 * (`enums.HardwareComponentType`). A component type this build has never seen
 * still reads as words rather than as an identifier.
 */
function componentTypeLabel(type: string): string {
  return humaniseEnum(type, "HardwareComponentType");
}

/** The row's provenance, in the operator's words (same table as the fleet list). */
const PROVENANCE_LABELS: Record<Device["provenance"], MessageKey> = {
  agent: "inventory.detail.provenanceAgent",
  operator: "inventory.detail.provenanceOperator",
  promoted: "inventory.detail.provenancePromoted",
  sweep: "inventory.detail.provenanceSweep",
};

function memory(value: unknown): string {
  return typeof value === "number" ? formatBytes(value) : "";
}

export function DeviceDetailPage() {
  const t = useT();
  const { deviceId } = useParams<{ deviceId: string }>();
  const api = useApi();
  const queryClient = useQueryClient();
  /**
   * THE TAB LIVES IN THE URL, and the stat cards below are what forced it.
   *
   * `StatCard` now refuses a tile that cannot say where its number lives, and
   * for CPU, memory and serial the answer is "the Hardware tab of this page" —
   * which, while the tab was `useState`, was not addressable at all. A tile
   * pointing at `/devices/:id` would have been a link that navigates nowhere:
   * precisely the dead tile the operator asked us to stop shipping.
   *
   * It pays for itself twice over. "Look at the Jobs tab on ws-alpha" is now a
   * URL somebody can paste into an incident channel, Back undoes a tab change
   * instead of leaving the page, and a reload keeps the operator where they
   * were. Same reasoning, and the same param mechanics, as the fleet list's
   * `?state=` (DevicesPage.tsx).
   */
  const [params, setParams] = useSearchParams();
  const requested = params.get("tab");
  // An unrecognised `?tab=` is the Overview, not a blank page.
  const tab: Tab = (ALL_TAB_KEYS as readonly string[]).includes(requested ?? "")
    ? (requested as Tab)
    : DEFAULT_TAB;
  const setTab = (key: Tab) => {
    const next = new URLSearchParams(params);
    next.set("tab", key);
    setParams(next);
  };
  const [softwareFilter, setSoftwareFilter] = useState("");
  const [showVirtual, setShowVirtual] = useState(false);
  const [outcome, setOutcome] = useState<ActionOutcome | null>(null);
  const [pushDetailsOpen, setPushDetailsOpen] = useState(false);

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canReadPatch = permissions.includes("patch.read");
  const canReadDeploy = permissions.includes("deploy.read");
  const canReadBackup = permissions.includes("backup.read");
  const canReadProvisioning = permissions.includes("provisioning.read");
  // The device health view is gated on `devices.read` server-side, NOT
  // `health.read` (health spec §10): a technician looking at a device needs
  // its health without holding health administration rights. Anyone who can
  // reach this page already holds `devices.read`, so the tab is unconditional.
  // The SAMPLE NOW button is the exception: it dispatches a signed job, so
  // §10's amendment moved it off `health.read` (which `read-only` and `auditor`
  // hold) onto `health.alert.manage`. This is an AFFORDANCE, not a gate --
  // `check_now_route` refuses server-side either way (invariant #6).
  const canCheckNow = (me.data?.permissions ?? []).includes("health.alert.manage");
  // The profile's zone, already fetched above for permissions (cycle 5). Every
  // time on this page -- last sweep, change, job -- is read in it, so the four
  // tabs cannot disagree with each other about when something happened.
  const zone = me.data?.timezone;

  // THE DEVICE ROW ITSELF, not only its inventory snapshot. An asset has no
  // snapshot -- nothing ever ran on it to take one -- so `summary` is empty and
  // cannot answer "does this record have an agent". `agent_status` can: it is
  // null exactly when no `agents` row exists (asset-inventory spec §2).
  const device = useQuery({
    queryKey: ["device", deviceId],
    queryFn: () => api.getDevice(deviceId as string),
    enabled: deviceId != null,
  });
  const managed = device.data?.agent_status != null;
  const hub = useDeviceSummary(deviceId, device.data, permissions);

  const inventory = useQuery({
    queryKey: ["inventory", deviceId],
    queryFn: () => api.getDeviceInventory(deviceId as string),
    enabled: deviceId != null,
  });
  const changes = useQuery({
    queryKey: ["inventory-changes", deviceId],
    queryFn: () => api.getDeviceChanges(deviceId as string),
    enabled: deviceId != null && tab === "changes",
  });
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api.listJobs(),
    enabled: tab === "jobs",
  });
  const linkedDocs = useQuery({
    queryKey: ["device-docs", deviceId],
    queryFn: () => api.listArticles({ target_type: "device", target_id: deviceId as string }),
    enabled: deviceId != null && tab === "docs",
  });
  // §11.5 — ONE TAB, TWO LISTS. "What is written about this machine" is one
  // question, and splitting it across two tabs would cost a click to learn
  // that the answer was on the other one. Same filter, same target vocabulary
  // (`doc_document_links` is the shipped `_LINK_TARGET_MODELS` set, §0.7), a
  // separate query because it is a separate route -- and separately stated
  // states, so a failing documents request cannot render as "no documents".
  const linkedDocuments = useQuery({
    queryKey: ["device-documents", deviceId],
    queryFn: () => api.listDocuments({ target_type: "device", target_id: deviceId as string }),
    enabled: deviceId != null && tab === "docs",
  });

  // A RESYNC re-reads everything below (final review W1): the socket had a
  // gap, and an event lost in it is otherwise lost until the page reloads.
  useLiveEvents((event) => {
    const resync = event.kind === RESYNC_KIND;
    if (resync || (event.kind === "inventory_updated" && event.device_id === deviceId)) {
      void queryClient.invalidateQueries({ queryKey: ["inventory", deviceId] });
      void queryClient.invalidateQueries({ queryKey: ["inventory-changes", deviceId] });
    }
    if (resync || event.kind === "job_status") {
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    }
  });

  // ONE INSTANT for every age on this page, so two rows cannot disagree about
  // how long they have been waiting.
  const now = Date.now();
  const summary = inventory.data?.summary ?? {};
  const facts = inventory.data?.facts ?? {};
  const serial =
    typeof summary.serial === "string" && summary.serial.trim() !== ""
      ? summary.serial
      : null;
  // CONTAINER PLUMBING BEHIND A TOGGLE, not deleted. See `hardware.ts`.
  const hardware = splitHardware(inventory.data?.hardware ?? []);
  const hardwareRows = showVirtual
    ? [...hardware.visible, ...hardware.virtual]
    : hardware.visible;
  const deviceJobs = (jobs.data?.items ?? []).filter((job) => job.device_id === deviceId);
  const filteredSoftware = (inventory.data?.software ?? []).filter((item) =>
    item.name.toLowerCase().includes(softwareFilter.toLowerCase()),
  );

  // PATCH, DEPLOY, BACKUP, PROVISIONING AND HEALTH CANNOT POPULATE WITHOUT AN
  // AGENT, so for an asset they are absent rather than empty. A zeroed Patch
  // card on a switch tells an operator that switch is patch-compliant -- which
  // is the same claim as a wrong number, not a weaker one. This is the rule
  // this page's fleet-list sibling already applies to columns
  // (asset-inventory spec §5).
  // THE INCIDENT ORDER: what is wrong (Overview, Health), what to do about it
  // (Updates, Backup), then the inventory detail. Named for the task, not the
  // module ("Updates", not "Patch").
  const tabItems: { key: Tab; label: string }[] = [
    { key: "overview", label: t("inventory.detail.tabOverview") },
    ...(managed ? [{ key: "health" as const, label: t("inventory.detail.tabHealth") }] : []),
    ...(managed && canReadPatch ? [{ key: "patch" as const, label: t("inventory.detail.tabUpdates") }] : []),
    ...(managed && canReadBackup ? [{ key: "backup" as const, label: t("inventory.detail.tabBackup") }] : []),
    { key: "software", label: t("inventory.detail.tabSoftware") },
    ...(managed && canReadDeploy ? [{ key: "deploy" as const, label: t("inventory.detail.tabDeployments") }] : []),
    { key: "hardware", label: t("inventory.detail.tabHardware") },
    { key: "jobs", label: t("inventory.detail.tabJobs") },
    { key: "changes", label: t("inventory.detail.tabChanges") },
    { key: "docs", label: t("inventory.detail.tabDocs") },
    ...(managed && canReadProvisioning ? [{ key: "provisioning" as const, label: t("inventory.detail.tabImaging") }] : []),
  ];

  // A URL asking for a tab this operator or this asset does not have —
  // `?tab=patch` without `patch.read`, or any agent-only tab on a device that
  // has no agent — falls back to the Overview. The alternative is an empty
  // panel under a tab strip with nothing selected, which reads as a page that
  // failed to load rather than a tab that was never offered.
  const activeTab: Tab = tabItems.some((item) => item.key === tab) ? tab : DEFAULT_TAB;
  const hostname = String(summary.hostname ?? device.data?.hostname ?? t("inventory.detail.fallbackHost"));

  // Where this page's own facts point. `/devices` when there is no id, so a
  // fact can never render an href of `/devices/undefined`.
  const here = deviceId != null ? `/devices/${deviceId}` : "/devices";

  // v3's sub-line under the title: "Ubuntu 26.04 LTS · 10.20.4.31 · last
  // contact 4 Sep 06:25". Composed from what the row actually carries and
  // nothing else; a part that is not known is left out rather than dashed.
  const sublineText = [
    // THE FAMILY IS A LABEL, NOT THE ENUM: this joined the two wire fields
    // verbatim, so the line under the h1 read "windows 11 24H2" on every
    // managed device and "macos 15.6" on every Mac. `osLabel` owns the three
    // spellings, and the fleet list reads the same function for its column.
    osLabel(
      typeof summary.os_family === "string" ? summary.os_family : null,
      typeof summary.os_version === "string" ? summary.os_version : null,
    ) ?? "",
    device.data?.primary_ip ?? "",
  ]
    .filter(Boolean)
    .join(" · ");
  const lastContact = device.data?.agent_status != null && device.data.last_seen_at ? device.data.last_seen_at : null;
  const subline = (
    <>
      {sublineText}
      {lastContact ? (
        <>
          {sublineText ? " · " : ""}{t("inventory.detail.lastContact")} <RelTime iso={lastContact} zone={zone} now={now} />
        </>
      ) : null}
    </>
  );

  /**
   * v3's `facts` strip, where the previous design drew four stat tiles.
   *
   * EACH FACT STILL OPENS THE TAB THAT HOLDS ITS DETAIL -- hardware for the
   * three hardware facts, the job history for the sweep that produced them --
   * because the tab is in the URL (see the top of this component) and a
   * number with nowhere to go is the dead tile the operator asked us to stop
   * shipping. `.stat-value` / `.stat-foot` stay as hooks the tests query.
   */
  const fact = (value: ReactNode, to: string, foot?: ReactNode) => (
    <>
      <Link className="stat-value" to={to}>
        {value}
      </Link>
      {foot ? <span className="stat-foot cell-sub block">{foot}</span> : null}
    </>
  );
  /* Empty when the row does not say: an unlabelled chip is worse than no chip,
     and `provenance` is a column a device row acquired later than some of the
     fixtures that stand in for one. */
  const provenance = device.data
    ? PROVENANCE_LABELS[device.data.provenance]
      ? t(PROVENANCE_LABELS[device.data.provenance])
      : (device.data.provenance ?? "")
    : "";
  const factItems = [
    ...(device.data
      ? [
          {
            // "AGENT LAST SEEN", never a bare "Last contact": the audit found
            // this page reporting two different last-contact times in two
            // formats; naming which subsystem each belongs to is half that
            // fix, and `formatDateTime` in the operator's zone is the other.
            k: t("inventory.detail.factAgent"),
            v:
              device.data.agent_status === null ? (
                t("inventory.detail.noAgentEnrolled")
              ) : (
                <>
                  {t("inventory.detail.agentLastSeen")} <RelTime iso={device.data.last_seen_at} zone={zone} now={now} />
                </>
              ),
          },
          {
            k: t("inventory.detail.factCertificate"),
            v: device.data.cert_revoked ? (
              <StatusPill value="revoked" />
            ) : device.data.cert_state ? (
              <>
                <StatusPill value={device.data.cert_state} />
                {/* ON ITS OWN LINE, and that is the whole of it: inline
                    after the pill, the date wrapped mid-value in this
                    170px-minimum cell -- "OK Valid to 08 Apr" then "2027"
                    alone on the next line, starting under the caption rather
                    than under the value it belongs to. A block sub-line is
                    what every other two-part value on this page already
                    uses. */}
                {device.data.cert_not_after ? (
                  <span className="cell-sub block">
                    {t("inventory.detail.validUntil")} <RelTime iso={device.data.cert_not_after} zone={zone} now={now} />
                  </span>
                ) : null}
              </>
            ) : (
              <NoValue label={t("inventory.detail.noCertificate")} />
            ),
          },
        ]
      : []),
    // AN ABSENT FACT SAYS SO. Every other empty value on this page is a
    // `NoValue` with an accessible name; a bare dash here was the one place
    // on the strip that said nothing to a screen reader.
    {
      k: t("inventory.detail.factCpu"),
      v: fact(
        summary.cpu == null ? (
          <NoValue label={t("inventory.detail.noCpu")} />
        ) : (
          String(summary.cpu)
        ),
        `${here}?tab=hardware`,
      ),
    },
    {
      k: t("inventory.detail.factMemory"),
      v: fact(
        memory(summary.ram_bytes) || (
          <NoValue label={t("inventory.detail.noMemory")} />
        ),
        `${here}?tab=hardware`,
      ),
    },
    {
      k: t("inventory.detail.factSerial"),
      // THE SERIAL SAYS WHY IT IS EMPTY. Auditor A: "'Serial' stat card
      // shows a bare em-dash given equal visual weight to real data". "Not
      // reported" is a fact an asset manager can act on; "—" is a shrug.
      v: fact(
        serial == null ? t("inventory.detail.notReported") : serial,
        `${here}?tab=hardware`,
        serial == null ? t("inventory.detail.noSerial") : undefined,
      ),
    },
    {
      k: t("inventory.detail.factSweep"),
      // AGE IN THE VALUE, INSTANT UNDERNEATH (Auditor A, [LOW]). The job
      // history, not the hardware tab: "why is this snapshot two days old?"
      // is answered by the inventory job that did or did not run.
      v: fact(
        describeAge(inventory.data?.snapshot_taken_at, now),
        `${here}?tab=jobs`,
        formatDateTime(inventory.data?.snapshot_taken_at, zone),
      ),
    },
  ];

  return (
    <>
      <PageHead
        title={hostname}
        description={subline}
        actions={
          /* THE INCIDENT HUB'S ACTIONS (review 2026-09-23, top-15 item 1),
             then a way back to the list, which keeps the list's own tab and
             search because `/devices` reads them from the query string. */
          <>
            {deviceId ? (
              <DeviceActions
                device={device.data}
                deviceId={deviceId}
                hostname={hostname}
                permissions={permissions}
                myUserId={me.data?.user_id}
                onOutcome={setOutcome}
              />
            ) : null}
            <Link className="btn" to="/devices">
              {t("inventory.detail.allDevices")}
            </Link>
          </>
        }
      />

      {/* WHAT THE LAST ACTION DID, said once, where the operator is looking. */}
      {outcome ? (
        <div role="status" className={`notice mb-4${outcome.tone === "error" ? " red" : ""}`}>
          <Icon name={KIND_ICON[outcome.tone === "error" ? "crit" : "info"]} size={17} />
          <div>
            {outcomeText(outcome.text)}
            {outcome.link ? (
              <>
                {" "}
                <Link to={outcome.link.to}>{outcomeText(outcome.link.label)}</Link>
              </>
            ) : null}
          </div>
        </div>
      ) : null}

      {/* IS THIS MACHINE ALL RIGHT? One line: whether it answers, its open
          alerts, its certificate, updates and backup, and who uses it. Each
          part links to the tab or page holding its detail, and a part this
          operator cannot read is left out rather than shown as unknown. */}
      <div className="mb-4 flex flex-wrap items-center gap-2" role="group" aria-label={t("inventory.detail.statusGroup")}>
        {device.data ? <StatusPill value={device.data.agent_status ?? "no agent"} /> : null}
        {managed && hub.health.data ? (
          <Link
            to={
              permissions.includes("health.read")
                ? `/health?device=${deviceId}&state=open`
                : `${here}?tab=health`
            }
          >
            <StatusPill
              value="alerts"
              kind={alertLine(hub.health.data.open_alerts).kind}
              label={alertLine(hub.health.data.open_alerts).text}
            />
          </Link>
        ) : null}
        {certificateLine(device.data) ? (
          <StatusPill
            value="certificate"
            kind={certificateLine(device.data)?.kind}
            label={certificateLine(device.data)?.text}
          />
        ) : null}
        {updatesLine(hub.patch.data) ? (
          <Link to={`${here}?tab=patch`}>
            <StatusPill
              value="updates"
              kind={updatesLine(hub.patch.data)?.kind}
              label={updatesLine(hub.patch.data)?.text}
            />
          </Link>
        ) : null}
        {hub.backup.data ? (
          <Link to={`${here}?tab=backup`}>
            {hub.backup.data.last_good_point ? (
              <span className="cell-sub">
                {t("inventory.detail.backedUp")} <RelTime iso={hub.backup.data.last_good_point.taken_at} zone={zone} now={now} />
              </span>
            ) : (
              <StatusPill value="backup" kind="warn" label={t("inventory.detail.neverBackedUp")} />
            )}
          </Link>
        ) : null}
        {/* PROVENANCE MOVED HERE FROM THE FACTS STRIP, and the reason is the
            strip's arithmetic. `.facts` is `repeat(auto-fit, minmax(170px,
            1fr))` (`viz.css`), so at 1440 it draws six tracks; a seventh cell
            opened a second row that held one value and about 970x90px of
            bordered white directly under the page head. The count has to
            divide the row, and provenance is the cell that was never a
            measurement: agent, certificate, CPU, memory, serial and last
            sweep are all things a machine reported, while "how did this row
            come to exist" is a property of the record, which is exactly what
            this chip line already carries beside the agent state. Nothing is
            lost -- same table of words, same four values, and it stays on
            screen at every width because chips wrap. */}
        {provenance ? <Badge variant="muted">{provenance}</Badge> : null}
        {/* The holder is a person of the DEVICE'S client: "Someone new" files
            them there (final review E), so the control waits for the row. */}
        {deviceId && device.data?.client_id ? (
          <DeviceAssignedTo deviceId={deviceId} clientId={device.data.client_id} />
        ) : null}
        {Object.entries(facts).map(([key, value]) => {
          const badge = factBadge(key, value);
          return badge ? (
            <Badge key={key} variant={badge.variant}>
              {badge.label}
            </Badge>
          ) : null;
        })}
      </div>

      {/* A HEADING PER SECTION. Auditor A: "[LOW] Page has a single `<h1>` and
          no section headings … With one heading per page, the heading shortcut
          is useless and the page's structure is invisible to assistive tech."
          `sr-only` because the design gives the facts strip and the tab strip
          no visible title and inventing two would change the page. */}
      <h2 className="sr-only">{t("inventory.detail.summary")}</h2>
      {/* v3's `facts`: caption/value cells in one strip, SIX of them, which is
          how many tracks the strip draws at the desktop width. Two are facts
          the row carries (agent, certificate) and four the inventory snapshot
          does (CPU, memory, serial, last sweep); provenance is the chip above.
          Site server, outstanding patches, last backup and open alerts are the
          design's other cells; each is another module's join and each has its
          own tab below, so they are omitted here rather than faked. */}
      <Facts items={factItems} />

      {device.data != null && !managed && (
        <Panel
          title={t("inventory.detail.noAgentTitle")}
          subtitle={
            device.data.lifecycle === "unmanageable"
              ? t("inventory.detail.unmanageable")
              : t("inventory.detail.needsAgent")
          }
          actions={
            device.data.lifecycle !== "unmanageable" ? (
              <Link className="btn" to="/agent-packages">
                {t("inventory.detail.agentPackages")}
              </Link>
            ) : undefined
          }
        >
          <p className="note">
            {device.data.lifecycle === "unmanageable"
              ? t("inventory.detail.neverPopulate")
              : t("inventory.detail.tabsFill")}
          </p>
          {/* WHAT A PUSH NEEDS, where the machine that lacks it is shown. A
              machine that never ran the agent has nothing to report its DNS
              name, OS or processor, and push refuses without them (Windows:
              Kerberos needs the name). A person types them; never reverse DNS
              (plan W10). The server refuses the edit on a managed device. */}
          {device.data.lifecycle !== "unmanageable" ? (
            <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
              <p className="note">
                {t("inventory.detail.pushNote", {
                  dns: device.data.fqdn ?? t("inventory.detail.notRecorded"),
                  os: osLabel(device.data.os_family, "") ?? t("inventory.detail.notRecorded"),
                  arch:
                    ARCH_OPTIONS.find((item) => item.value === device.data?.architecture)?.label ??
                    (device.data.architecture || t("inventory.detail.notRecorded")),
                })}
              </p>
              {permissions.includes("assets.manage") ? (
                <Button variant="outline" onClick={() => setPushDetailsOpen(true)}>
                  {t("inventory.detail.pushDetails")}
                </Button>
              ) : null}
            </div>
          ) : null}
          {pushDetailsOpen ? (
            <PushDetailsDialog device={device.data} open onClose={() => setPushDetailsOpen(false)} />
          ) : null}
        </Panel>
      )}

      {/* THE TAB STRIP NOW CONTROLS SOMETHING. Auditor A: "[MEDIUM] Tabs are
          missing `aria-controls`/`tabpanel` wiring … no element on the page
          has `role='tabpanel'` (count = 0) … A screen-reader user is told
          'tab, selected' but there is no associated panel to navigate to."
          `Tabs` already carries the roving tabindex and the arrow keys; what
          it could not supply is an id for a panel the page had not grown. */}
      <h2 className="sr-only">{t("inventory.detail.deviceDetails")}</h2>
      <Tabs
        items={tabItems}
        active={activeTab}
        onSelect={(key) => setTab(key as Tab)}
        panelId="device-tabpanel"
        // Eleven tabs fold into three rows on a phone; one row scrolls.
        scroll
      />

      <Panel bare>
        <div
          id="device-tabpanel"
          role="tabpanel"
          aria-label={tabItems.find((item) => item.key === activeTab)?.label ?? t("inventory.detail.detailsFallback")}
          tabIndex={-1}
        >
        {activeTab === "overview" && deviceId != null && (
          <DeviceOverview
            deviceId={deviceId}
            summary={hub}
            permissions={permissions}
            zone={zone}
            now={now}
          />
        )}
        {activeTab === "hardware" && (
          <QueryState
            loading={inventory.isLoading}
            error={inventory.error}
            empty={(inventory.data?.hardware ?? []).length === 0}
            emptyLabel={t("inventory.detail.noHardware")}
            emptyIcon="server"
          >
            {/* THE PLUMBING TOGGLE. Auditor A: "Filter out `veth*`/`docker*`/
                `br-*` interfaces by default behind a 'Show virtual interfaces
                (20)' toggle, and sort real NICs first by whether they hold a
                routable address." Rendered only when there is something behind
                it, and it says how many — a toggle that hides an unstated
                number of rows is its own kind of lie.

                A DISCLOSURE, NOT A `Btn`. `aria-expanded` and `aria-controls`
                are what make it one, and `Btn` carries neither; it wears
                `.btn small` so it is drawn by the design either way. */}
            {hardware.virtual.length > 0 && (
              <FilterBar>
                <button
                  type="button"
                  className="btn small"
                  aria-expanded={showVirtual}
                  aria-controls="hardware-table"
                  onClick={() => setShowVirtual((shown) => !shown)}
                >
                  {t(showVirtual ? "inventory.detail.hideVirtual" : "inventory.detail.showVirtual", {
                    count: hardware.virtual.length,
                  })}
                </button>
              </FilterBar>
            )}
            <TableWrap>
              <table id="hardware-table">
                <thead>
                  <tr>
                    {/* `scope="col"`. Auditor A: "[LOW] Table header cells omit
                        `scope='col'` … Without `scope`, assistive technology
                        has to guess the header-to-cell association." */}
                    <th scope="col">{t("inventory.detail.thType")}</th>
                    <th scope="col">{t("inventory.detail.thInterface")}</th>
                    <th scope="col">{t("inventory.detail.thAttributes")}</th>
                  </tr>
                </thead>
                <tbody>
                  {hardwareRows.map((component) => (
                    <tr key={`${component.type}-${component.identifier}`}>
                      <td>{componentTypeLabel(component.type)}</td>
                      <td>
                        {/* THE NAME AN OPERATOR RECOGNISES, over the MAC the
                            agent keys the row by. `nicComponentFrom` sets
                            `identifier` to the MAC where it has one, so a
                            column of identifiers alone is a column of MAC
                            addresses — the "MAC-address sort order" the audit
                            found the real interfaces buried in. */}
                        <div>{interfaceName(component)}</div>
                        {interfaceName(component) === component.identifier ? null : (
                          <div className="cell-sub mono">
                            {component.identifier}
                          </div>
                        )}
                      </td>
                      {/* NOT `JSON.stringify`, AND NOT A HAND-ROLLED JOIN
                          EITHER. Auditor A: "[HIGH] Raw JSON blobs are
                          rendered directly into the UI as table content …
                          Byte counts are unformatted (`256022937600` instead
                          of '238 GiB' — note the Memory stat card *does*
                          format correctly, proving the formatter exists and
                          simply is not applied here)". The join this file used
                          to carry fixed the flat cells and left both of the
                          audit's actual examples intact: it printed
                          `size_bytes 256022937600` and fell back to
                          `JSON.stringify` for a NIC's `addresses[]`, which is
                          the 225-character cell the finding quotes.
                          `<Attributes>` formats `_bytes` keys through the same
                          `lib/format` the Memory card uses, flattens an
                          address record to its address, and keeps the raw
                          object behind the "View raw" disclosure the finding
                          asks for. */}
                      <td>
                        {/* THE UNIT IS NAMED ONCE, AND AFTER THE NUMBER.
                            `<Attributes>` humanises a wire key and formats a
                            `_bytes` value, and doing both to `size_bytes`
                            printed "Size bytes 512 GiB" -- the unit in the
                            caption, a different unit in the value, and the
                            two in the wrong order. `presentAttributes` is
                            where a hardware key that carries its unit in its
                            NAME becomes a caption a person reads and a value
                            that carries the unit instead.

                            RAW STAYS EXACT, which is why it is rendered here
                            from `component.attributes` rather than from the
                            presented copy: the disclosure exists so an
                            engineer can read 549755813888, and a "View raw"
                            showing "512 GiB" would be the audit's original
                            finding wearing the fix's clothes. */}
                        <Attributes
                          value={presentAttributes(component.attributes)}
                          empty={t("inventory.detail.noAttributes")}
                          raw={false}
                        />
                        {component.attributes &&
                        Object.keys(component.attributes).length > 0 ? (
                          <details className="attribute-raw">
                            <summary>{t("inventory.detail.viewRaw")}</summary>
                            <pre className="code">
                              {JSON.stringify(component.attributes, null, 2)}
                            </pre>
                          </details>
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
          </QueryState>
        )}
        {activeTab === "software" && (
          <div>
            <FilterBar>
              <SearchField
                placeholder={t("inventory.detail.filterPlaceholder")}
                /* Bounded, like every other search field in the console
                   (Auditor A, "[LOW] Search input has no maxlength and accepts
                   500+ characters"). */
                maxLength={128}
                value={softwareFilter}
                onChange={(event) => setSoftwareFilter(event.target.value)}
                aria-label={t("inventory.detail.filterAria")}
              />
              <span className="filter-spacer" />
            </FilterBar>
            <QueryState
              loading={inventory.isLoading}
              error={inventory.error}
              empty={filteredSoftware.length === 0}
              emptyLabel={
                (inventory.data?.software ?? []).length === 0
                  ? t("inventory.detail.noSoftware")
                  : t("inventory.detail.noSoftwareMatch")
              }
              emptyIcon="package"
            >
              <TableWrap>
                <table>
                  <thead>
                    <tr>
                      <th scope="col">{t("inventory.detail.thName")}</th>
                      <th scope="col">{t("inventory.detail.thVersion")}</th>
                      <th scope="col">{t("inventory.detail.thPublisher")}</th>
                      <th scope="col">{t("inventory.detail.thInstalled")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredSoftware.map((item) => (
                      <tr key={item.name}>
                        <td className="cell-title">{item.name}</td>
                        <td>{item.version}</td>
                        <td>{item.publisher}</td>
                        {/* UTC ON PURPOSE, and it is the one exception on
                            this page. `install_date` is a SQL `date` -- a
                            floating calendar day with no time and no zone --
                            and `new Date("2026-08-01")` is midnight UTC.
                            Rendering that in a zone west of UTC moves it to
                            the 31st: an off-by-one-day that reads as real
                            data. The zone-aware helpers are for instants. */}
                        <td>{formatDate(item.install_date, "UTC")}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableWrap>
            </QueryState>
          </div>
        )}
        {activeTab === "changes" && (
          <QueryState
            loading={changes.isLoading}
            error={changes.error}
            empty={(changes.data?.items ?? []).length === 0}
            emptyLabel={t("inventory.detail.noChanges")}
            emptyIcon="changes"
          >
            <TableWrap>
              <table>
                <thead>
                  <tr>
                    <th scope="col">{t("inventory.detail.thWhen")}</th>
                    <th scope="col">{t("inventory.detail.thChange")}</th>
                    <th scope="col">{t("inventory.detail.thSubject")}</th>
                    <th scope="col">{t("inventory.detail.thFrom")}</th>
                    <th scope="col">{t("inventory.detail.thTo")}</th>
                  </tr>
                </thead>
                <tbody>
                  {(changes.data?.items ?? []).map((change) => (
                    <tr key={change.id}>
                      <td className="whitespace-nowrap">
                        <RelTime iso={change.ts} zone={zone} now={now} />
                      </td>
                      <td>
                        <StatusPill
                          value={change.change_type}
                          kind={change.change_type === "software_removed" ? "warn" : "info"}
                          label={humaniseEnum(change.change_type, "InventoryChangeType")}
                        />
                      </td>
                      <td className="cell-title">{change.subject}</td>
                      <td>{change.from_value ?? <NoValue label={t("inventory.detail.noPrevious")} />}</td>
                      <td>{change.to_value ?? <NoValue label={t("inventory.detail.noNew")} />}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
          </QueryState>
        )}
        {activeTab === "docs" && (
          <div className="panel-b">
            <h3 className="cell-title mb-2">{t("inventory.detail.articles")}</h3>
            <QueryState
              loading={linkedDocs.isLoading}
              error={linkedDocs.error}
              empty={(linkedDocs.data?.items ?? []).length === 0}
              emptyLabel={t("inventory.detail.noArticles")}
              emptyIcon="book"
            >
              <div>
                {(linkedDocs.data?.items ?? []).map((doc) => (
                  <p key={doc.id} className="py-1">
                    {/* DEEP-LINKED, not "somewhere on /docs". The docs page
                        carries its selection in the query string exactly so
                        this link can name one article. */}
                    <Link to={`/docs?article=${doc.id}`}>{doc.title}</Link>
                    <span className="note ml-2">{doc.category}</span>
                  </p>
                ))}
              </div>
            </QueryState>

            <h3 className="cell-title mb-2 mt-4">{t("inventory.detail.documents")}</h3>
            <QueryState
              loading={linkedDocuments.isLoading}
              error={linkedDocuments.error}
              empty={(linkedDocuments.data?.items ?? []).length === 0}
              emptyLabel={t("inventory.detail.noDocuments")}
              emptyIcon="book"
            >
              <div>
                {(linkedDocuments.data?.items ?? []).map((doc) => (
                  <p key={doc.id} className="py-1">
                    <Link to={`/docs?document=${doc.id}`}>{doc.title}</Link>
                    <span className="note ml-2">
                      {doc.category}
                      {/* A DOCUMENT WITH NO FILE IS A REAL STATE (§4.1), and
                          a row that says nothing about it reads as one that
                          has a file. */}
                      {doc.current_version === 0
                        ? ` · ${t("inventory.detail.noFileYet")}`
                        : ` · v${doc.current_version}`}
                    </span>
                  </p>
                ))}
              </div>
            </QueryState>
          </div>
        )}
        {activeTab === "jobs" && (
          <QueryState
            loading={jobs.isLoading}
            error={jobs.error}
            empty={deviceJobs.length === 0}
            emptyLabel={t("inventory.detail.noJobsDispatched")}
            emptyIcon="jobs"
          >
            <TableWrap>
              <table>
                <thead>
                  <tr>
                    <th scope="col">{t("inventory.detail.thType")}</th>
                    <th scope="col">{t("inventory.detail.thStatus")}</th>
                    <th scope="col">{t("inventory.detail.thIssued")}</th>
                    <th scope="col">{t("inventory.detail.thActions")}</th>
                  </tr>
                </thead>
                <tbody>
                  {deviceJobs.map((job) => (
                    <tr key={job.id}>
                      {/* The name over the wire type, matching /jobs. A column
                          of `patch.scan` was "raw internal identifiers, not
                          human language" — but the identifier is the string an
                          operator greps the backend for, so both stay. */}
                      <td>
                        <div>{jobTypeLabel(job.type)}</div>
                        <div className="cell-sub mono">{job.type}</div>
                      </td>
                      <td>
                        <StatusPill value={job.status} kind={jobStatusKind(job.status)} />
                      </td>
                      {/* "[HIGH] Jobs queued for over 24 hours are reported as
                          healthy" — the age, and a flag when it is stuck. */}
                      <td>
                        <span className="flex flex-wrap items-center gap-2">
                          <RelTime iso={job.issued_at} zone={zone} now={now} />
                          {isStuck(job, now) ? <Badge variant="warning">{t("inventory.detail.stillWaiting")}</Badge> : null}
                        </span>
                      </td>
                      {/* "[HIGH] Queued jobs cannot be cancelled, inspected, or
                          retried — the Jobs tab is read-only text … Every row
                          contains zero buttons and zero links." INSPECTED is
                          the part this console can honestly close: the job
                          engine has no cancel or retry route (see
                          `backend/app/api/v1/jobs.py`), and a button that 404s
                          is a worse answer than a stated no. The full record —
                          params, result, logs, expiry — is one link away. */}
                      <td>
                        {/* PER-ROW CONTEXT ON THE ACTION. Auditor A: "[LOW]
                            'ACTIONS' column header is announced but the
                            actions have no per-row context … a screen-reader
                            user tabbing the column hears 'Run job, Run job,
                            Run job…' with no indication of which device each
                            belongs to." Same defect, same column, this table's
                            version of it. */}
                        <Link
                          to={`/jobs?job=${job.id}`}
                          aria-label={t("inventory.detail.inspectAria", {
                            type: jobTypeLabel(job.type),
                            when: formatDateTime(job.issued_at, zone),
                          })}
                        >
                          {t("inventory.detail.inspect")}
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
          </QueryState>
        )}
        {activeTab === "patch" && canReadPatch && deviceId != null && <DevicePatchTab deviceId={deviceId} />}
        {activeTab === "deploy" && canReadDeploy && deviceId != null && <DeviceDeployTab deviceId={deviceId} />}
        {activeTab === "backup" && canReadBackup && deviceId != null && <DeviceBackupTab deviceId={deviceId} />}
        {activeTab === "provisioning" && canReadProvisioning && deviceId != null && (
          <DeviceProvisioningTab deviceId={deviceId} />
        )}
        {activeTab === "health" && deviceId != null && (
          <DeviceHealthPanel deviceId={deviceId} canCheckNow={canCheckNow} />
        )}
        </div>
      </Panel>
    </>
  );
}
