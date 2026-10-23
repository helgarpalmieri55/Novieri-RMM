import { useEffect } from "react";
import { Link, useLocation } from "react-router-dom";
import { NoValue } from "../../components/emptystate";
import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate, type MessageKey } from "../../i18n/core";
import { formatDateTime, intlLocale } from "../../lib/datetime";
import { humaniseEnum } from "../../lib/labels";
import { PLATFORM_STATUS_POLL_MS, usePlatformStatus, useProfileZone } from "./api";
import type { ProcessHealth, Probe, ReasonCode, RegisteredModule, Status } from "./types";
import { Icon } from "../../shell/icons";
import { KIND_ICON, Pill, type Kind } from "../../shell/viz";
import {
  Btn,
  PageHead,
  Panel,
  QueryState,
  StatCard,
  StatsGrid,
  StatusPill,
} from "../../shell/components";

// The operator-facing status page (Task 9; app/api/v1/platform.py). Three
// sections, in the order the API reports them: dependency probes, per-process
// heartbeats, then the module registry.
//
// TWO THINGS THIS PAGE MUST NEVER DO, because they are the specific blind
// spots the design exists to close:
//
//   1. Render a status pill (or anything health-shaped) next to a module. The
//      registry is what got REGISTERED at startup -- name, route count,
//      job-type count -- because that is what `build_module_registry`
//      measures. Modules are listed, never probed (platform.py's module
//      docstring, spec §1); a pill here would claim a health signal that does
//      not exist.
//   2. Let `healthy_instances < instances` read as merely "ok". A process
//      behind a load balancer with one dead replica is a partial outage, and
//      the shortfall is rendered as its own visible fact (bold, a distinct
//      colour, AND different text -- "1 / 2 healthy" vs "2 / 2") rather than
//      leaning on the status badge alone to carry it.
//   3. Drop a probe's `detail`. Added 2026-08-18 with the `keycloak_events`
//      probe, and it is not cosmetic: that probe answers `ok` with
//      `configured: false` for a deployment where Keycloak credential events
//      are NOT being forwarded into the audit chain. A row reading
//      `keycloak_events / ok / —` would tell an operator forwarding works. The
//      status alone is deliberately unalarming -- nothing is broken -- so the
//      whole distinction lives in `detail`, and a page that discards it turns
//      a visible gap back into a silent one. `jwks` (`keys: n`) was invisible
//      for the same reason until now.
//
// ---------------------------------------------------------------------------
// UX REMEDIATION CYCLE 12 — THREE FINDINGS, ONE THEME: the page knew the
// platform was down and said so in the quietest way available.
//
// Auditor B, [HIGH]: *"The page's overall verdict is a pill reading 'down'
// measuring 51 x 23 px … positioned at the far top-right … rendered in the
// same muted pill style as every 'ok' badge in the table below. A whole-
// platform outage verdict is the highest-priority information this page can
// convey and it is given the least visual weight on the screen."* So a
// not-OK verdict now opens a full-width `role="alert"` banner above the KPI
// row that NAMES the failing probes; the pill stays for the healthy state.
//
// Auditor B, [MEDIUM]: *"A regex for `as of|updated|refreshed` over
// main.innerText returns false … The relative heartbeat ages ('1 s') actively
// imply live data while being frozen at render time."* The query has polled
// every 15s since it was written, which the page never mentioned. It says so
// now, with the instant of the last read, and offers a manual re-read.
//
// Auditor B, [MEDIUM]: *"'unreachable' is the entire diagnostic. There is no
// retry/re-probe action, no link to configuration, no link to the relevant
// logs."* Each reason code now carries the operator-facing sentence for that
// code, the banner carries the re-probe, and the failing row links to the
// error monitor. WHAT IS DELIBERATELY NOT HERE is the auditor's fourth item,
// "list the dependent capabilities": nothing in the platform declares which
// module depends on which probe, so that list would be this console asserting
// a dependency graph it does not have -- the exact failure mode the module
// registry section below exists to avoid.

/** The three probe statuses as v3 kinds. */
const STATUS_KIND: Record<Status, Kind> = {
  ok: "ok",
  degraded: "warn",
  down: "crit",
};

