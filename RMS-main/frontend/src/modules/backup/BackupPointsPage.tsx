/**
 * Backup points, on the v3 kit: the design's `P['backup']` head (the Run
 * form stands where the design draws its "Run now" primary), the four
 * tiles, and the "Restore points" panel as a bare list led by the device's
 * platform glyph, each row carrying its verification pill and its Restore.
 *
 * HONESTY ABOUT WHAT THIS SCREEN SHOWS. This is a record of backup points the
 * control plane has, not a claim that backup works end to end. As of this
 * migration `backup.run` has never completed end to end on any platform: the
 * snapshot providers exist and are wired, but nobody has run one on Windows or
 * macOS, the Linux LVM path only exercises via a skipped integration test, and
 * a Linux root not on LVM still refuses (docs/modules/backup/spec.md §6.1;
 * HANDOFF.md §5). The prefix-scoped restore READ leg is built and unit-tested
 * but has never run against a real object store. Nothing on this page asserts
 * otherwise: it renders whatever state the API returns, including "no backup
 * points" and "the request failed", and says nothing about whether a run
 * would succeed.
 *
 * WHAT THE DESIGN ASKS FOR THAT THE API DOES NOT SERVE. The design's tiles
 * read Covered / Run success 24h / Awaiting verification / Stored, and its
 * rows carry a "Sample" (chunks verified) column. `GET /backup/report`
 * (useBackupReport) serves devices_total/devices_covered, coverage_rate,
 * run_counts_by_state, success_rate, points_needing_verification, and a dedup
 * report, so:
 *
 * - "Backup success" -> success_rate over every run, not the last 24h: the
 *   report carries no time window and inventing one client-side would count
 *   only what the list has paginated into view.
 * - "Protected endpoints" -> devices_covered of devices_total.
 * - "Failed jobs" -> run_counts_by_state.failed.
 * - "Needs verification" -> points_needing_verification.
 * - "Stored" and "Sample" are OMITTED: no endpoint returns repository
 *   capacity, and a point carries no chunk-sample count.
 */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { Form, FormActions, attemptProblems } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { EntityPicker } from "../../components/picker";
import { Button, Dialog, Table, Td, Th } from "../../components/ui";
import { formatDateTime } from "../../lib/datetime";
import { useBackupPoints, useBackupReport, useBackupRepositories, useDevices, useRunBackupMutation } from "./api";
import {
  backupPointStateKind,
  backupPointStateLabel,
  notCrashConsistent,
  restoreTestLabel,
  warningText,
} from "./backupState";
import { NoValue } from "../../components/emptystate";
import { formatBytes } from "../../lib/format";
import { ADD_COMPUTER_TO } from "../../shell/nav";
import { OsIcon } from "../../shell/viz";
import {
  devicePickerEmptyHint,
  devicePickerOptions,
  nameForOption,
  repositoryPickerOptions,
} from "./pickerOptions";
import { RestoreDialog } from "./RestoreDialog";
import { runQueuedMessage } from "./runMessage";
import type { BackupPoint } from "./types";
import { BackupPageNav } from "./backupNav";
import { BackupReadiness } from "./BackupReadiness";
import {
  Btn,
  FilterBar,
  PageHead,
  Panel,
  QueryState,
  StatCard,
  StatsGrid,
  StatusPill,
} from "../../shell/components";

/**
 * What "Run now" may not run without.
 *
 * Auditor A (HIGH), /backup -> Device ID + Run now, counted as "the eighth
 * occurrence": the control read `disabled={run.isPending || !deviceId}`, so a
 * click with the field empty moved nothing and named nothing -- the silent
 * no-op. The control is live and the click produces the reason.
 *
 * CYCLE 6 RETIRED THE SHAPE CHECK WITH THE FIELD. This rule used to carry a
 * `validate` that matched the value against a UUID pattern, because the field
 * was free text and `POST /backup/devices/{device_id}/run` declares
 * `device_id: UUID` in its path. The field is an `EntityPicker` now, whose
 * value can only ever be an id it was given -- typing into it filters the
 * list rather than setting the value -- so the malformed case cannot be
 * reached from this screen and a check for it would be untestable code
 * asserting a state the UI no longer has. Absence is still a live case, and
 * still answers through `requiredProblems`.
 */
const runRules = (): readonly FieldRule[] => [
  { name: "deviceId", label: translate("backup.runDialog.device") },
];

