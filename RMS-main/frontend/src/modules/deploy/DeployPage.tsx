import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ConfirmDialog } from "../../components/confirm";
import { Form, FormActions, attemptProblems, describeSubmitError } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { EntityPicker } from "../../components/picker";
import { Button, Dialog, Field, inputClass, Table, Td, Th } from "../../components/ui";
import { WeekPainter } from "../../components/weekpainter";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { humaniseEnum } from "../../lib/labels";
import { useApi } from "../../lib/ApiContext";
import { Icon } from "../../shell/icons";
import { OsIcon, Person } from "../../shell/viz";
import {
  useAuthorizingChanges,
  useCreateDeploymentMutation,
  useDeployments,
  useDeviceGroups,
  useExecuteDeploymentMutation,
  useOrgUsers,
  usePackages,
  useTargetDevices,
} from "./api";
import { DeployPageNav } from "./deployNav";
import { DeployReadiness } from "./DeployReadiness";
import { ADD_COMPUTER_CTA, CREATE_GROUP_CTA } from "../../lib/detectors";
import { CRON_ZONE_NOTE, cronProblem, windowLabel } from "./cron";
import { deploymentStateKind } from "./deployState";
import {
  deploymentActionLabel,
  devicePickerEmptyHint,
  devicePickerOptions,
  groupPickerEmptyHint,
  groupPickerOptions,
  nameForOption,
  packagePickerEmptyHint,
  packagePickerOptions,
  requesterName,
} from "./pickerOptions";
import { initialsOf, personIndex } from "../../lib/people";
import type { Deployment, DeploymentAction, DeployTargetType } from "./types";
import { Btn, FilterBar, PageHead, Panel, QueryState, SearchField, StatCard, StatsGrid, StatusPill } from "../../shell/components";

// The design's `P['deploy']` (docs/design/itms-prototype-v3.html): the head
// with New deployment as the page's primary, a tile row, and one bare panel
// of rows led by the package glyph, each carrying its state pill. The
// design's rows also draw a progress bar and the person who requested the
// run. No per-device tally exists on `Deployment` and no endpoint counts one,
// so PROGRESS stays omitted rather than drawn as a zero bar; BY is drawn --
// `requested_by` is on every row, and the directory turns that id into the
// person (see `useOrgUsers`).

const ACTIONS: DeploymentAction[] = ["install", "update", "uninstall"];
const TARGET_TYPES: DeployTargetType[] = ["device", "group"];

const ONE_WEEK_MS = 7 * 24 * 60 * 60 * 1000;

/**
 * The deployment state vocabulary the backend actually writes
 * (`deployState.ts`: pending | dispatching | complete | failed), plus `week`.
 *
 * `week` IS NOT A STATE and is in the same control on purpose: it is the view
 * the "Deployed this week" tile links to, and one filter the reader can see
 * and clear beats a second select or, worse, a URL filter set by a tile with
 * no control anywhere that undoes it. `JobsPage`'s strip mixes `stuck` in with
 * the real statuses for the same reason.
 */
const STATE_FILTERS = ["week", "pending", "dispatching", "complete", "failed"] as const;

/** A filter option's label; the states are the deployment state labels, read at render. */
function filterLabel(value: (typeof STATE_FILTERS)[number]): string {
  return value === "week" ? translate("deploy.deployments.filterWeek") : humaniseEnum(value, "DeploymentState");
}

// Action and Target type are selects that always hold a value, and Window is
// optional by its own label, so the two fields a deployment cannot be created
// without are the only REQUIRED rules here. Labels are what the dialog prints
// beside the control -- "Target", never `target_id`.
//
// The window is optional and still checked -- `cronProblem` returns null for
// an empty value and a sentence for a malformed one, so it is added to the
// same `problems` list rather than through a second mechanism. Auditor A found
// this field unvalidated, and its own placeholder (`0 2 * * SAT`) is a value
// the server's matcher throws on -- see `cron.ts`.
//
// A function, so the labels are worded when the problems are shown (see `Attempt`).
// The optional window is a rule only when something was typed: a rule with a
// value to check and no "required".
const createRules = (windowTyped: boolean): (() => readonly FieldRule[]) => () => [
  { name: "packageId", label: translate("deploy.deployments.rulePackage") },
  { name: "targetId", label: translate("deploy.deployments.ruleTarget") },
  ...(windowTyped
    ? [
        {
          name: "windowCron",
          label: translate("deploy.deployments.create.windowRule"),
          validate: (value: string) => cronProblem(value),
        },
      ]
    : []),
];