/**
 * The status as a person reads it.
 *
 * THIS PASSED `label={status}` AND SAID SO ON PURPOSE -- "the words an
 * operator greps the health surface for" -- and the result was nine pills
 * reading a raw lowercase `ok` two lines under prose that writes "OK", on the
 * one page whose subject is whether the platform is well. The wire word is
 * still what the API says and what a log line carries; it is not what a pill
 * says. `down` and `degraded` keep their own words because those ARE the
 * words; `ok` becomes "Healthy", as v3 draws it, because "ok" alone is the
 * one of the three that reads as an untranslated identifier.
 */
const STATUS_LABEL: Record<Status, MessageKey> = {
  ok: "platformStatus.status.ok",
  degraded: "platformStatus.status.degraded",
  down: "platformStatus.status.down",
};

function StatusBadge({ status }: { status: Status }) {
  const t = useT();
  return <StatusPill value={status} label={t(STATUS_LABEL[status])} kind={STATUS_KIND[status]} />;
}

/** A probe's name as a person reads it: the catalogue's label for the names
 *  the backend registers today, the derivation for one added later. */
function probeLabel(name: string): string {
  const key = name.replace(/\./g, "_");
  return humaniseEnum(key, "PlatformProbe");
}

/** A probe's identifier. `name` is what the server prints and follows the request's
 *  language (Spanish for a Spanish request), so glyphs, keys and enum lookups use `key`;
 *  an older payload without one still carries the identifier in `name`. */
function probeKey(probe: Probe): string {
  return probe.key ?? probe.name;
}

/** What a probe row and tile print: the server's own words when it sent a `key`
 *  (they follow the request's language and match the catalogue), else the
 *  catalogue's label for the identifier. */
function probeTitle(probe: Probe): string {
  return probe.key !== undefined ? probe.name : probeLabel(probe.name);
}

/** A registered module's name, the same way. */
function moduleLabel(name: string): string {
  return humaniseEnum(name, "PlatformModule");
}

/**
 * v3 draws each component with a glyph that says what it is. The probe and
 * process names are the server's (`app.core.health`), so this is a lookup
 * over the names that exist today with a generic fallback for one added
 * later -- a probe with no glyph still renders, it just leads with the
 * activity mark.
 */
const PROBE_GLYPH: Record<string, string> = {
  database: "database",
  redis: "bolt",
  cache: "bolt",
  vault: "lock",
  object_store: "archive",
  objectstore: "archive",
  identity: "id-badge-2",
  keycloak: "id-badge-2",
  keycloak_events: "history",
  jwks: "key",
  "backup.nightly": "archive",
  "backup.offsite": "archive",
  "backup.drill": "archive",
};

const PROCESS_GLYPH: Record<string, string> = {
  api: "activity",
  gateway: "wifi",
  worker: "bolt",
  beat: "clock",
};

function glyphFor(table: Record<string, string>, name: string): string {
  return table[name] ?? table[name.split(/[_-]/)[0] ?? ""] ?? "activity";
}

/**
 * What a reason code means, and what to do about it.
 *
 * ONE SENTENCE PER CODE OF `app.core.health.types.ReasonCode`, and nothing
 * beyond what the code itself asserts. The probe deliberately reports a code
 * rather than text (that module's docstring: a connection failure's `str()`
 * carries the DSN, which carries a password), so the gloss is written here
 * from the code's meaning -- it is never assembled from anything the server
 * sent.
 */
const REASON_GUIDANCE: Record<ReasonCode, MessageKey> = {
  unreachable: "platformStatus.guidance.unreachable",
  timeout: "platformStatus.guidance.timeout",
  auth_failed: "platformStatus.guidance.auth_failed",
  sealed: "platformStatus.guidance.sealed",
  empty_cache: "platformStatus.guidance.empty_cache",
  misconfigured: "platformStatus.guidance.misconfigured",
  stale: "platformStatus.guidance.stale",
  failed: "platformStatus.guidance.failed",
};

function guidanceFor(reason: ReasonCode | null): string | null {
  const key = reason ? REASON_GUIDANCE[reason] : undefined;
  return key ? translate(key) : null;
}

// Ages arrive in fractional seconds (`time.time()` deltas); rendered coarse
// enough to read at a glance rather than to the millisecond.
function formatAge(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)} s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  // One decimal, with the language's own decimal mark; never a thousands
  // separator, so English stays "1.5 h" exactly as `toFixed(1)` wrote it.
  const hours = (seconds / 3600).toLocaleString(intlLocale(), {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
    useGrouping: false,
  });
  return `${hours} h`;
}