export function BackupPointsPage() {
  const t = useT();
  const api = useApi();
  /**
   * THE FILTER LIVES IN THE URL, because the tile above it is a link.
   * "Needs verification 3" is a promise that somewhere there is a list of
   * those three, and the only list of them in the product is the table on
   * this page. Held in `useState` the tile would have been an anchor to the
   * page the reader is already standing on, the dead tile the instruction
   * named. `verify=pending` is read here and written by the tile; the select
   * below is the same vocabulary, so an operator who arrives by link can see
   * why the table is short and clear it.
   */
  const [params, setParams] = useSearchParams();
  const verifyFilter = params.get("verify") ?? "";
  // The run form lives in a dialog now (see the page head), so the field and
  // its refusals belong to that dialog rather than to the page.
  const [runOpen, setRunOpen] = useState(false);
  // THE POINT, NOT ITS ID. The restore confirmation has to name the device and
  // the moment the point was taken (cycle 8: the subject is what lets somebody
  // notice they have the wrong row), and neither is recoverable from a UUID.
  const [restoreTarget, setRestoreTarget] = useState<BackupPoint | null>(null);

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("backup.read");
  const canRun = permissions.includes("backup.run");
  const canRestore = permissions.includes("backup.restore");

  // The zone the operator set on their profile (`/me`, cycle 2). `me` is
  // already fetched here for permissions, so nothing new is plumbed; a null
  // timezone falls the helper back to the browser zone.
  const zone = me.data?.timezone;

  const points = useBackupPoints();
  const report = useBackupReport();
  const devices = useDevices();
  const repositories = useBackupRepositories();

  const deviceOptions = devicePickerOptions(devices.data?.items ?? []);
  const repositoryOptions = repositoryPickerOptions(repositories.data ?? []);
  /** The platform of the device a point was taken from, for the row's lead glyph. */
  const deviceOs = (id: string): string =>
    devices.data?.items.find((device) => device.id === id)?.os_family ?? "";

  const items = points.data ?? [];
  /**
   * THE SERVER'S FORMULA, RE-EXPRESSED, not a near-enough guess.
   * `points_needing_verification` is `state == "complete" AND verified_at IS
   * NULL` (backend/app/modules/backup/reporting.py:180). The filter behind
   * the tile matches those two conditions exactly, so the rows the operator
   * lands on are the rows that were counted, modulo whatever the points list
   * has not paginated into view.
   */
  const needsVerificationRow = (point: BackupPoint) =>
    point.state === "complete" && point.verified_at == null;
  const visible = verifyFilter === "pending" ? items.filter(needsVerificationRow) : items;
  const failedRuns = report.data?.run_counts_by_state["failed"] ?? 0;
  const needsVerification = report.data?.points_needing_verification ?? 0;

  // Auditor A (MEDIUM): "'Backup success 0%' and 'Protected endpoints 0 / 1'
  // present an unprotected fleet as measured fact ... '0%' is a computed rate
  // over zero runs: mathematically undefined, and displayed identically to a
  // genuine 0% success rate, which would be a catastrophic condition."
  //
  // A rate over no runs is not a rate. "Never run" is the honest reading and
  // it is also the alarming one, which on THIS module is the point: an
  // operator glancing at these cards must not be reassured by a number that
  // means nothing yet.
  const totalRuns = Object.values(report.data?.run_counts_by_state ?? {}).reduce(
    (sum, count) => sum + count,
    0,
  );
  // The other half of the same finding: "surface '0 of 1 endpoints protected'
  // as an alert state rather than a neutral statistic". `gold` is the
  // attention treatment the Failed-jobs card beside it already uses.
  const unprotected = report.data ? report.data.devices_total - report.data.devices_covered : 0;

  return (
    <div>
      <PageHead
        title={t("backup.points.title")}
        // Auditor A (HIGH): "Production page copy tells the user to read the
        // page source to find out what works." The removed sentence pointed
        // an auditor at view-source; the caveat itself is true and stays
        // where it belongs: this file's docstring and the module spec.
        description={t("backup.points.description")}
        actions={
          canRun ? (
            /* ONE PRIMARY, AS EVERY OTHER HEAD IN THIS GROUP HAS. The run
               form used to sit in this slot, and it was the only page head
               with a labelled field in it: the picker's "Device" label put
               the control 21px below the h1's rhythm, and `.picker`'s 16px
               block margin dropped the submit 16px past the field it belongs
               to, so the crumb, the title and the action row read on three
               different lines of the page. The form itself is unchanged --
               same picker, same rules, same refusal, same queued sentence --
               it opens in a dialog, the way /deploy's New deployment and
               /provisioning's Provision do. */
            <Btn
              label={t("backup.points.runBackup")}
              icon="player-play"
              variant="primary"
              onClick={() => setRunOpen(true)}
            />
          ) : undefined
        }
      />

      <BackupPageNav canRead={canRead} />
      <BackupReadiness />

      {/* EVERY TILE OPENS THE ROWS IT COUNTED.
          COLOUR IS CATEGORY, and the categories here are the same three this
          module uses everywhere: green for what succeeded, red for what
          failed, gold for a gap somebody has to close. WEIGHT IS URGENCY, and
          only two of these have a deadline: an endpoint with no backup point
          at all, and a run that failed. The verification backlog is real work
          but it is not an incident, so it keeps the pale wash rather than
          shouting alongside them.

          "Backup success" and "Failed jobs" hand over to the job queue,
          narrowed to backup work by the search box `/jobs` already reads
          (`q` matches the job type and its label, so `backup` selects
          `backup.run`/`backup.restore` and nothing else). The counts come
          from `BackupRun` rows rather than from the job table, so the queue
          is where those runs were dispatched from: the nearest true list,
          not a claim that the two totals are identical. */}
      <StatsGrid columns={4}>
        <StatCard
          label={t("backup.points.tileSuccess")}
          value={
            report.data
              ? totalRuns === 0
                ? t("backup.points.neverRun")
                : t("backup.points.percent", { percent: Math.round(report.data.success_rate * 100) })
              : "—"
          }
          icon="circle-check"
          to="/jobs?q=backup"
          tone="green"
        />
        <StatCard
          label={t("backup.points.tileProtected")}
          value={report.data ? `${report.data.devices_covered} / ${report.data.devices_total}` : "—"}
          icon="shield-check"
          to="/devices"
          /* GOLD IS THE COVERAGE GAP, and it stays gold when the gap is
             closed: the hue says what the number is about, and `urgent` is
             what says it needs somebody today. Auditor A asked for "0 of 1
             endpoints protected" to read as an alert rather than a neutral
             statistic; that is this flag, not a colour that changes
             underneath the reader. */
          tone="gold"
          urgent={unprotected > 0}
        />
        <StatCard
          label={t("backup.points.tileFailed")}
          value={report.data ? String(failedRuns) : "—"}
          icon="circle-x"
          to="/jobs?status=failed&q=backup"
          tone="red"
          urgent={failedRuns > 0}
          /* "0 failed" over a hundred runs and "0 failed" because nothing has
             ever run are the same glyph and opposite facts. */
          zeroLabel={totalRuns === 0 ? t("backup.points.noRunEver") : t("backup.points.noRunFailed")}
        />
        <StatCard
          label={t("backup.points.tileNeedsVerification")}
          value={report.data ? String(needsVerification) : "—"}
          icon="clock-exclamation"
          to="/backup?verify=pending"
          tone="gold"
          zeroLabel={
            items.length === 0
              ? t("backup.points.noPointCaptured")
              : t("backup.points.allVerified")
          }
        />
      </StatsGrid>

      <Panel bare title={t("backup.points.panelTitle")} subtitle={t("backup.points.panelSubtitle")}>
        <FilterBar>
          <select
            className="select-field"
            value={verifyFilter}
            onChange={(event) => {
              const next = new URLSearchParams(params);
              // An empty value is ABSENT, not `?verify=`: a URL carrying an
              // empty filter reads as a filter set to nothing.
              if (event.target.value === "") next.delete("verify");
              else next.set("verify", event.target.value);
              setParams(next);
            }}
            aria-label={t("backup.points.filterAria")}
          >
            <option value="">{t("backup.points.allPoints")}</option>
            <option value="pending">{t("backup.points.awaitingVerification")}</option>
          </select>
          <span className="filter-spacer" />
        </FilterBar>
        <QueryState
          loading={points.isLoading}
          error={points.error}
          empty={visible.length === 0}
          emptyLabel={
            items.length === 0
              ? t("backup.points.emptyNone")
              : t("backup.points.emptyFiltered")
          }
          emptyIcon="backup"
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("backup.points.colDevice")}</Th>
                <Th>{t("backup.points.colRepository")}</Th>
                <Th>{t("backup.points.colTaken")}</Th>
                <Th>{t("backup.points.colSize")}</Th>
                <Th>{t("backup.points.colState")}</Th>
                {/* TWO DIFFERENT FACTS, NOT ONE COLUMN TWICE. "Verification"
                    and "Restore test" read as near-synonyms and were
                    routinely confused for each other; they cannot be merged
                    without losing information -- a point can have its data
                    checked for corruption (`verified_at`) without anyone
                    ever having restored from it (`restore_tested_at`), or
                    the reverse. Renamed to what each one actually checks,
                    with the difference spelled out on hover. */}
                <Th>
                  <span title={t("backup.points.colDataCheckedHint")}>
                    {t("backup.points.colDataChecked")}
                  </span>
                </Th>
                <Th>
                  <span title={t("backup.points.colRestoreTestedHint")}>
                    {t("backup.points.colRestoreTested")}
                  </span>
                </Th>
                <Th>{""}</Th>
              </tr>
            </thead>
            <tbody>
              {visible.map((point) => {
                const kind = backupPointStateKind(point.state);
                // A complete point nobody has verified is the row that needs
                // somebody, and the design draws it with the warning edge.
                const rowKind = needsVerificationRow(point) ? "warn" : kind;
                return (
                  <tr key={point.id}>
                    {/* Was the raw device and repository UUIDs. The console
                        knows both names (it is fetching both lists for the
                        picker above), and a table nobody can read a row out
                        of is the reading half of the same finding. The point
                        id stays, in mono under the name, because it is what
                        a restore job and an audit row name. */}
                    {/* Eight columns, so at 390 this scrolls and the first
                        column was the one that gave way: `lis-app-02` over two
                        lines and `bp-app02-0903` over three. The device name
                        and the point id are both single tokens that must read
                        as one line each. */}
                    <Td lead={<OsIcon os={deviceOs(point.device_id)} />} gutter={rowKind} clip={220}>
                      <div className="cell-title">{nameForOption(deviceOptions, point.device_id)}</div>
                      <div className="cell-sub mono">{point.id}</div>
                    </Td>
                    <Td>{nameForOption(repositoryOptions, point.repository_id)}</Td>
                    {/* CYCLE 5. Was `new Date(point.taken_at).toLocaleString()`:
                        the browser's zone and the browser's format, so this
                        column and the same point rendered on the device tab
                        disagreed in both. One helper, and the zone is the
                        operator's profile zone. */}
                    <Td>{formatDateTime(point.taken_at, zone)}</Td>
                    {/* Was the raw byte count: a 40 GB point rendered as
                        `42949672960`, which cannot be compared against the
                        row above it without counting digits. */}
                    <Td>
                      {point.size == null || point.size < 0 ? (
                        <NoValue label={t("backup.points.noSize")} />
                      ) : (
                        <span className="mono">{formatBytes(point.size)}</span>
                      )}
                    </Td>
                    <Td>
                      <StatusPill
                        value={point.state}
                        kind={kind}
                        label={backupPointStateLabel(point.state)}
                      />
                      {point.crash_consistent === false ? (
                        <div className="cell-sub">{notCrashConsistent()}</div>
                      ) : null}
                      {(point.warnings ?? []).length > 0 ? (
                        <ul className="cell-sub">
                          {(point.warnings ?? []).map((w) => (
                            <li key={w}>{warningText(w)}</li>
                          ))}
                        </ul>
                      ) : null}
                    </Td>
                    <Td>
                      <StatusPill
                        value={point.verified_at ? "verified" : "unverified"}
                        kind={point.verified_at ? "ok" : needsVerificationRow(point) ? "warn" : "mut"}
                        label={point.verified_at ? t("backup.points.verified") : t("backup.points.notVerified")}
                      />
                    </Td>
                    <Td>
                      <StatusPill
                        value={point.restore_tested_at ? "tested" : "untested"}
                        kind={point.restore_tested_at ? "ok" : "mut"}
                        label={restoreTestLabel(point.restore_tested_at, point.restore_test_scope)}
                      />
                    </Td>
                    <Td>
                      {/* A failed or pruned point has nothing a restore job
                          could read: `failed` never finished writing a
                          manifest, and `pruned` has aged out of the
                          repository's retention. The control stays visible
                          (recognition over recall for why the row cannot be
                          restored) but says why it cannot be pressed. */}
                      {canRestore && (
                        <Btn
                          label={t("backup.points.restore")}
                          icon="history"
                          small
                          disabled={point.state === "failed" || point.state === "pruned"}
                          title={
                            point.state === "failed"
                              ? t("backup.points.restoreFailedHint")
                              : point.state === "pruned"
                                ? t("backup.points.restorePrunedHint")
                                : undefined
                          }
                          onClick={() => setRestoreTarget(point)}
                        />
                      )}
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        </QueryState>
      </Panel>

      {/* KEYED, and this one prevented a restore against the wrong selection.
          The dialog is never unmounted (the parent toggles `pointId`) and
          its `selected` map is keyed by PATH with no reference to the point
          it came from. Tick two files in point A, close without restoring,
          open point B: `selectedPaths()` still returned A's paths, and
          Restore would have submitted them against B. Remounting per point
          makes the selection belong to the point on screen. */}
      <RestoreDialog
        key={restoreTarget?.id ?? "closed"}
        point={restoreTarget}
        deviceName={
          nameForOption(deviceOptions, restoreTarget?.device_id) ?? t("backup.points.thisDevice")
        }
        onClose={() => setRestoreTarget(null)}
      />

      {/* KEYED so a refusal, or a "Backup queued" from the last device,
          cannot be read as this one's: the dialog is never unmounted, only
          closed, and cycle 6's rule about a stale outcome sitting beside a
          fresh one applies to a dialog exactly as it applied to the page
          head this form used to live in. */}
      <RunBackupDialog
        /* The key is namespaced because `RestoreDialog` above is its sibling
           and keys "closed" twice in one parent is two children with the same
           key -- React warns, and one of them is a dialog. */
        key={runOpen ? "run-open" : "run-closed"}
        open={runOpen}
        onClose={() => setRunOpen(false)}
      />
    </div>
  );
}

/**
 * Running a backup now: the picker, the rule that names the field it needs,
 * and the sentence saying what was dispatched.
 *
 * WHAT MOVED AND WHAT DID NOT. Only the container. The picker is the same
 * `EntityPicker` over the same `listDevices` response, `RUN_RULES` is the
 * same rule, the submit is still live with the field empty (the silent no-op
 * is the finding this module was rebuilt around), and the queued sentence is
 * still `runQueuedMessage`, announced through `FormActions`' `role="status"`.
 * The dialog is NOT closed on success, because closing it would take the
 * sentence saying what happened with it -- the operator dismisses it having
 * read what was dispatched.
 */
function RunBackupDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useT();
  const devices = useDevices();
  const run = useRunBackupMutation();
  const [deviceId, setDeviceId] = useState("");
  // Held as the rules and the values they were checked against, so a language
  // switch re-words the refusal.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  const deviceOptions = devicePickerOptions(devices.data?.items ?? []);

  function submit() {
    // `reset()` first so the previous outcome cannot be read as this one: a
    // stale "Backup queued" sitting beside a fresh validation refusal is the
    // same lie about what just happened that the audit found on the failure
    // side, told in the other direction.
    run.reset();
    const next: Attempt = { rules: runRules, values: { deviceId } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    run.mutate(deviceId, { onSuccess: () => setDeviceId("") });
  }

  function dismiss() {
    setAttempt(null);
    run.reset();
    onClose();
  }

  return (
    <Dialog open={open} onClose={dismiss} title={t("backup.runDialog.title")}>
      <Form onSubmit={submit}>
        <div className="form-grid">
          {/* CYCLE 6, and this is Auditor A's HIGH: "A fifth hand-typed raw
              ID field: 'Device ID' is the only way to target a backup ... no
              picker, no search, no validation ... The stat card directly
              beside it reads 'Protected endpoints 0 / 1', so the system knows
              there is exactly one device, but will not offer it." It offers
              it now, by hostname, with `os_family · agent_status` beneath: a
              backup is a signed job on the endpoint, so an offline agent
              means a run that queues and does not start, and "no agent" means
              one that never can. A mistyped UUID is not reachable: the
              picker's value can only be an id it was given. */}
          <EntityPicker
            label={t("backup.runDialog.device")}
            options={deviceOptions}
            value={deviceId}
            onChange={setDeviceId}
            loading={devices.isLoading}
            emptyHint={devicePickerEmptyHint()}
            placeholder={t("backup.runDialog.searchDevices")}
          />
          {devices.isSuccess && devices.data.items.length === 0 ? (
            <p className="form-hint">
              <Link to={ADD_COMPUTER_TO}>{t("backup.runDialog.addComputer")}</Link>
            </p>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          {/* `type="button"`: `ui.tsx`'s Button sets no type, and an untyped
              button inside a form submits it -- Cancel would run a backup. */}
          <Button type="button" variant="ghost" onClick={dismiss}>
            {t("common.actions.cancel")}
          </Button>
          {/* Enabled with the field empty ON PURPOSE. "Run now" became "Run":
              `FormActions` builds its pending text from the label (`Run` ->
              `Running…`, which is exactly what this control said while
              running before), and "Run nowing…" is not a word. */}
          <FormActions
            label={t("backup.runDialog.run")}
            pending={run.isPending}
            error={run.error}
            saved={run.isSuccess}
            problems={attemptProblems(attempt)}
            savedMessage={runQueuedMessage(run.data)}
          />
        </div>
      </Form>
    </Dialog>
  );
}
