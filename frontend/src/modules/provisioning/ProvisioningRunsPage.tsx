/**
 * Provisioning runs — migrated onto the design per the UI port spec §6,
 * following DevicesPage.tsx as the template.
 *
 * WHAT THE DESIGN ASKS FOR THAT THE API DOES NOT SERVE. The prototype's
 * Provisioning overview shows four stat cards: Builds this month, Success
 * rate, Median build time, Ready site servers.
 *
 * - "Success rate" -> `GET /provisioning/report` (FleetProvisioningReport)
 *   serves `success_rate` directly (a fraction; multiplied by 100 only here).
 * - "Builds this month" is OMITTED and replaced with "Machines provisioned"
 *   (`report.machines_provisioned`) — the report has no time-windowed count,
 *   and inventing a "this month" scope client-side from run timestamps would
 *   silently exclude whatever the API does not paginate into view.
 * - "Median build time" is computed CLIENT-SIDE from the runs actually
 *   fetched (`finished_at - started_at` over runs that have both), since no
 *   endpoint aggregates duration. This is a real computation over real data,
 *   not a fabricated figure — but it describes only the runs this page holds,
 *   not the whole fleet's history.
 * - "Ready site servers" is OMITTED. That is a site-servers-module concept
 *   (PXE readiness), and site-servers here only implement the `wake` role —
 *   there is no PXE-readiness field to report, honestly or otherwise.
 */

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ConfirmDialog } from "../../components/confirm";
import { NoValue } from "../../components/emptystate";
import { Form, FormActions, attemptProblems } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { EntityPicker } from "../../components/picker";
import { Button, Dialog, Field, inputClass, Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { humaniseEnum } from "../../lib/labels";
import { useApi } from "../../lib/ApiContext";
import { NO_DATE, formatDateTime } from "../../lib/datetime";
import { Icon } from "../../shell/icons";
import { OsIcon } from "../../shell/viz";
import {
  useOsImages,
  useProvisionProfiles,
  useProvisionRuns,
  useProvisioningReport,
  useStartProvisionRunMutation,
} from "./api";
import { nameForOption, profilePickerEmptyHint, profilePickerOptions } from "./pickerOptions";
import { provisionRunStateKind, provisionRunStateLabel } from "./provisioningState";
import { ProvisioningPageNav } from "./provisioningNav";
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
import { ProvisioningPrerequisites } from "./ProvisioningPrerequisites";
import type { ProvisionRunState } from "./types";

// The design's `P['provisioning']`: the head with New run as the primary,
// three tiles, and one bare panel of rows led by the machine's platform
// glyph, the profile under the target, a stage pill. The design's rows also
// draw a progress bar and a time remaining; `ProvisionRun` carries neither,
// so both are OMITTED rather than faked.

function medianDurationLabel(
  runs: ReadonlyArray<{ started_at: string | null; finished_at: string | null }>,
): string {
  const durations = runs
    .filter((run) => run.started_at && run.finished_at)
    .map((run) => new Date(run.finished_at as string).getTime() - new Date(run.started_at as string).getTime())
    .filter((ms) => ms >= 0)
    .sort((a, b) => a - b);
  if (durations.length === 0) return NO_DATE;
  const mid = Math.floor(durations.length / 2);
  const lower = durations[mid - 1];
  const upper = durations[mid];
  if (upper === undefined) return NO_DATE;
  const medianMs = durations.length % 2 === 0 && lower !== undefined ? (lower + upper) / 2 : upper;
  return translate("provisioning.runs.minutes", { minutes: Math.round(medianMs / 60_000) });
}

/**
 * The run states this page can filter to, sourced from the backend's own
 * vocabulary (`reporting.py` RUN_STATES, cited in `provisioningState.ts`) —
 * a made-up state here would produce a tile linking to a filter that matches
 * nothing, which is a dead tile wearing a live one's clothes.
 */
const RUN_STATE_FILTERS: ReadonlyArray<ProvisionRunState> = [
  "pending",
  "awaiting_enrollment",
  "enrolled",
  "complete",
  "failed",
];

export function ProvisioningRunsPage() {
  const t = useT();
  const api = useApi();
  const [provisionOpen, setProvisionOpen] = useState(false);
  // The filter lives in the URL because the tiles above link into it: "Failed
  // runs 2" opens those two rows. Held in component state the tile would be
  // an anchor to the page the reader is already standing on.
  const [params, setParams] = useSearchParams();
  const stateFilter = params.get("state") ?? "";

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("provisioning.read");
  const canRun = permissions.includes("provisioning.run");

  // The page already holds `me` for its permission gates, so the profile
  // timezone is right here — no extra request, no prop drilled through the
  // table (cycle 5; see lib/datetime.ts for why the profile beats the
  // browser).
  const zone = me.data?.timezone ?? null;

  const runs = useProvisionRuns();
  const report = useProvisioningReport();
  const profiles = useProvisionProfiles();
  const images = useOsImages();
  const items = runs.data ?? [];
  const visible = stateFilter ? items.filter((run) => run.state === stateFilter) : items;
  const failedRuns = report.data?.run_counts_by_state["failed"] ?? 0;

  // Auditor A, MEDIUM, filed against /backup's identical cards and just as
  // true of these: "'0%' is a computed rate over zero runs — mathematically
  // undefined, and displayed identically to a genuine 0% success rate, which
  // would be a catastrophic condition." A fleet that has never provisioned
  // anything and a fleet whose every build failed are not the same fact.
  const totalRuns = Object.values(report.data?.run_counts_by_state ?? {}).reduce(
    (sum, count) => sum + count,
    0,
  );

  // Otherwise the table names each run's profile by UUID, the same raw-id
  // defect the pickers close on the writing side, seen on the reading side.
  const profileOptions = profilePickerOptions(profiles.data ?? [], images.data ?? []);
  /** The platform a profile's image lays down, for the row's lead glyph; null until both lists are here. */
  const imageOsFor = (profileId: string): string | null => {
    const profile = profiles.data?.find((item) => item.id === profileId);
    return images.data?.find((image) => image.id === profile?.image_id)?.os_family ?? null;
  };

  return (
    <div>
      <PageHead
        title={t("provisioning.runs.title")}
        description={t("provisioning.runs.description")}
        actions={
          canRun ? (
            <Btn label={t("provisioning.runs.provision")} icon="plus" variant="primary" onClick={() => setProvisionOpen(true)} />
          ) : undefined
        }
      />

      <ProvisioningPageNav canRead={canRead} />

      {canRead ? <ProvisioningPrerequisites /> : null}

      {/* EVERY TILE OPENS THE ROWS IT COUNTED, and the state names in those
          links are the backend's own (`RUN_STATE_FILTERS` above), so a filter
          that matches nothing cannot be shipped by a typo.

          Colour is the category — green for builds that finished, red for
          builds that failed, blue for the fleet they produced, neutral for an
          inert measurement — and only the failures carry weight: a failed
          build is a machine somebody is waiting for. "Machines provisioned"
          leaves this module entirely, because the machines themselves live in
          the device list, not in the run history. */}
      <StatsGrid columns={4}>
        <StatCard
          label={t("provisioning.runs.successRate")}
          value={
            report.data
              ? totalRuns === 0
                ? t("provisioning.runs.neverRun")
                : t("provisioning.runs.percent", { percent: Math.round(report.data.success_rate * 100) })
              : "—"
          }
          icon="circle-check"
          to="/provisioning?state=complete"
          tone="green"
        />
        <StatCard
          label={t("provisioning.runs.machinesProvisioned")}
          value={report.data ? String(report.data.machines_provisioned) : "—"}
          icon="devices"
          to="/devices"
          tone="blue"
          zeroLabel={t("provisioning.runs.noMachineProvisioned")}
        />
        <StatCard
          label={t("provisioning.runs.failedRuns")}
          value={report.data ? String(failedRuns) : "—"}
          icon="circle-x"
          to="/provisioning?state=failed"
          tone="red"
          urgent={failedRuns > 0}
          /* "0 failed" over fifty builds and "0 failed" because nothing has
             ever been built are the same glyph and opposite facts. */
          zeroLabel={totalRuns === 0 ? t("provisioning.runs.noBuildEverRan") : t("provisioning.runs.noBuildFailed")}
        />
        {/* Computed from the runs this page holds (see `medianDurationLabel`),
            so the list it opens is exactly the set it was measured over: no
            filter, because a median spans the failed builds too. */}
        <StatCard
          label={t("provisioning.runs.medianBuildTime")}
          value={medianDurationLabel(items)}
          icon="clock"
          to="/provisioning"
          tone="neutral"
        />
      </StatsGrid>

      <Panel bare>
        <FilterBar>
          <select
            className="select-field"
            value={stateFilter}
            onChange={(event) => {
              const next = new URLSearchParams(params);
              // An empty value is ABSENT, not `?state=`.
              if (event.target.value === "") next.delete("state");
              else next.set("state", event.target.value);
              setParams(next);
            }}
            aria-label={t("provisioning.runs.filterAria")}
          >
            <option value="">{t("provisioning.runs.allRuns")}</option>
            {RUN_STATE_FILTERS.map((value) => (
              <option key={value} value={value}>
                {humaniseEnum(value, "ProvisionRunState")}
              </option>
            ))}
          </select>
          <span className="filter-spacer" />
        </FilterBar>
        <QueryState
          loading={runs.isLoading}
          error={runs.error}
          empty={visible.length === 0}
          emptyLabel={
            items.length === 0 ? t("provisioning.runs.emptyNone") : t("provisioning.runs.emptyFiltered")
          }
          emptyIcon="image"
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("provisioning.runs.colTarget")}</Th>
                <Th>{t("provisioning.runs.colProfile")}</Th>
                <Th>{t("provisioning.runs.colState")}</Th>
                <Th>{t("provisioning.runs.colStarted")}</Th>
                <Th>{t("provisioning.runs.colFinished")}</Th>
                {/* THE COLUMN THAT MADE THE ROW 190px TALL. An error message is
                    a paragraph, and with no measure of its own it was allotted
                    whatever the five columns before it left over -- roughly 90px
                    at 390 -- so it wrapped to eight or nine lines and stood the
                    row up around it. The visible part of the row was the MAC
                    address and a Failed pill floating in a mostly empty band,
                    because the text that justified the height was off-screen.
                    A floor, NOT a clip: the whole point of this column is the
                    sentence in it, so it keeps wrapping and simply gets a
                    reasonable measure to wrap within. */}
                <Th width={280}>{t("provisioning.runs.colError")}</Th>
              </tr>
            </thead>
            <tbody>
              {visible.map((run) => {
                const kind = provisionRunStateKind(run.state);
                const os = imageOsFor(run.profile_id);
                return (
                  <tr key={run.id}>
                    {/* The machine the build is for, led by the platform the
                        profile's image lays down (a row that names a device
                        shows its platform glyph), with the run id in mono
                        beneath: the id is what the job chain and the audit
                        rows name. */}
                    <Td
                      lead={os ? <OsIcon os={os} /> : <Icon name="device-laptop" size={18} />}
                      gutter={kind}
                      clip={220}
                    >
                      <div className="cell-title">
                        {run.target_ref ?? run.device_id ?? <NoValue label={t("provisioning.runs.noTarget")} />}
                      </div>
                      <div className="cell-sub mono">{run.id}</div>
                    </Td>
                    <Td>{nameForOption(profileOptions, run.profile_id) ?? NO_DATE}</Td>
                    <Td>
                      <StatusPill value={run.state} kind={kind} label={provisionRunStateLabel(run.state)} />
                    </Td>
                    {/* One format, one zone, and NO_DATE for a run that has
                        not started or not finished: the guards are inside
                        `formatDateTime`, so there is no second place for
                        "Invalid Date" to leak from. */}
                    <Td>{formatDateTime(run.started_at, zone)}</Td>
                    <Td>{formatDateTime(run.finished_at, zone)}</Td>
                    <Td>
                      {run.error ? (
                        <span className="note" style={{ color: "var(--sev-crit)" }}>
                          {run.error}
                        </span>
                      ) : null}
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        </QueryState>
      </Panel>

      <ProvisionDialog open={provisionOpen} onClose={() => setProvisionOpen(false)} />
    </div>
  );
}

// Auditor A (MEDIUM): "'Provision a device' is the same shared modal — fourth
// confirmation": `form: false`, no `required` on either control, and a Start
// that sat disabled until a profile was picked without ever saying that was
// what it wanted. `Profile` is the only required field — `target_ref` is
// genuinely optional (a run may be started before the machine's MAC is known
// and matched at enrollment), so it is not a rule.
const provisionRules = (): readonly FieldRule[] => [
  { name: "profileId", label: translate("provisioning.provisionDialog.profile") },
];

function ProvisionDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useT();
  const profiles = useProvisionProfiles();
  const images = useOsImages();
  const start = useStartProvisionRunMutation();
  const [profileId, setProfileId] = useState("");
  const [targetRef, setTargetRef] = useState("");
  // Populated ON SUBMIT, never while typing. Held as the rules and the values
  // they were checked against, so a language switch re-words the sentences.
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  // The submit does not start the build. It asks — see `confirmBody`.
  const [confirming, setConfirming] = useState(false);

  // This dialog is never unmounted — the page keeps it rendered and toggles
  // `open` — so without this a failure from the previous attempt is still on
  // screen when it reopens, claiming this run failed before it was started.
  useEffect(() => {
    start.reset();
    setAttempt(null);
    setConfirming(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed on the
    // dialog opening; `start` is a fresh object every render.
  }, [open]);

  function reset() {
    setProfileId("");
    setTargetRef("");
    setAttempt(null);
  }

  function requestStart() {
    const next: Attempt = { rules: provisionRules, values: { profileId, targetRef } };
    setAttempt(next);
    // The old `if (!profileId) return` was the silent no-op in miniature: the
    // one line that knew what was wrong was also the one that said nothing.
    if (attemptProblems(next).length > 0) return;
    // Cycle 8: what happens next is a reimage. Ask first.
    setConfirming(true);
  }

  function startRun() {
    start.mutate(
      { profileId, body: { target_ref: targetRef.trim() || null } },
      {
        onSuccess: () => {
          // Closing is safe HERE and only here: `useStartProvisionRunMutation`
          // invalidates `provisioning-runs`, so the started run appears in the
          // table behind this dialog. The confirmation is the row, not a
          // message on a component that is about to disappear.
          reset();
          onClose();
        },
      },
    );
  }

  // Auditor A's BLOCKER on this same dialog — "Provisioning cannot be started
  // — the Profile dropdown is empty" — is the other half of `disabledReason`'s
  // job: not "you got the input wrong" but "there is nothing here to pick".
  // Gated on a settled query so a slow profiles fetch never reads as an empty
  // one.
  const noProfiles = profiles.isSuccess && (profiles.data?.length ?? 0) === 0;

  const options = profilePickerOptions(profiles.data ?? [], images.data ?? []);
  const chosen = options.find((option) => option.id === profileId) ?? null;

  // WHAT THE OPERATOR IS ABOUT TO DO, in the words that matter. A provisioning
  // run lays an OS image down on an endpoint: whatever was on that disk is
  // gone afterwards. And there is no way back through this console — the
  // provisioning API exposes runs as GET only (backend/app/modules/
  // provisioning/api.py has no cancel route), so the last moment anybody can
  // stop this is before the click.
  const confirmBody = [
    t("provisioning.provisionDialog.bodyLead", {
      profile: chosen?.sublabel ?? t("provisioning.provisionDialog.bodyProfileFallback"),
      target: targetRef.trim()
        ? t("provisioning.provisionDialog.bodyTargetAt", { ref: targetRef.trim() })
        : t("provisioning.provisionDialog.bodyTargetClaims"),
    }),
    t("provisioning.provisionDialog.bodyWipe"),
    t("provisioning.provisionDialog.bodyNoCancel"),
  ].join(" ");

  return (
    <Dialog open={open} onClose={onClose} title={t("provisioning.provisionDialog.title")}>
      <Form onSubmit={requestStart}>
        <div className="form-grid">
          {/* CYCLES 6 AND 7, and this is Auditor A's BLOCKER for the module.
              The `<select>` here offered exactly one option on a fresh install
              — its own "Select a profile…" placeholder — so the module's
              primary action could not be completed and nothing said why.
              `EntityPicker` renders `emptyHint` IN PLACE OF the empty list, so
              the dead end names its own cause; the link below is the other half
              of the audit's fix ("there is no link to the 'Profiles' tab that
              sits just above"). The sublabel carries the image the profile lays
              down and whether it is enabled — see pickerOptions.ts. */}
          <EntityPicker
            label={t("provisioning.provisionDialog.profile")}
            options={options}
            value={profileId}
            onChange={setProfileId}
            loading={profiles.isLoading}
            emptyHint={profilePickerEmptyHint()}
            placeholder={t("provisioning.provisionDialog.searchProfiles")}
          />
          {noProfiles ? (
            <p className="form-hint">
              <Link to="/provisioning/profiles">{t("provisioning.provisionDialog.createProfile")}</Link>
            </p>
          ) : null}
          <Field label={t("provisioning.provisionDialog.targetRef")} full>
            <input
              className={inputClass}
              value={targetRef}
              onChange={(e) => setTargetRef(e.target.value)}
            />
          </Field>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          {/* `type="button"` is load-bearing now that these controls live in a
              real `<form>`: `ui.tsx`'s Button sets no type, and an untyped
              button inside a form submits it — Cancel would start the run. */}
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("common.actions.cancel")}
          </Button>
          {/* Enabled with no profile selected ON PURPOSE: the click now names
              the field instead of moving nothing. */}
          <FormActions
            label={t("provisioning.provisionDialog.start")}
            pending={start.isPending}
            error={start.error}
            problems={attemptProblems(attempt)}
            disabledReason={noProfiles ? t("provisioning.provisionDialog.noProfilesYet") : undefined}
          />
        </div>
      </Form>

      {/* CYCLE 8. The subject is the profile, because the profile decides what
          the machine becomes and picking the wrong one is the mistake this
          exists to catch. `destructive` and NOT `requireTyping`: a reimage is
          unrecoverable but narrow — one endpoint — and asking for a typed name
          on every build is the kind of friction people learn to copy without
          reading, which is worse than the confirmation it replaced. */}
      <ConfirmDialog
        open={confirming}
        title={t("provisioning.provisionDialog.confirmTitle")}
        subject={chosen?.label ?? t("provisioning.provisionDialog.confirmSubjectFallback")}
        body={confirmBody}
        confirmLabel={t("provisioning.provisionDialog.confirmLabel")}
        destructive
        onCancel={() => setConfirming(false)}
        onConfirm={() => {
          setConfirming(false);
          startRun();
        }}
      />
    </Dialog>
  );
}