/**
 * Scroll the section a stat tile points at into view.
 *
 * THIS PAGE IS THE ONLY READER OF ITS OWN NUMBERS. `GET /platform/status`
 * serves probes, per-process heartbeats and the module registry, and nothing
 * else in the console renders any of them — there is no /dependencies screen
 * to send anyone to, and no filter on this page for a tile to set. So every
 * tile here points at a fragment of this page, and without this hook React
 * Router would push that fragment and stop: it does not scroll, and `App.tsx`
 * wires no scroll restoration. A tile that changed the URL and nothing on
 * screen is precisely the dead tile this cycle removes, wearing an href.
 */
function useScrollToSection(rendered: boolean) {
  const { hash } = useLocation();
  useEffect(() => {
    // `rendered` IS LOAD-BEARING, not defensive. The three sections live behind
    // `QueryState`, so arriving at /platform-status#processes from the nav or a
    // pasted link runs this effect while the page still says "Loading…" and the
    // element does not exist yet. Re-running once the data lands is what makes
    // the deep link work on a cold page rather than only on a warm one.
    if (!hash || !rendered) return;
    const target = document.getElementById(decodeURIComponent(hash.slice(1)));
    // jsdom does no layout and ships no `scrollIntoView`, so this is guarded
    // rather than assumed -- a TypeError here would take the whole page down.
    if (target && typeof target.scrollIntoView === "function") {
      target.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [hash, rendered]);
}

export function PlatformStatusPage() {
  const t = useT();
  const status = usePlatformStatus();
  const zone = useProfileZone();
  const data = status.data;

  useScrollToSection(!status.isLoading && !status.error);

  const probes = data?.probes ?? [];
  const failing = probes.filter((probe) => probe.status !== "ok");
  const downProbes = failing.length;
  const processes = data?.processes ?? [];
  const shortfall = processes.filter((p) => p.healthy_instances < p.instances);
  const shortfallProcesses = shortfall.length;
  const notOk = data != null && (data.overall !== "ok" || downProbes > 0 || shortfallProcesses > 0);
  const pollSeconds = Math.round(PLATFORM_STATUS_POLL_MS / 1000);

  /* THE TWO INVENTORY TILES OWE A SUB-LINE TOO. The two urgency tiles carry
     one ("Every probe answered OK", "Every process is at full replica count")
     because a measured zero has to say what it means; these two had nothing
     under the figure, so at the row's shared height they showed an empty band
     -- most of the tile, at 390px where each is full width.

     What goes there is the fact the figure alone withholds, not filler:
     - a probe count says nothing about what the read COST, and the slowest
       dependency is the one an incident is usually about. It is a summary of
       the DURATION column below rather than a number invented here;
     - a module count says nothing about what those modules brought, and
       routes and job types are exactly the two columns the registry table
       carries.
     Both are `null` when there is nothing to summarise, so an empty registry
     draws no line rather than "0 routes · 0 job types" under a zero that
     already has a `zeroLabel` saying what it means. */
  /* AND THE OTHER TWO TILES ARE ONLY DRESSED AT ZERO. `zeroLabel` renders
     when the count is zero, which is the state the fixtures happen to show;
     a platform with a sealed vault has a bare "1" and the same empty band.
     A failure count's missing fact is what it is a failure count OF, so each
     names its denominator -- the pairing /devices and the dashboard already
     use ("of 14 enrolled"). The names of the failing probes are not repeated
     here: the red banner above already lists them with their reason codes. */
  const slowestProbe = probes.reduce<Probe | null>(
    (worst, probe) => (worst === null || probe.duration_ms > worst.duration_ms ? probe : worst),
    null,
  );
  const registrySummary =
    data && data.modules.length > 0
      ? `${t("platformStatus.tiles.routes", { count: data.modules.reduce((n, m) => n + m.routes, 0) })} · ${t(
          "platformStatus.tiles.jobTypes",
          { count: data.modules.reduce((n, m) => n + m.job_types, 0) },
        )}`
      : null;

  return (
    <>
      <PageHead
        title={t("platformStatus.page.title")}
        description={t("platformStatus.page.description")}
        actions={
          <>
            {data ? <StatusBadge status={data.overall} /> : null}
            <Btn
              label={status.isFetching ? t("platformStatus.page.reprobing") : t("platformStatus.page.reprobe")}
              icon="refresh"
              onClick={() => {
                void status.refetch();
              }}
            />
          </>
        }
      />

      {/* WHEN WAS THIS TRUE? The durations and the heartbeat ages below read as
          live and are frozen at the instant of the last read, and an operator
          during an incident cannot tell a page rendered ten minutes ago from
          one rendered now. The query has polled since it was written; the page
          never said so. */}
      <p className="note">
        {status.dataUpdatedAt
          ? t("platformStatus.page.readAt", {
              when: formatDateTime(new Date(status.dataUpdatedAt).toISOString(), zone),
              count: pollSeconds,
            })
          : t("platformStatus.page.rereadEvery", { count: pollSeconds })}
      </p>

      {/* THE VERDICT, AT THE WEIGHT OF THE VERDICT. A 51x23px pill in the
          corner, in the same style as eight passing badges, is not how a
          platform says it is down. The failing probes are NAMED here, so the
          banner is the diagnosis and not merely an alarm. */}
      {notOk ? (
        <div role="alert" className="notice red">
          <Icon name={KIND_ICON.crit} size={17} />
          <div>
            <strong>
              {downProbes > 0
                ? t("platformStatus.banner.probesFailing", { count: downProbes, total: probes.length })
                : t("platformStatus.banner.processesShort", { count: shortfallProcesses })}
              {failing.length > 0
                ? `: ${failing.map((probe) => `${probe.name} ${probe.reason ?? probe.status}`).join(", ")}`
                : ""}
              .
            </strong>
            <br />
            {failing.length > 0 ? (
              <>
                {guidanceFor(failing[0]!.reason) ?? t("platformStatus.banner.noReasonCode")}
                <br />
              </>
            ) : null}
            <Link className="btn small" to="/error-monitor?state=open">
              {t("platformStatus.banner.openErrors")}
            </Link>
          </div>
        </div>
      ) : null}

      {/* EVERY TILE OPENS THE ROWS IT COUNTED — which on this page means a
          section of this page, for the reason given at `useScrollToSection`:
          nothing else in the console renders a probe, a heartbeat or the
          registry, and this page has no filters for a tile to set. The first
          two therefore share `#dependencies`; they are the same table, and the
          honest thing is to say so rather than to invent a `?probe=down` this
          page does not read so that four tiles could have four hrefs.

          COLOUR IS CATEGORY: neutral for the two inventory counts (how many
          probes exist, how many modules registered — facts about the
          deployment, not about its health), red for a dependency that is
          failing outright, orange for a process that is answering but short of
          replicas. That distinction is this page's stated purpose: a service
          behind a load balancer with one dead replica is a partial outage, and
          it is not the same event as a sealed vault.

          WEIGHT IS URGENCY. Both failure counts carry it, and they are the only
          two numbers on this screen that mean somebody has to act now.

          A PENDING PROBE IS NOT A PASSING PROBE: `probes` defaults to `[]`, so
          before the first read every one of these rendered "0" and the row read
          as a clean bill of health for a platform nothing had measured. That is
          the exact shape the audit filed nine times over. Undefined data gets an
          em dash; a zero here is now always a measured zero, and its
          `zeroLabel` always true. */}
      <StatsGrid columns={4}>
        <StatCard
          label={t("platformStatus.tiles.probes")}
          value={data ? String(probes.length) : "—"}
          icon="status"
          to="/platform-status#dependencies"
          tone="neutral"
          zeroLabel={t("platformStatus.tiles.probesZero")}
          foot={
            slowestProbe
              ? t("platformStatus.tiles.slowest", {
                  probe: probeTitle(slowestProbe),
                  ms: slowestProbe.duration_ms,
                })
              : undefined
          }
        />
        <StatCard
          label={t("platformStatus.tiles.probesNotOk")}
          value={data ? String(downProbes) : "—"}
          icon="warning"
          to="/platform-status#dependencies"
          tone="red"
          urgent
          zeroLabel={t("platformStatus.tiles.probesNotOkZero")}
          foot={downProbes > 0 ? t("platformStatus.tiles.ofProbes", { count: probes.length }) : undefined}
        />
        <StatCard
          label={t("platformStatus.tiles.short")}
          value={data ? String(shortfallProcesses) : "—"}
          icon="server"
          to="/platform-status#processes"
          tone="orange"
          urgent
          zeroLabel={t("platformStatus.tiles.shortZero")}
          foot={
            shortfallProcesses > 0
              ? t("platformStatus.tiles.ofProcesses", { count: processes.length })
              : undefined
          }
        />
        <StatCard
          label={t("platformStatus.tiles.modules")}
          value={data ? String(data.modules.length) : "—"}
          icon="package"
          to="/platform-status#modules"
          tone="neutral"
          /* Registration, not health -- see the module docstring. A zero here
             is a platform that started with no modules, not a platform whose
             modules are down. */
          zeroLabel={t("platformStatus.tiles.modulesZero")}
          foot={registrySummary ?? undefined}
        />
      </StatsGrid>

      <QueryState
        loading={status.isLoading}
        error={status.error}
        empty={false}
        emptyLabel=""
      >
        {/* THE FRAGMENT TARGETS BELOW ARE REAL ELEMENTS. `Panel` takes no id,
            so the anchor is the wrapper; a tile pointing at `#dependencies`
            when no `#dependencies` exists scrolls nowhere, which is a dead
            tile that happens to have an href. */}
        <div id="dependencies">
          <Panel title={t("platformStatus.dependencies.title")} bare>
            <Table>
              <thead>
                <tr>
                  <Th>{t("platformStatus.dependencies.probe")}</Th>
                  <Th>{t("platformStatus.dependencies.status")}</Th>
                  <Th>{t("platformStatus.dependencies.duration")}</Th>
                  <Th>{t("platformStatus.dependencies.reason")}</Th>
                  <Th>{t("platformStatus.dependencies.detail")}</Th>
                </tr>
              </thead>
              <tbody>
                {probes.map((probe) => (
                  <ProbeRow key={probeKey(probe)} probe={probe} />
                ))}
              </tbody>
            </Table>
          </Panel>
        </div>

        <div id="processes">
          <Panel title={t("platformStatus.processes.title")} bare>
            <Table>
              <thead>
                <tr>
                  <Th>{t("platformStatus.processes.process")}</Th>
                  <Th>{t("platformStatus.processes.healthyInstances")}</Th>
                  <Th>{t("platformStatus.processes.status")}</Th>
                  <Th>{t("platformStatus.processes.oldestHeartbeat")}</Th>
                  <Th>{t("platformStatus.processes.version")}</Th>
                </tr>
              </thead>
              <tbody>
                {processes.map((proc) => (
                  <ProcessRow key={proc.process} process={proc} />
                ))}
              </tbody>
            </Table>
          </Panel>
        </div>

        <div id="modules">
          <Panel
            title={t("platformStatus.modules.title")}
            subtitle={t("platformStatus.modules.subtitle")}
            bare
          >
            <Table>
              <thead>
                <tr>
                  <Th>{t("platformStatus.modules.module")}</Th>
                  <Th>{t("platformStatus.modules.routes")}</Th>
                  <Th>{t("platformStatus.modules.jobTypes")}</Th>
                </tr>
              </thead>
              <tbody>
                {(data?.modules ?? []).map((mod) => (
                  <ModuleRow key={mod.name} module={mod} />
                ))}
              </tbody>
            </Table>
          </Panel>
        </div>
      </QueryState>
    </>
  );
}

/**
 * A PROBE'S DETAIL, IN THE READER'S ALPHABET.
 *
 * `detail` is a free-shaped bag the probe filled in, and it was printed as
 * `rls: enforced · sealed: false · bucket: itms-backups` -- a payload fragment
 * on the page an operator opens when the platform is broken. The key is
 * translated where the catalogue knows it (`humaniseEnum(key,
 * "PlatformProbeDetail")`, in both languages), and any other key reads as its
 * derived English.
 *
 * `rls` NEEDS NO ROW: the derivation said "Rls" until `lib/labels`'s
 * INITIALISMS table learned it, so this page spells it "RLS" the way the rest
 * of the console does, out of one list rather than two.
 *
 * A KEY THIS TABLE DOES NOT KNOW is still shown, humanised, never dropped:
 * the whole reason `/platform/status` may surface detail at all is that it is
 * the one place a measurement can be seen.
 */
function detailLabel(key: string): string {
  return humaniseEnum(key, "PlatformProbeDetail");
}

/** `false` is a fact a person reads as "no", not as a JSON literal. */
function detailValue(value: string | number | boolean): string {
  if (typeof value === "boolean") {
    return translate(value ? "platformStatus.dependencies.yes" : "platformStatus.dependencies.no");
  }
  return String(value);
}

function ProbeRow({ probe }: { probe: Probe }) {
  const t = useT();
  const detail = Object.entries(probe.detail ?? {});
  const kind = STATUS_KIND[probe.status];
  return (
    <tr>
      {/* v3's row: the glyph leads, and a failing probe carries the severity
          edge, so the row reads as a failure before the pill does. */}
      <Td lead={<Icon name={glyphFor(PROBE_GLYPH, probeKey(probe))} size={18} />} gutter={kind}>
        {/* THE SAME SHAPE AS THE REASON AND MODULE CELLS: the words first,
            the key under them. `object_store` and `keycloak_events` sat in
            snake case in a column of ordinary nouns, and the key is still on
            the row because it is what the health endpoint and the logs call
            this probe. */}
        <div className="cell-title">{probeTitle(probe)}</div>
        <div className="cell-sub mono">{probeKey(probe)}</div>
      </Td>
      <Td>
        <StatusBadge status={probe.status} />
      </Td>
      <Td>
        <span className="mono">{probe.duration_ms} ms</span>
      </Td>
      <Td>
        {probe.reason ? (
          <>
            {/* THE CODE STAYS, and the sentence is added beside it. The code is
                what an operator greps the health surface for; the sentence is
                what tells them what to do about it, which "unreachable" on its
                own never did. */}
            <div className="cell-title">{humaniseEnum(probe.reason, "ReasonCode")}</div>
            <div className="cell-sub mono">{probe.reason}</div>
            <div className="cell-sub">{guidanceFor(probe.reason)}</div>
          </>
        ) : (
          <NoValue label={t("platformStatus.dependencies.noReason")} />
        )}
      </Td>
      <Td>
        {detail.length === 0 ? (
          <NoValue label={t("platformStatus.dependencies.noDetail")} />
        ) : (
          <span className="cell-sub">
            {detail.map(([k, v]) => `${detailLabel(k)}: ${detailValue(v)}`).join(" · ")}
          </span>
        )}
      </Td>
    </tr>
  );
}

function ProcessRow({ process }: { process: ProcessHealth }) {
  const t = useT();
  const shortfall = process.healthy_instances < process.instances;
  const kind = STATUS_KIND[process.status];
  return (
    <tr>
      <Td lead={<Icon name={glyphFor(PROCESS_GLYPH, process.process)} size={18} />} gutter={kind}>
        <div className="cell-title">{process.process}</div>
      </Td>
      <Td>
        {/* A SHORTFALL IS ITS OWN FACT: a warn pill with its glyph AND
            different text ("1 / 3 healthy" against "2 / 2"), so the partial
            outage never rests on the status pill alone, nor on colour. */}
        {shortfall ? (
          <Pill kind="warn">
            {t("platformStatus.processes.healthy", { healthy: process.healthy_instances, total: process.instances })}
          </Pill>
        ) : (
          <span className="mono">{`${process.healthy_instances} / ${process.instances}`}</span>
        )}
      </Td>
      <Td>
        <StatusBadge status={process.status} />
      </Td>
      <Td>
        <span className="mono">{formatAge(process.oldest_age_s)}</span>
      </Td>
      <Td>
        {process.version ? (
          <span className="mono">{process.version}</span>
        ) : (
          <NoValue label={t("platformStatus.processes.noVersion")} />
        )}
      </Td>
    </tr>
  );
}

// NO STATUS BADGE. NO STATUS COLUMN. This row is intentionally the same shape
// as the registry itself (name, routes, job_types) so nothing here can be
// mistaken for a health signal -- see the module docstring above.
function ModuleRow({ module }: { module: RegisteredModule }) {
  useT();
  return (
    <tr>
      <Td lead={<Icon name="package" size={18} />}>
        {/* THE NAME A PERSON READS, WITH THE KEY UNDER IT -- the same shape
            the Reason column beside it uses. `service_desk` sat in snake case
            in a column of words; the registry key is still on the row, because
            it is what the registry, the logs and the API all call it. */}
        <div className="cell-title">{moduleLabel(module.name)}</div>
        <div className="cell-sub mono">{module.name}</div>
      </Td>
      <Td>
        <span className="mono">{module.routes}</span>
      </Td>
      <Td>
        <span className="mono">{module.job_types}</span>
      </Td>
    </tr>
  );
}
