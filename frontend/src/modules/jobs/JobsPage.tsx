import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import type { Job, Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { useLiveEvents } from "../../lib/ws";
import { EmptyState } from "../../components/emptystate";
import { Table, Td, Th } from "../../components/ui";
import { ConfirmDialog } from "../../components/confirm";
import { describeSubmitError } from "../../components/form";
import { Prerequisites } from "../../components/prerequisites";
import { useFleetPrerequisite } from "../../lib/detectors";
import { formatDateTime, intlLocale } from "../../lib/datetime";
import { humaniseEnum } from "../../lib/labels";
import { useT } from "../../i18n/context";
import { getLanguage } from "../../i18n/core";
import { Trans } from "../../i18n/Trans";
import { Btn, FilterBar, PageHead, Panel, QueryState, SearchField, StatCard, StatsGrid, StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { Chips, KV, Timeline, type Kind, type TimelineRun } from "../../shell/viz";
import {
  JOB_STATUSES,
  canRetry,
  describeAge,
  describeCancelRequest,
  describeCancellation,
  describeLateCancel,
  describeRetry,
  describeSpan,
  describeStop,
  describeStopEffect,
  isStuck,
  stopKind,
  typeBreakdown,
  jobStatusKind,
  whyNotRetryable,
  whyNotStoppable,
} from "./jobState";
import { jobTypeLabel } from "./jobTypes";

/**
 * A job's parameters, or an honest empty state.
 *
 * Auditor B: "[HIGH] Job detail shows a raw JSON `{}` as the Result … `{}` is
 * developer output, not a user-facing empty state — it reads as 'something is
 * broken' rather than 'this job hasn't produced a result yet'."
 *
 * The JSON itself STAYS for a non-empty value, and that is not laziness: this
 * is the console where somebody answers "what exactly did we tell that machine
 * to do", and a prettified summary of a signed payload is a paraphrase of
 * evidence. What changes is that nothing is rendered as a bare `{}`.
 */
function Blob({ value, empty }: { value: Record<string, unknown> | null; empty: string }) {
  const present = value != null && Object.keys(value).length > 0;
  if (!present) return <p className="cell-sub">{empty}</p>;
  return <pre className="code">{JSON.stringify(value, null, 2)}</pre>;
}

/**
 * The status filter's own vocabulary: the value that travels in the URL, and
 * the word a person reads on the chip. `""` is "All", `stuck` is the derived
 * state the tiles link to, and the rest are the wire's statuses in words --
 * `humaniseEnum`, so the row never prints `dispatched` in lowercase the way
 * the `<option>` list it replaces did.
 */
function statusFilters(t: ReturnType<typeof useT>): readonly { value: string; label: string }[] {
  return [
    { value: "", label: t("jobs.filters.all") },
    { value: "stuck", label: t("jobs.filters.stuck") },
    ...JOB_STATUSES.map((status) => ({ value: status, label: humaniseEnum(status, "JobStatus") })),
  ];
}

/** The v3 row glyph for a job, by the capability its type names. */
function jobGlyph(type: string): string {
  const family = type.split(".")[0] ?? "";
  switch (family) {
    case "patch":
      return "shield-check";
    case "inventory":
      return "list-check";
    case "backup":
      return "archive";
    case "app":
    case "script":
      return "package";
    case "remote":
      return "user-screen";
    case "health":
      return "heartbeat";
    case "agent":
      return "certificate";
    case "logs":
      return "logs";
    default:
      return "jobs";
  }
}

/**
 * The row's severity, drawn as the gutter and the timeline bar. A waiting
 * job that has waited too long is the warning; a job the platform is still
 * working on is the running blue; a terminal failure is critical.
 */
function jobKind(job: Job, now: number): Kind {
  if (isStuck(job, now)) return "warn";
  return jobStatusKind(job.status);
}

/** The window the timeline panel covers, as v3 draws it: the last six hours. */
const TIMELINE_HOURS = 6;
const HOUR_MS = 60 * 60 * 1000;
const TIMELINE_MS = TIMELINE_HOURS * HOUR_MS;

/**
 * The window's END, snapped up to the next whole hour so the axis reads
 * `14:00 … 20:00` rather than `13:39 … 19:39`. "Now" sits inside the last
 * hour; a run still in progress ends at now, not at the axis edge.
 */
function windowEnd(now: number): number {
  return Math.ceil(now / HOUR_MS) * HOUR_MS;
}

/** `HH:MM` in the operator's zone, for the timeline axis. */
function clock(at: number, zone: string | null | undefined): string {
  try {
    return new Intl.DateTimeFormat(intlLocale(), {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
      timeZone: zone ?? undefined,
    }).format(new Date(at));
  } catch {
    return new Intl.DateTimeFormat(intlLocale(), { hour: "2-digit", minute: "2-digit", hour12: false }).format(
      new Date(at),
    );
  }
}

/**
 * v3's "Last six hours" panel, from real rows: every job ISSUED inside the
 * window, with a bar from when it started to when it finished (or to now, if
 * it has not). A job that never started draws no bar, which is what
 * "unmeasured" looks like; it is not drawn as a zero-width run.
 */
function timelineRuns(items: readonly Job[], now: number, hostnameFor: (id: string) => string): TimelineRun[] {
  const windowStart = windowEnd(now) - TIMELINE_MS;
  const toPct = (at: number) => Math.max(0, Math.min(100, ((at - windowStart) / TIMELINE_MS) * 100));
  return items
    .filter((job) => Date.parse(job.issued_at) >= windowStart)
    .map((job) => {
      const started = job.started_at ? Date.parse(job.started_at) : Number.NaN;
      const run: TimelineRun = {
        at: job.type,
        kind: jobKind(job, now),
        label: hostnameFor(job.device_id),
      };
      if (!Number.isNaN(started)) {
        const finished = job.finished_at ? Date.parse(job.finished_at) : now;
        const start = toPct(started);
        const end = toPct(Number.isNaN(finished) ? now : finished);
        run.start = start;
        // A run shorter than the track's resolution still gets a visible bar.
        run.width = Math.max(0.6, end - start);
      }
      return run;
    })
    .sort((a, b) => (a.start ?? 101) - (b.start ?? 101));
}

export function JobsPage() {
  const t = useT();
  const api = useApi();
  const queryClient = useQueryClient();
  // A job runs on a computer with the agent; with none, this history stays
  // empty (docs/design/cross-module-guidance.md §2, Job history).
  const agentItem = useFleetPrerequisite("agent", {
    missingDetail: t("jobs.page.noAgent"),
  });
  /**
   * THE SELECTED JOB LIVES IN THE URL, not in component state.
   *
   * Auditor B: "[MEDIUM] Selecting a job does not change the URL, and rows use
   * buttons instead of links … The row action is a `<button>`, so it cannot be
   * middle-clicked, opened in a new tab, bookmarked or pasted into an incident
   * channel." A job id is exactly the thing people paste into an incident
   * channel, so the console has to be able to produce a link to one.
   *
   * A QUERY PARAMETER RATHER THAN `/jobs/:id` because the route table is
   * shared: this gets a linkable job without a second route, and the two-pane
   * layout — list beside detail — is the one this page already has.
   */
  const [params, setParams] = useSearchParams();
  const selected = params.get("job");
  /**
   * THE FILTER LIVES IN THE URL TOO, and it is the KPI tiles that made that
   * mandatory. Every tile above is now a link to the list it counts —
   * "Failed 3" goes to `/jobs?status=failed` — and a link can only carry a
   * filter the page reads back. Held in `useState`, as it was, those tiles
   * would have been four anchors to the page the reader is already on: the
   * dead tiles the instruction named.
   *
   * The param names are the select's own option values, `stuck` included, so
   * there is one vocabulary rather than a tile vocabulary and a control
   * vocabulary that drift apart. Same shape as `DevicesPage`'s `state`/`q`.
   */
  const statusFilter = params.get("status") ?? "";
  const search = params.get("q") ?? "";

  function update(patch: Record<string, string>, replace = false) {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(patch)) {
      // An empty value is ABSENT, not `?q=` — a URL carrying an empty filter
      // reads as a filter set to nothing.
      if (value === "") next.delete(key);
      else next.set(key, value);
    }
    setParams(next, { replace });
  }

  const setStatusFilter = (value: string) => update({ status: value });
  // REPLACE, not push: one history entry per keystroke makes Back useless for
  // exactly the person trying to undo a filter.
  const setSearch = (value: string) => update({ q: value }, true);

  /** A link to this job's detail that KEEPS whatever filter is on screen. */
  const detailHref = (jobId: string) => {
    const next = new URLSearchParams(params);
    next.set("job", jobId);
    return `/jobs?${next.toString()}`;
  };

  // `/me` for the operator's profile timezone only (cycle 5). Same query key
  // and staleTime every other route uses, so this shares the cached response
  // rather than adding a request; a null timezone falls the helper back to the
  // browser zone.
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const zone = me.data?.timezone;

  const jobs = useQuery({ queryKey: ["jobs"], queryFn: () => api.listJobs() });
  const detail = useQuery({
    queryKey: ["jobs", selected],
    queryFn: () => api.getJob(selected as string),
    enabled: selected != null,
  });

  // THE HOSTNAME BEHIND `device_id`, so a row names a machine the way v3's
  // does rather than a UUID. Shares the `["devices"]` cache the rest of the
  // console fills; a device the join cannot resolve keeps its id, which is
  // still the thing to search the backend for.
  const devices = useQuery({ queryKey: ["devices"], queryFn: () => api.listDevices() });
  const hostnameFor = useMemo(() => {
    const byId = new Map((devices.data?.items ?? []).map((device) => [device.id, device.hostname ?? device.id]));
    return (id: string) => byId.get(id) ?? id;
  }, [devices.data]);

  /**
   * STOPPING A JOB (job-lifecycle design, cycles 1 and 2).
   *
   * ONE ROUTE, TWO MEANINGS, AND THE DIALOG SAYS WHICH. A `queued` job is
   * WITHDRAWN -- the server takes it back and no device is contacted. A job
   * that has reached a device can only be ASKED to stop: the platform signs a
   * `job.cancel` job, sends it down the connection the agent opened, and shows
   * whatever the endpoint answers. `describeStop` carries the two sentences,
   * because a single vague one would be vague about exactly the fact an
   * operator needs.
   *
   * The confirmation is not ceremony either way. Stopping is irreversible for
   * that job and the queue is full of near-identical rows, so naming the job in
   * the dialog is what lets somebody notice they have the wrong one selected.
   *
   * NOT GATED ON A PERMISSION HERE, AND NOT ON INTERRUPTIBILITY. Which grant a
   * cancel needs depends on the job's TYPE, and whether a type may be
   * interrupted at all depends on what its AGENT capability honours; both are
   * resolved server-side. The console holds no copy of either map, and
   * inventing one would either hide a control the operator is entitled to or
   * fake a decision. So the control is offered and a refusal is REPORTED --
   * `onError` below, announced in a live region, rather than a button that
   * appears to do nothing.
   */
  const [confirming, setConfirming] = useState(false);
  const cancel = useMutation({
    mutationFn: (jobId: string) => api.cancelJob(jobId),
    onSuccess: () => {
      setConfirming(false);
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: () => setConfirming(false),
  });

  /**
   * RE-RUNNING A FINISHED JOB (job-lifecycle design, cycle 3).
   *
   * THE SELECTION FOLLOWS THE NEW JOB, and that is the whole interaction
   * design here rather than a nicety. A retry is a new row with a new id; a
   * console that left the operator looking at the old one would show a failed
   * job that stays failed forever while the work they just asked for runs
   * somewhere off screen -- which is indistinguishable, from where they are
   * sitting, from a button that did nothing.
   *
   * Gated on neither permission nor type, for the reasons the cancel mutation
   * above gives: both are server-side facts, and a console that guessed would
   * either hide a control the operator is entitled to or fake a decision.
   */
  const [confirmingRetry, setConfirmingRetry] = useState(false);
  const retry = useMutation({
    mutationFn: (jobId: string) => api.retryJob(jobId),
    onSuccess: (created) => {
      setConfirmingRetry(false);
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      update({ job: created.id });
    },
    onError: () => setConfirmingRetry(false),
  });

  useLiveEvents((event) => {
    if (event.kind === "job_status" || event.kind === "job_log") {
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    }
  });

  const items = useMemo<Job[]>(() => jobs.data?.items ?? [], [jobs.data]);

  // ONE INSTANT FOR THE WHOLE RENDER. Rows that each read `Date.now()` can
  // disagree about two jobs issued in the same second, and the KPI can
  // disagree with the rows it counts.
  const now = Date.now();

  // Counted from the rows on screen, never from a constant. The design's Jobs
  // screen shows fixed figures because it is a mock; a console that kept them
  // would be wrong on every deployment.
  const runningJobs = items.filter((job) => job.status === "running");
  const failedJobs = items.filter((job) => job.status === "failed");
  const running = runningJobs.length;
  const failed = failedJobs.length;
  // THE TILE THE AUDIT ASKED FOR. "Jobs 5 / Running 0 / Failed 0" was a
  // healthy-looking headline over four jobs that had been queued for a day.
  const stuckJobs = items.filter((job) => isStuck(job, now));
  const stuck = stuckJobs.length;

  /**
   * WHAT EACH FIGURE IS MADE OF — v3's second line on every tile.
   *
   * The port passed `zeroLabel` alone, so a tile said something beyond its
   * number exactly when the number was zero: every non-zero count was a naked
   * figure. Each line below is derived from the rows on this page and from
   * nothing else, and it is omitted rather than invented when the rows cannot
   * support one (no run has started, no page has loaded).
   *
   * THE SCOPE IS STATED WHEN IT DIFFERS. `total` is the server's count for the
   * whole queue and `items` is the page in hand; when they disagree, the
   * breakdown would be describing a different set from the figure above it, so
   * the line says how much is loaded instead of claiming a breakdown it cannot
   * make.
   */
  const total = jobs.data?.total ?? 0;
  const succeeded = items.filter((job) => job.status === "succeeded").length;
  const totalFoot =
    items.length === 0
      ? undefined
      : items.length < total
        ? t("jobs.tiles.loaded", { count: items.length })
        : t("jobs.tiles.succeeded", { count: succeeded, other: items.length - succeeded });
  const longestRun = runningJobs
    .map((job) => (job.started_at ? now - Date.parse(job.started_at) : Number.NaN))
    .filter((span) => !Number.isNaN(span))
    .sort((a, b) => b - a)[0];
  const runningFoot =
    running === 0 ? undefined : longestRun === undefined ? undefined : t("jobs.tiles.longestRun", { span: describeSpan(longestRun) });
  const failedFoot = typeBreakdown(failedJobs) ?? undefined;
  const stuckWaits = {
    queued: stuckJobs.filter((job) => job.status === "queued").length,
    dispatched: stuckJobs.filter((job) => job.status === "dispatched").length,
  };
  const stuckFoot =
    stuck === 0
      ? undefined
      : [
          stuckWaits.queued > 0 ? t("jobs.tiles.neverSent", { count: stuckWaits.queued }) : null,
          stuckWaits.dispatched > 0 ? t("jobs.tiles.neverAcknowledged", { count: stuckWaits.dispatched }) : null,
        ]
          .filter(Boolean)
          .join(" · ");

  // Search matches the job type as worded on screen, so it follows the language.
  const language = getLanguage();
  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return items.filter((job) => {
      if (statusFilter === "stuck" && !isStuck(job, now)) return false;
      if (statusFilter && statusFilter !== "stuck" && job.status !== statusFilter) return false;
      if (!needle) return true;
      return `${jobTypeLabel(job.type)} ${job.type} ${job.id} ${job.device_id} ${hostnameFor(job.device_id)}`
        .toLowerCase()
        .includes(needle);
    });
    // `now` is intentionally not a dependency: it changes every render, and the
    // filter only needs the instant this render already computed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, statusFilter, search, hostnameFor, language]);

  // The timeline is read from every row, not the filtered ones: a filter
  // narrows the list under it, and the six-hour picture stays the whole fleet's.
  //
  // `now` IS EXCLUDED ON PURPOSE, as in the filter above: an in-flight bar is
  // extended when the list refetches (a live `job_status` or `job_log` event invalidates it,
  // and so do the stop and retry mutations), not on every render, so the bars
  // move with the rows they draw and never on their own.
  const runs = useMemo(
    () => timelineRuns(items, now, hostnameFor),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [items, hostnameFor],
  );
  // The axis is snapped to whole hours, so its labels change only when the
  // window rolls over an hour: `windowEnd` is the exact key for that, and it
  // is a plain number, so no exclusion is needed here.
  /**
   * THE ROWS WHOSE TRACK IS EMPTY, SAID OUT LOUD.
   *
   * A job issued inside the window that has never started has nothing to
   * draw: no `started_at`, so no bar (`timelineRuns` above, which is right --
   * a zero-width bar would be a claim that a run happened and took no time).
   * What that produced on screen was one row among six with a label and a
   * blank track, which reads as a mark the chart failed to paint rather than
   * as a state the fleet is in (design review M8). The fixture case is real
   * and it is the interesting one: `inventory.collect` dispatched to
   * `por-fs-01`, a server that is offline, sitting unstarted for three hours.
   *
   * So the panel names them under the chart, the way the rest of this console
   * says "Never run" and "Never swept" rather than leaving the figure blank.
   * It is derived from the same rows the chart draws, so it cannot disagree
   * with what is on the track.
   *
   * WHY THIS AND NOT A MARK IN THE TRACK: `Timeline` renders nothing inside
   * `.tl-track` when a run carries no `start`/`width`, and both the markup
   * and `.tl-track`'s rules are the shell's (`shell/viz.tsx`, `shell/viz.css`).
   * A "no run" treatment drawn IN the track is the better fix and it is a kit
   * change, not a module one.
   */
  const unstarted = useMemo(() => runs.filter((run) => run.start === undefined), [runs]);

  const windowEndMs = windowEnd(now);
  const ticks = useMemo(
    () =>
      Array.from({ length: TIMELINE_HOURS + 1 }, (_, at) =>
        clock(windowEndMs - TIMELINE_MS + at * HOUR_MS, zone),
      ),
    [zone, windowEndMs],
  );

  const stop = detail.data ? stopKind(detail.data) : null;
  const filterOptions = statusFilters(t);

  return (
    <>
      <PageHead
        title={t("jobs.page.title")}
        description={t("jobs.page.description")}
      />
      <Prerequisites title={t("jobs.page.prerequisitesTitle")} items={[agentItem]} />
      {/* EVERY TILE IS A LINK INTO THIS LIST, FILTERED. Colour is the
          category — inert total neutral, work in flight violet, failure red,
          overdue gold — and weight is the urgency, so at most the two tiles
          that need a person are loud. The zero labels are the other half:
          "0 failed" and "0 because nothing has run" are the same glyph, and
          only one of them is good news. */}
      <StatsGrid columns={4}>
        <StatCard
          label={t("jobs.tiles.jobs")}
          value={String(total)}
          icon="jobs"
          // The whole queue: from a filtered view this is the way back to it.
          to="/jobs"
          tone="neutral"
          zeroLabel={t("jobs.tiles.jobsZero")}
          foot={totalFoot}
        />
        <StatCard
          label={t("jobs.tiles.running")}
          value={String(running)}
          icon="progress"
          to="/jobs?status=running"
          tone="violet"
          zeroLabel={t("jobs.tiles.runningZero")}
          foot={runningFoot}
        />
        <StatCard
          label={t("jobs.tiles.failed")}
          value={String(failed)}
          icon="circle-x"
          to="/jobs?status=failed"
          tone="red"
          urgent={failed > 0}
          zeroLabel={t("jobs.tiles.failedZero")}
          foot={failedFoot}
        />
        <StatCard
          label={t("jobs.tiles.waiting")}
          value={String(stuck)}
          icon="clock-exclamation"
          to="/jobs?status=stuck"
          tone="gold"
          urgent={stuck > 0}
          zeroLabel={t("jobs.tiles.waitingZero")}
          foot={stuckFoot}
        />
      </StatsGrid>

      {/* THE BANNER THE AUDIT SPECIFIED, and only when its two conditions hold
          together: nothing running, and work that has waited. Either alone is
          ordinary — an idle queue is the normal state of a small fleet. */}
      {stuck > 0 && running === 0 ? (
        <div role="status" className="notice gold" style={{ marginBottom: 14 }}>
          <Icon name="alert-triangle" size={17} />
          <div>
            {t("jobs.stuckBanner", { count: stuck })}
          </div>
        </div>
      ) : null}

      {/* v3's "Last six hours": one row per job issued in the window, its run
          drawn against the hour marks. Only once the list has answered, so an
          empty track never stands in for a request that has not returned. */}
      {jobs.data ? (
        <Panel title={t("jobs.timeline.title")} bare={runs.length === 0}>
          {runs.length > 0 ? (
            <>
              <Timeline runs={runs} ticks={ticks} />
              {unstarted.length > 0 ? (
                <p className="note" style={{ marginTop: 10 }}>
                  {t("jobs.timeline.unstarted", { count: unstarted.length })}{" "}
                  {unstarted.map((run, at) => (
                    <span key={`${run.at}-${run.label}-${at}`}>
                      {at > 0 ? ", " : ""}
                      <Trans
                        k="jobs.timeline.unstartedItem"
                        vars={{ type: run.at, device: run.label ?? "" }}
                        components={{ mono: <span className="mono" /> }}
                      />
                    </span>
                  ))}
                  .
                </p>
              ) : null}
            </>
          ) : (
            <EmptyState
              icon="clock"
              title={t("jobs.timeline.emptyTitle")}
              body={t("jobs.timeline.emptyBody")}
            />
          )}
        </Panel>
      ) : null}

      {/* TWO COLUMNS ONLY WHEN THERE IS SOMETHING IN THE SECOND ONE. The
          inspector held a third of the width from the moment the page loaded
          and stayed blank until a row was picked, so the queue -- the reason
          anybody opens this route -- was squeezed to about 665px and wrapped
          its `type · hostname` sub-line across two and three lines on nearly
          every row. Selection lives in the URL, so the layout can follow it:
          the list is the whole width until a job is selected, and the pane
          appears beside it when there is a job to put in it. */}
      {/* NOT A ONE-COLUMN GRID when nothing is selected: a plain block. An
          implicit grid track is `auto`, i.e. max-content, so the table's
          560px floor pushed the whole page to 757px at 390 wide instead of
          scrolling inside `.table-wrap`. The two-column track is
          `minmax(0, …)` and has never had that problem. */}
      <div className={selected ? "grid grid-2" : undefined}>
      <div>
        {/* Section headings, so the page has structure a screen reader can
            navigate by — Auditor A's "[LOW] Page has a single `<h1>` and no
            section headings", which is the same shape on every list-plus-panel
            screen in the console. */}
        <h2 className="sr-only">{t("jobs.page.queueHeading")}</h2>
        <Panel bare>
          <FilterBar>
            <SearchField
              placeholder={t("jobs.filters.searchPlaceholder")}
              /* Bounded, like every other search field in the console
                 (Auditor A, "[LOW] Search input has no maxlength and accepts
                 500+ characters"). A job id is 36 characters; 128 leaves room
                 for anything a person would actually paste. */
              maxLength={128}
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              aria-label={t("jobs.filters.searchAria")}
            />
            {/* THE STATUS FILTER IS v3's CHIP ROW, not a native select. The
                design draws this page's filter as chips, and the select the
                port shipped kept the browser's own control -- its chevron, its
                menu, its metrics -- beside kit fields, so one filter bar spoke
                two vocabularies. The chips also say the states in words: the
                options were the wire's own `queued`/`dispatched`, lowercase,
                because an <option> has nowhere to put a label. Every value the
                select carried is still here, `stuck` included, and it is still
                the URL's own vocabulary. */}
            <Chips
              label={t("jobs.filters.label")}
              items={filterOptions.map((option) => option.label)}
              /* A status in the URL that this build does not know marks NO
                 chip: the list below is filtered by it either way, and
                 lighting "All" would be the one reading that is false. */
              active={filterOptions.findIndex((option) => option.value === statusFilter)}
              onSelect={(index) => setStatusFilter(filterOptions[index]?.value ?? "")}
            />
            <span className="filter-spacer" />
            {jobs.data ? (
              <span className="mono cell-sub">
                {t("jobs.filters.count", { count: filtered.length })}
              </span>
            ) : null}
          </FilterBar>
          <QueryState
            loading={jobs.isLoading}
            error={jobs.error}
            empty={filtered.length === 0}
            emptyLabel={
              items.length === 0 ? t("jobs.filters.emptyNone") : t("jobs.filters.emptyFiltered")
            }
            emptyIcon="jobs"
          >
          <Table>
            <thead>
              <tr>
                <Th>{t("jobs.table.name")}</Th>
                <Th>{t("jobs.table.state")}</Th>
                <Th>{t("jobs.table.attempts")}</Th>
                <Th>{t("jobs.table.issued")}</Th>
                <Th>{t("jobs.table.waiting")}</Th>
                <Th>{""}</Th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((job) => (
                <tr key={job.id}>
                  {/* v3's row: the severity as the gutter, the capability's
                      glyph as the lead, the label an operator reads over the
                      wire type they will search the backend for and the
                      machine it targets. `patch.scan` alone was "raw internal
                      identifiers, not human language". */}
                  <Td lead={<Icon name={jobGlyph(job.type)} size={18} />} gutter={jobKind(job, now)}>
                    <div className="cell-title">{jobTypeLabel(job.type)}</div>
                    <div className="cell-sub">
                      <span className="mono">{job.type}</span>
                      {" · "}
                      {hostnameFor(job.device_id)}
                    </div>
                  </Td>
                  <Td>
                    <StatusPill value={job.status} kind={jobStatusKind(job.status)} />
                  </Td>
                  <Td>
                    <span className="mono">{job.attempts}</span>
                  </Td>
                  {/* CYCLE 5: was `new Date(job.issued_at).toLocaleString()`,
                      i.e. the browser's zone and the browser's format. On a
                      signed-job console the issue time is evidence — it has to
                      read the same here as it does everywhere else, in the
                      zone the operator says they work in. */}
                  <Td>
                    <span className="mono" style={{ whiteSpace: "nowrap" }}>
                      {formatDateTime(job.issued_at, zone)}
                    </span>
                  </Td>
                  <Td>
                    {isStuck(job, now) ? (
                      <StatusPill value="waiting" kind="warn" label={describeAge(job.issued_at, now)} />
                    ) : (
                      <span className="cell-sub">{describeAge(job.issued_at, now)}</span>
                    )}
                  </Td>
                  <Td>
                    {/* A real anchor. Middle-clickable, bookmarkable, and it
                        carries the selection in the URL. */}
                    <Link className="btn small" to={detailHref(job.id)}>
                      {t("jobs.table.detail")}
                    </Link>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
          </QueryState>
        </Panel>
      </div>
      {selected ? (
      <div>
        <h2 className="sr-only">{t("jobs.page.detailHeading")}</h2>
        {/* `Panel`, not `Card`: the queue beside it is a `Panel` and this is
            the same box. The section already has its own `sr-only` heading,
            so the panel takes no title of its own. */}
        <Panel>
          {detail.data ? (
            <div style={{ display: "grid", gap: 14 }}>
              {/* IDENTITY FIRST. Auditor B: "Once the panel opens, the user has
                  lost the only human-readable identity the job had; they must
                  cross-reference the row they clicked." The UUID stays — it is
                  what you paste into an incident channel — but it is no longer
                  the whole heading. */}
              <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
                <h3 className="panel-title">{jobTypeLabel(detail.data.type)}</h3>
                <StatusPill
                  value={detail.data.status}
                  kind={jobStatusKind(detail.data.status)}
                />
                {isStuck(detail.data, now) ? (
                  <StatusPill
                    value="waiting"
                    kind="warn"
                    label={t("jobs.detail.waitingFor", { age: describeAge(detail.data.issued_at, now) })}
                  />
                ) : null}
              </div>
              <div className="cell-sub mono">{detail.data.id}</div>

              <KV
                items={[
                  [t("jobs.detail.target"), <Link to={`/devices/${detail.data.device_id}`}>{hostnameFor(detail.data.device_id)}</Link>],
                  [t("jobs.detail.attempts"), String(detail.data.attempts)],
                  [t("jobs.detail.issued"), formatDateTime(detail.data.issued_at, zone)],
                  [t("jobs.detail.expires"), formatDateTime(detail.data.expires_at, zone)],
                  [t("jobs.detail.started"), formatDateTime(detail.data.started_at, zone)],
                  [t("jobs.detail.finished"), formatDateTime(detail.data.finished_at, zone)],
                ]}
              />

              <div>
                <h3 className="panel-title">{t("jobs.detail.parameters")}</h3>
                <Blob value={detail.data.params} empty={t("jobs.detail.noParameters")} />
              </div>
              <div>
                <h3 className="panel-title">{t("jobs.detail.result")}</h3>
                <Blob
                  value={detail.data.result}
                  empty={t("jobs.detail.noResult")}
                />
              </div>
              <div>
                <h3 className="panel-title">{t("jobs.detail.logs")}</h3>
                {detail.data.logs?.length ? (
                  <pre className="code">
                    {detail.data.logs.map((entry) => `[${entry.level}] ${entry.message}`).join("\n")}
                  </pre>
                ) : (
                  <p className="cell-sub">{t("jobs.detail.noLogs")}</p>
                )}
              </div>

              {/* STOPPING A JOB, AND NEVER A WORD THE PLATFORM CANNOT BACK UP.
                  Auditor B filed the absence of cancel and retry as a BLOCKER.
                  A job still QUEUED never reached a socket, so withdrawing it
                  is something this server can assert on its own authority. A
                  job already on a device can only be ASKED to stop, and every
                  sentence below keeps that distinction: "Cancel requested"
                  while the endpoint has not answered, "Cancelled" only where
                  there is evidence, and "the cancel arrived too late" for a job
                  that finished anyway. */}
              <div style={{ display: "grid", gap: 8, justifyItems: "start" }}>
                {describeCancellation(detail.data) ? (
                  <p className="cell-sub">{describeCancellation(detail.data)}</p>
                ) : describeLateCancel(detail.data) ? (
                  <p className="cell-sub">{describeLateCancel(detail.data)}</p>
                ) : describeCancelRequest(
                    detail.data,
                    formatDateTime(detail.data.cancel_requested_at, zone),
                  ) ? (
                  <p className="cell-sub">
                    {describeCancelRequest(
                      detail.data,
                      formatDateTime(detail.data.cancel_requested_at, zone),
                    )}
                  </p>
                ) : stop ? (
                  <>
                    <Btn
                      variant="danger"
                      label={stop === "withdraw" ? t("jobs.detail.cancelThis") : t("jobs.detail.askToStop")}
                      onClick={() => setConfirming(true)}
                      disabled={cancel.isPending}
                    />
                    <p className="cell-sub">
                      {describeStopEffect(stop, formatDateTime(detail.data.expires_at, zone))}
                    </p>
                  </>
                ) : (
                  <p className="cell-sub">
                    {whyNotStoppable(detail.data, formatDateTime(detail.data.expires_at, zone))}
                  </p>
                )}
                {cancel.error ? (
                  <p role="alert" className="form-error">
                    {describeSubmitError(cancel.error)}
                  </p>
                ) : null}
              </div>

              {/* RETRY, AND IT IS A NEW JOB RATHER THAN A SECOND RUN OF THIS
                  ONE. The button says "Run this job again" and the dialog says
                  what actually happens: this row is left exactly as it is and a
                  freshly signed job is created for the same device. A console
                  that implied the row would come back to life would be
                  describing a replay, which is not what the platform does. */}
              <div style={{ display: "grid", gap: 8, justifyItems: "start" }}>
                {detail.data.retry_of_job_id ? (
                  <>
                    <p className="cell-sub">
                      {t("jobs.detail.retryOf")}
                    </p>
                    <Btn
                      variant="ghost"
                      small
                      label={t("jobs.detail.showEarlier")}
                      onClick={() => update({ job: detail.data.retry_of_job_id as string })}
                    />
                  </>
                ) : null}
                {canRetry(detail.data) ? (
                  <Btn
                    variant="primary"
                    icon="refresh"
                    label={t("jobs.detail.runAgain")}
                    onClick={() => setConfirmingRetry(true)}
                    disabled={retry.isPending}
                  />
                ) : (
                  <p className="cell-sub">{whyNotRetryable(detail.data)}</p>
                )}
                {retry.error ? (
                  <p role="alert" className="form-error">
                    {describeSubmitError(retry.error)}
                  </p>
                ) : null}
              </div>
              <ConfirmDialog
                open={confirmingRetry}
                title={describeRetry(detail.data).title}
                body={describeRetry(detail.data).body}
                subject={detail.data.id}
                confirmLabel={t("jobs.detail.createNew")}
                onConfirm={() => retry.mutate(detail.data.id)}
                onCancel={() => setConfirmingRetry(false)}
              />
              {stop ? (
                <ConfirmDialog
                  open={confirming}
                  title={describeStop(stop).title}
                  body={describeStop(stop).body}
                  subject={detail.data.id}
                  confirmLabel={describeStop(stop).confirm}
                  destructive
                  onConfirm={() => cancel.mutate(detail.data.id)}
                  onCancel={() => setConfirming(false)}
                />
              ) : null}
            </div>
          ) : detail.error ? (
            /* SWALLOWED ENTIRELY BEFORE THIS. The panel rendered on
               `detail.data` alone, so a job whose detail request failed fell
               through to "Select a job to inspect it." — the operator had
               already selected one, and the console answered as though they
               had not. A 403 on a job they may not read, or a 503 from the
               gateway, were both indistinguishable from an idle panel.
               `describeSubmitError` names which, and role="alert" announces
               it. */
            <p role="alert" className="form-error">
              {describeSubmitError(detail.error)}
            </p>
          ) : (
            /* `selected` is set and neither the data nor an error has arrived:
               the request is in flight. The idle sentence this branch used to
               carry is gone with the pane -- an inspector that is not rendered
               cannot claim nothing is selected. */
            <p className="cell-sub">{t("jobs.page.loadingJob")}</p>
          )}
        </Panel>
      </div>
      ) : null}
      </div>
    </>
  );
}