/** The glyph a row leads with: the design's package for installs, the terminal for the rest. */
function actionGlyph(action: string): string {
  return action === "uninstall" ? "trash" : "package";
}

export function DeployPage() {
  const t = useT();
  const api = useApi();
  const [createOpen, setCreateOpen] = useState(false);

  /**
   * THE LIST FILTER LIVES IN THE URL, because the stat tiles link into it.
   *
   * "Failed 3" is an anchor to `/deploy?state=failed` now, and a filter held
   * in component state cannot be reached by a link: the tile would have been
   * a link to the page its reader is already on. `q` mirrors `DevicesPage`'s
   * search param; `state` carries the backend's own state words.
   */
  const [params, setParams] = useSearchParams();
  const search = params.get("q") ?? "";
  const stateFilter = params.get("state") ?? "";

  function update(patch: Record<string, string>, replace = false) {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(patch)) {
      // Absent, not `?q=`: an empty filter in the URL reads as a filter set to
      // nothing.
      if (value === "") next.delete(key);
      else next.set(key, value);
    }
    setParams(next, { replace });
  }

  const setSearch = (value: string) => update({ q: value }, true);
  const setStateFilter = (value: string) => update({ state: value });
  // The deployment awaiting confirmation, or null. Holding the ROW rather than
  // an id keeps the confirmation's subject readable even if a refetch drops it.
  const [pendingExecute, setPendingExecute] = useState<Deployment | null>(null);

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("deploy.read");
  const canCreate = permissions.includes("deploy.create");
  const canExecute = permissions.includes("deploy.execute");

  const deployments = useDeployments();
  const execute = useExecuteDeploymentMutation();

  // Fetched by the PAGE, not only by the create dialog: the table rendered
  // `package_id` and `target_id` as bare UUIDs, and the confirmation below has
  // to name what it is about to install and where. Both lists are cached by
  // key, so the dialog shares these responses rather than refetching.
  const packages = usePackages();
  const devices = useTargetDevices();
  const groups = useDeviceGroups();
  // Not memoised: the sublabels are worded, and must follow a language switch.
  const packageOptions = packagePickerOptions(packages.data ?? []);
  const deviceOptions = devicePickerOptions(devices.data ?? []);
  const groupOptions = groupPickerOptions(groups.data ?? []);

  // Who asked for each run. The directory carries its own permission, so this
  // is a CONVENIENCE on the row and not its subject: a 403 leaves the id in
  // mono rather than emptying the column.
  const users = useOrgUsers();
  const userNames = useMemo(
    () =>
      new Map((users.data?.users ?? []).map((user) => [user.id, user.display_name || user.email])),
    [users.data],
  );

  const allDeployments = deployments.data ?? [];
  // ONE INSTANT for the whole render, so "this week" means the same thing in
  // the tile and in the list that tile links to.
  const now = Date.now();
  const isThisWeek = (deployment: Deployment): boolean =>
    now - new Date(deployment.created_at).getTime() <= ONE_WEEK_MS;

  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return allDeployments.filter((deployment: Deployment) => {
      if (stateFilter === "week" && !isThisWeek(deployment)) return false;
      if (stateFilter && stateFilter !== "week" && deployment.state !== stateFilter) return false;
      if (needle === "") return true;
      return [
        deployment.id,
        deployment.package_id,
        deployment.action,
        deployment.target_id,
        deployment.state,
      ]
        .join(" ")
        .toLowerCase()
        .includes(needle);
    });
    // `now` is deliberately not a dependency: it changes every render, and the
    // filter only needs the instant this render already computed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allDeployments, search, stateFilter]);

  /** What a deployment installs, as the operator would say it. */
  const packageName = (deployment: Deployment): string =>
    nameForOption(packageOptions, deployment.package_id) ?? deployment.package_id;

  /** Where it goes: the device's or the group's name, the id only when it
   *  cannot be resolved from either list. */
  const targetName = (deployment: Deployment): string =>
    deployment.target_type === "device"
      ? (nameForOption(deviceOptions, deployment.target_id) ?? deployment.target_id)
      : (nameForOption(groupOptions, deployment.target_id) ?? deployment.target_id);

  /** The person who asked for a run, as v3 draws them. */
  const requesterCell = (id: string | null) => {
    const name = requesterName(userNames, id);
    if (!id || !name) return <span className="cell-sub">{t("shell.viz.na")}</span>;
    return userNames.has(id) ? (
      <Person initials={initialsOf(name)} index={personIndex(id)} name={name} />
    ) : (
      <span className="mono">{id}</span>
    );
  };

  /** The platform of a device target, for the row's `OsIcon`; null for a group or an unknown device. */
  const targetOs = (deployment: Deployment): string | null => {
    if (deployment.target_type !== "device") return null;
    return devices.data?.find((device) => device.id === deployment.target_id)?.os_family ?? null;
  };

  // The design's four stat cards read Published packages / Deployments this
  // week / Success rate / Awaiting approval. "Published packages" belongs on
  // the Packages screen (that is where the catalog lives), and "Awaiting
  // approval" has no meaning here -- AD-04 makes deployment an RBAC-gated,
  // operator-initiated action with no approval workflow (that is patch's
  // ring/approval gate, not deploy's). The three cards below are computed
  // from `deployments`, the same list this page renders.
  const stats = useMemo(() => {
    const thisWeek = allDeployments.filter((d) => isThisWeek(d)).length;
    const terminal = allDeployments.filter((d) => d.state === "complete" || d.state === "failed");
    const complete = terminal.filter((d) => d.state === "complete").length;
    const failed = terminal.filter((d) => d.state === "failed").length;
    // "Success rate —" beside "Deployments 0" was Auditor A's [LOW]: three
    // zeros and one dash for one underlying condition, sending the reader
    // hunting for a distinction that does not exist. The dash is the honest
    // half (a rate over zero completed runs is undefined, not 0%) so it
    // stays, and what was missing is the sentence saying so. A count that is
    // genuinely zero still prints 0: "we deployed nothing" is knowledge, and
    // dashing it out would be the false-unknown the patch page's four zeros
    // were the mirror image of.
    const rateUnknown = terminal.length === 0;
    const successRate = rateUnknown ? "—" : `${Math.round((complete / terminal.length) * 100)}%`;
    // EVERY TILE IN THE ROW CARRIES A SUB-LINE, which is what the design
    // draws and what stops four tiles of the same height ending on four
    // different rhythms -- two figures with a sentence under them and two
    // with 35px of white. Each sub-line says something the figure above it
    // does not: what is still moving, what "this week" counts, and what the
    // failures are a share of.
    const running = allDeployments.length - terminal.length;
    return {
      total: allDeployments.length,
      thisWeek,
      successRate,
      rateUnknown,
      complete,
      failed,
      running,
      finished: terminal.length,
    };
    // `isThisWeek` closes over this render's `now` and is deliberately not a
    // dependency: it is a new function every render, and this only needs the
    // instant of the render it last ran in, the same instant the list below
    // is filtered against.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allDeployments]);

  return (
    <div>
      <PageHead
        title={t("deploy.deployments.title")}
        description={t("deploy.deployments.description")}
        actions={
          canCreate ? (
            <Btn label={t("deploy.deployments.newDeployment")} icon="plus" variant="primary" onClick={() => setCreateOpen(true)} />
          ) : undefined
        }
      />

      {/* The group's other two pages (Packages, Scripts) are routed but the
          rail carries only this one entry, so this strip is the only thing in
          the console that links to them. */}
      <DeployPageNav canRead={canRead} />
      <DeployReadiness />

      {/* Execute DISPATCHES SIGNED JOBS TO A FLEET, and this was the only
          thing standing between the operator and a silent failure: for the
          bodyless 503 a proxy returns, `String(execute.error)` rendered
          "ApiError: " (a red line with no sentence in it) while the row it
          sits beside kept its old state, so nothing on screen said the
          dispatch had not happened. `describeSubmitError` names the status,
          and `role="alert"` makes it announced rather than merely present.
          Guarded on `execute.error` rather than `isError` so the alert
          element exists only when it has something to say. */}
      {execute.error ? (
        <p role="alert" className="form-error">
          {describeSubmitError(execute.error)}
        </p>
      ) : null}

      {/* Each tile lands on the deployment list it counts. Colour is category
          (inert total neutral, an informational count blue, healthy green,
          failure red) and only Failed is loud, because it is the only one of
          the four that needs somebody. */}
      <StatsGrid columns={4}>
        <StatCard
          label={t("deploy.deployments.tileDeployments")}
          value={String(stats.total)}
          icon="package"
          // Every deployment: from a filtered view, the way back.
          to="/deploy"
          tone="neutral"
          zeroLabel={t("deploy.deployments.tileDeploymentsZero")}
          foot={
            stats.total === 0
              ? undefined
              : stats.running > 0
                ? t("deploy.deployments.stillToRun", { count: stats.running })
                : t("deploy.deployments.allFinished")
          }
        />
        <StatCard
          label={t("deploy.deployments.tileWeek")}
          value={String(stats.thisWeek)}
          icon="calendar-event"
          to="/deploy?state=week"
          tone="blue"
          zeroLabel={t("deploy.deployments.tileWeekZero")}
          foot={stats.thisWeek === 0 ? undefined : t("deploy.deployments.tileWeekFoot")}
        />
        <StatCard
          label={t("deploy.deployments.tileRate")}
          value={stats.successRate}
          icon="circle-check"
          to="/deploy?state=complete"
          tone="green"
          // NO `zeroLabel`: this tile's "nothing measured" case is "—", not 0,
          // and `rateFoot` already says which: "No completed runs yet".
          foot={
            stats.rateUnknown
              ? t("deploy.deployments.rateNone")
              : t("deploy.deployments.rateOf", {
                  complete: stats.complete,
                  finished: stats.finished,
                  count: stats.complete,
                })
          }
        />
        <StatCard
          label={t("deploy.deployments.tileFailed")}
          value={String(stats.failed)}
          icon="circle-x"
          to="/deploy?state=failed"
          tone="red"
          urgent={stats.failed > 0}
          zeroLabel={t("deploy.deployments.tileFailedZero")}
          foot={stats.failed === 0 ? undefined : t("deploy.deployments.runsFinished", { count: stats.finished })}
        />
      </StatsGrid>

      <Panel bare>
        <FilterBar>
          <SearchField
            placeholder={t("deploy.deployments.searchPlaceholder")}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            aria-label={t("deploy.deployments.searchLabel")}
          />
          {/* The control the tiles link into, and the one that clears what
              they set. */}
          <select
            className="select-field"
            value={stateFilter}
            onChange={(event) => setStateFilter(event.target.value)}
            aria-label={t("deploy.deployments.filterLabel")}
          >
            <option value="">{t("deploy.deployments.filterAll")}</option>
            {STATE_FILTERS.map((option) => (
              <option key={option} value={option}>
                {filterLabel(option)}
              </option>
            ))}
          </select>
          <span className="filter-spacer" />
        </FilterBar>

        <QueryState
          loading={deployments.isLoading}
          error={deployments.error}
          empty={deployments.data != null && filtered.length === 0}
          emptyIcon="package"
          emptyLabel={
            allDeployments.length === 0 ? t("deploy.deployments.emptyNone") : t("deploy.deployments.emptyFiltered")
          }
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("deploy.deployments.colPackage")}</Th>
                <Th>{t("deploy.deployments.colAction")}</Th>
                <Th>{t("deploy.deployments.colTarget")}</Th>
                <Th>{t("deploy.deployments.colWindow")}</Th>
                <Th>{t("deploy.deployments.colState")}</Th>
                <Th>{t("deploy.deployments.colRequestedBy")}</Th>
                <Th>{""}</Th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((deployment) => {
                const os = targetOs(deployment);
                const kind = deploymentStateKind(deployment.state);
                return (
                  <tr key={deployment.id}>
                    {/* The package's NAME, resolved from the catalog this page
                        already holds, with the deployment id beneath it in
                        mono. A UUID as the title is what the operator was asked
                        to recognise before confirming an install. */}
                    <Td lead={<Icon name={actionGlyph(deployment.action)} size={18} />} gutter={kind}>
                      <div className="cell-title">{packageName(deployment)}</div>
                      <div className="cell-sub mono">{deployment.id}</div>
                    </Td>
                    {/* The verb as a person says it. The column read
                        "install" / "uninstall" -- the wire enum, lowercase,
                        beside five columns of prose. */}
                    <Td>{deploymentActionLabel(deployment.action)}</Td>
                    {/* A row that names a device shows its platform glyph; a
                        row that names a GROUP shows the group's own name,
                        resolved from the same list the picker below offers.
                        "group grp-build" as the whole cell was a database
                        value wearing a lowercase noun -- the id is now shown
                        only when the group cannot be found (`targetName`). */}
                    {deployment.target_type === "device" ? (
                      <Td lead={os ? <OsIcon os={os} /> : undefined}>{targetName(deployment)}</Td>
                    ) : (
                      <Td lead={<Icon name="users-group" size={18} />}>{targetName(deployment)}</Td>
                    )}
                    {/* THE WINDOW IS PAINTED, SO IT IS PRINTED THE WAY IT IS
                        PAINTED. `windowLabel` puts the same window into the
                        same words `WeekPainter` writes under its grid; an
                        expression the grid cannot draw has no sentence to
                        print and stays the expression it is. */}
                    <Td>
                      {deployment.window_cron ? (
                        (windowLabel(deployment.window_cron) ?? (
                          <span className="mono">{deployment.window_cron}</span>
                        ))
                      ) : (
                        <span className="cell-sub">{t("deploy.deployments.alwaysOpen")}</span>
                      )}
                    </Td>
                    <Td>
                      <StatusPill
                        value={deployment.state}
                        kind={kind}
                        label={humaniseEnum(deployment.state, "DeploymentState")}
                      />
                    </Td>
                    {/* THE DESIGN'S BY COLUMN. A person once the directory
                        names them, the id in mono when it does not (no
                        `users.read`, or an account since deleted), and `n/a`
                        for a deployment with no requester recorded -- never a
                        blank cell and never an invented name. */}
                    <Td>
                      {requesterCell(deployment.requested_by)}
                    </Td>
                    <Td>
                      {/* Execute dispatches a job. A deployment that has
                          already reached a terminal state has nothing left
                          for the click to do -- re-running is a new
                          deployment, not this one executed twice. */}
                      {canExecute && deployment.state !== "complete" && deployment.state !== "failed" && (
                        <Btn
                          label={t("deploy.deployments.execute")}
                          icon="player-play"
                          small
                          disabled={execute.isPending}
                          onClick={() => setPendingExecute(deployment)}
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

      {/* CYCLE 8. Execute signs a job per targeted device and hands it to the
          agent gateway; once an installer has run on an endpoint there is no
          "undo deployment" on any path in this product. The subject names the
          package AND the target, because the mistake this catches is the wrong
          ROW: the table's Execute buttons are identical and adjacent, and
          before this the first click was the dispatch.

          `requireTyping` is deliberately NOT set: a deployment is recoverable
          by deploying again (or an uninstall action), the target is one device
          or one group, and typing drills teach people to copy words without
          reading them. That control is for erase-the-machine. */}
      <ConfirmDialog
        open={pendingExecute != null}
        title={t("deploy.deployments.confirmTitle")}
        subject={
          pendingExecute
            ? t("deploy.deployments.confirmSubject", {
                action: deploymentActionLabel(pendingExecute.action),
                package: packageName(pendingExecute),
                target: targetName(pendingExecute),
              })
            : ""
        }
        body={
          pendingExecute?.window_cron
            ? t("deploy.deployments.confirmWindow", {
                window: windowLabel(pendingExecute.window_cron) ?? pendingExecute.window_cron,
                zone: CRON_ZONE_NOTE,
              })
            : t("deploy.deployments.confirmNow")
        }
        confirmLabel={t("deploy.deployments.execute")}
        destructive
        onCancel={() => setPendingExecute(null)}
        onConfirm={() => {
          if (pendingExecute) execute.mutate(pendingExecute.id);
          setPendingExecute(null);
        }}
      />

      <CreateDeploymentDialog open={createOpen} onClose={() => setCreateOpen(false)} />
    </div>
  );
}

function CreateDeploymentDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useT();
  const packages = usePackages();
  const devices = useTargetDevices();
  const groups = useDeviceGroups();
  const create = useCreateDeploymentMutation();
  const changes = useAuthorizingChanges(open);
  const [changeId, setChangeId] = useState("");
  const [packageId, setPackageId] = useState("");
  const [action, setAction] = useState<DeploymentAction>("install");
  const [targetType, setTargetType] = useState<DeployTargetType>("device");
  const [targetId, setTargetId] = useState("");
  const [windowCron, setWindowCron] = useState("");
  // Filled ON SUBMIT only. Complaining about a field the operator has not
  // reached yet is its own kind of unhelpful. Held as rules + values and worded
  // at render (see `Attempt`), so a language switch re-words it.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  // Not memoised: the sublabels are worded, and must follow a language switch.
  const packageOptions = packagePickerOptions(packages.data ?? []);
  const deviceOptions = devicePickerOptions(devices.data ?? []);
  const groupOptions = groupPickerOptions(groups.data ?? []);
  const catalogEmpty = packages.data != null && packages.data.length === 0;

  function submit() {
    // Read from the snapshot below, never from live state (see `Attempt`).
    const next: Attempt = {
      rules: createRules(windowCron.trim() !== ""),
      values: { packageId, targetId, windowCron },
    };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    create.mutate(
      {
        package_id: packageId,
        action,
        target_type: targetType,
        // Trimmed on the way out, so the value the server records is the
        // value the check above passed -- otherwise `" dev1"` clears
        // validation and is stored with the space.
        target_id: targetId.trim(),
        window_cron: windowCron.trim() || null,
        ...(changeId ? { change_id: changeId } : {}),
      },
      {
        onSuccess: () => {
          setChangeId("");
          setPackageId("");
          setTargetId("");
          setWindowCron("");
          setAttempt(null);
          onClose();
        },
      },
    );
  }

  // This dialog is never unmounted -- the page keeps it rendered and toggles
  // `open` -- so a refusal from the last attempt would otherwise still be
  // announced when the operator opens it again for a different deployment.
  function dismiss() {
    setAttempt(null);
    create.reset();
    onClose();
  }

  // Escape closes it and the page behind it stops scrolling. Both live in the
  // shared `Dialog` (components/ui.tsx). `dismiss`, not `onClose`, so leaving
  // by keyboard clears a stale refusal exactly as leaving by Cancel does.

  return (
    <Dialog open={open} onClose={dismiss} title={t("deploy.deployments.create.title")}>
      <Form onSubmit={submit}>
        <div className="form-grid">
          {/* THE BLOCKER. The Package control was a `<select>` holding exactly
              one option, "Select a package…", on a fresh install: a dead end
              with no explanation and no way out. `EntityPicker` renders
              `emptyHint` in place of the empty list, and the link below puts the
              screen that fills it one click away rather than leaving the
              operator to discover Packages on their own. */}
          <EntityPicker
            label={t("deploy.deployments.create.package")}
            options={packageOptions}
            loading={packages.isLoading}
            emptyHint={packagePickerEmptyHint()}
            placeholder={t("deploy.deployments.create.packagePlaceholder")}
            value={packageId}
            onChange={setPackageId}
          />
          {catalogEmpty ? (
            <p className="form-hint">
              <Link to="/deploy/packages">{t("deploy.deployments.create.addPackage")}</Link>
            </p>
          ) : null}
          <Field label={t("deploy.deployments.create.action")}>
            <select
              className={inputClass}
              value={action}
              onChange={(event) => setAction(event.target.value as DeploymentAction)}
            >
              {ACTIONS.map((option) => (
                <option key={option} value={option}>
                  {deploymentActionLabel(option)}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("deploy.deployments.create.targetType")}>
            <select
              className={inputClass}
              value={targetType}
              onChange={(event) => {
                // The chosen target does not survive the switch: a device id is
                // not a group id, and carrying it across would submit an id of
                // the wrong kind against a type the server trusts.
                setTargetId("");
                setTargetType(event.target.value as DeployTargetType);
              }}
            >
              {TARGET_TYPES.map((option) => (
                <option key={option} value={option}>
                  {t(option === "device" ? "deploy.targetType.device" : "deploy.targetType.group")}
                </option>
              ))}
            </select>
          </Field>
          {targetType === "device" ? (
            <EntityPicker
              label={t("deploy.deployments.create.target")}
              options={deviceOptions}
              loading={devices.isLoading}
              emptyHint={devicePickerEmptyHint()}
              placeholder={t("deploy.deployments.create.devicePlaceholder")}
              value={targetId}
              onChange={setTargetId}
            />
          ) : (
            <EntityPicker
              label={t("deploy.deployments.create.target")}
              options={groupOptions}
              loading={groups.isLoading}
              emptyHint={groupPickerEmptyHint()}
              placeholder={t("deploy.deployments.create.groupPlaceholder")}
              value={targetId}
              onChange={setTargetId}
            />
          )}
          {/* THE SCREEN THAT FILLS AN EMPTY TARGET LIST, one click away, as the
              catalog link above does for packages (cross-module-guidance §2
              Software #2, #3). Only on a list that loaded empty: a refused
              read is the hint's "you may not have permission", not a gap. */}
          {targetType === "device" && devices.isSuccess && devices.data.length === 0 ? (
            <p className="form-hint">
              <Link to={ADD_COMPUTER_CTA.to}>{ADD_COMPUTER_CTA.label}</Link>
            </p>
          ) : null}
          {targetType !== "device" && groups.isSuccess && groups.data.length === 0 ? (
            <p className="form-hint">
              <Link to={CREATE_GROUP_CTA.to}>{CREATE_GROUP_CTA.label}</Link>
            </p>
          ) : null}
          {/* THE WINDOW IS PAINTED, NOT TYPED. v3 draws the week as a grid and
              says "drag to paint"; `WeekPainter` is that grid, and it emits
              the five-field expression the server's matcher reads. The raw
              expression stays reachable under Advanced, and `cronProblem`
              still runs on submit whichever way the value arrived.

              NOT A `<label>`: the grid is twenty-four times seven buttons, and
              a label wrapping it would name only the first. The caption is the
              design's `.field > span`; the grid names itself. The zone is
              stated because the matcher runs on `datetime.now(UTC)`. */}
          <div className="field full">
            <span>{t("deploy.deployments.create.windowCaption", { zone: CRON_ZONE_NOTE })}</span>
            <WeekPainter
              label={t("deploy.deployments.create.windowLabel")}
              value={windowCron || null}
              onChange={(cron) => setWindowCron(cron ?? "")}
              emptyHint={t("deploy.deployments.create.windowEmpty")}
            />
          </div>
          {changes.data && changes.data.length > 0 ? (
            <Field
              label={t("deploy.deployments.create.changeRequest")}
              help={t("deploy.deployments.create.changeHelp")}
            >
              <select
                className={inputClass}
                value={changeId}
                onChange={(event) => setChangeId(event.target.value)}
              >
                <option value="">{t("deploy.deployments.create.noChange")}</option>
                {changes.data.map((change) => (
                  <option key={change.id} value={change.id}>
                    {change.title}
                  </option>
                ))}
              </select>
            </Field>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          {/* `type="button"` is load-bearing now these controls sit in a real
              `<form>`: `ui.tsx`'s Button sets no type, and an untyped button
              inside a form submits it -- Cancel would create the deployment. */}
          <Button type="button" variant="ghost" onClick={dismiss}>
            {t("common.actions.cancel")}
          </Button>
          {/* Enabled with no package and no target ON PURPOSE. Audit [MEDIUM],
              /deploy → New deployment: the disabled button was the silent
              no-op -- the click moved nothing and named no field. Now the
              click produces the reason, and `String(create.error)` is gone
              with it (an ApiError with an empty body stringified to nothing). */}
          <FormActions
            label={t("deploy.deployments.create.submit")}
            pending={create.isPending}
            error={create.error}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
}
