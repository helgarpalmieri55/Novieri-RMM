/**
 * Backup policies, on the v3 kit. The design has no policies page of its
 * own (its Backup screen folds them into a tab of named cards with a
 * retention window and a target count); `GET /backup/policies`
 * (BackupPolicy) returns a repository id, an optional device-group id, a
 * cron window and a retention_gfs blob: no policy name and no target count.
 * So this is the design's bare list, led by the calendar glyph, listing
 * what the API actually returns rather than inventing names or counts.
 *
 * THE WINDOW IS PAINTED, NOT TYPED. The editor's cron box asked an operator
 * to write `0 2 * * *` from memory; v3 draws the week as a grid and says
 * "drag to paint", and `WeekPainter` is that grid. The raw expression stays
 * reachable under Advanced, and a stored value the grid cannot draw opens
 * there.
 *
 * A POLICY IS ONE CLIENT'S, AND SO IS ITS REPOSITORY (MSP clients phase 2,
 * Task 13: a repository's key travels to every device that backs up into it).
 * A group's policy is its group's client's; one with no group is a client's
 * default. The repository picker lists only that client's repositories
 * (`policyClient`, `repositoriesOfClient`), and the server refuses any other.
 */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Button, Dialog, Table, Td, Th } from "../../components/ui";
import { Form, FormActions, attemptProblems } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { EntityPicker } from "../../components/picker";
import { WeekPainter } from "../../components/weekpainter";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { internalClientId, requireKnown } from "../../lib/internalClient";
import { Icon } from "../../shell/icons";
import {
  useBackupPolicies,
  useBackupRepositories,
  useDeviceGroups,
  useUpsertBackupPolicyMutation,
} from "./api";
import {
  groupPickerOptions,
  nameForOption,
  policyClient,
  repositoriesOfClient,
  repositoryPickerEmptyHint,
  repositoryPickerEmptyHintElsewhere,
  repositoryPickerOptions,
} from "./pickerOptions";
import type { BackupPolicy } from "./types";
import { windowLabel } from "./windowLabel";
import { BackupPageNav } from "./backupNav";
import { PolicyReadiness } from "./BackupReadiness";
import {
  Btn,
  FilterBar,
  PageHead,
  Panel,
  QueryState,
  StatsGrid,
  StatCard,
  StatusPill,
} from "../../shell/components";

export function BackupPolicyEditor() {
  const t = useT();
  const api = useApi();
  const [editing, setEditing] = useState<BackupPolicy | "new" | null>(null);
  // In the URL, so the "Enabled" tile above can hand over the rows it counted
  // rather than linking to the page the reader is already on.
  const [params, setParams] = useSearchParams();
  const enabledFilter = params.get("enabled") ?? "";

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("backup.read");
  const canManage = permissions.includes("backup.policy.manage");

  const policies = useBackupPolicies();
  const repositories = useBackupRepositories();
  const groups = useDeviceGroups();
  const items = policies.data ?? [];
  const enabledCount = items.filter((policy) => policy.enabled).length;
  // What the totals are made of, for the tiles' second lines: how many
  // distinct repositories these policies write into, and how many of the
  // enabled ones are held back by a window rather than always open.
  const repositoriesInUse = new Set(items.map((policy) => policy.repository_id)).size;
  const windowed = items.filter((policy) => policy.enabled && policy.window_cron).length;
  const visible =
    enabledFilter === "true"
      ? items.filter((policy) => policy.enabled)
      : enabledFilter === "false"
        ? items.filter((policy) => !policy.enabled)
        : items;
  // A policy's whole purpose is "which repository", and the table named it by
  // UUID: unreadable in exactly the column an operator scans.
  const repositoryOptions = repositoryPickerOptions(repositories.data ?? []);
  const groupOptions = groupPickerOptions(groups.data ?? []);

  return (
    <div>
      <PageHead
        title={t("backup.policies.title")}
        description={t("backup.policies.description")}
        actions={
          canManage ? (
            <Btn label={t("backup.policies.newPolicy")} icon="plus" variant="primary" onClick={() => setEditing("new")} />
          ) : undefined
        }
      />

      <BackupPageNav canRead={canRead} />
      <PolicyReadiness />

      {/* A policy that exists and is disabled schedules nothing, so "Enabled"
          is the number that says whether anything is actually being backed
          up: green, and its zero says which zero it is. The total is the
          route back to the unfiltered list, and "Disabled" is the one the
          reader has to act on -- it is what is NOT being backed up.

          THREE TILES, NOT TWO (review wave, round 2, harmony): two tiles
          stretched to about 570px each and left a 26px figure floating in
          half a card, looser than every neighbouring page. Each tile lands on
          a slice of the list the filter below it can also reach, so the third
          is a real view and not a spacer. */}
      <StatsGrid columns={3}>
        <StatCard
          label={t("backup.policies.tilePolicies")}
          value={String(items.length)}
          icon="calendar-week"
          to="/backup/policies"
          tone="neutral"
          zeroLabel={t("backup.policies.noPolicy")}
          foot={
            items.length === 0
              ? undefined
              : t("backup.policies.writingInto", { count: repositoriesInUse })
          }
        />
        <StatCard
          label={t("backup.policies.tileEnabled")}
          value={String(enabledCount)}
          icon="circle-check"
          to="/backup/policies?enabled=true"
          tone="green"
          zeroLabel={
            items.length === 0
              ? t("backup.policies.noPolicyToEnable")
              : t("backup.policies.everyDisabled")
          }
          foot={
            enabledCount === 0
              ? undefined
              : windowed === 0
                ? t("backup.policies.noneWindowed")
                : t("backup.policies.windowed", { count: windowed })
          }
        />
        <StatCard
          label={t("backup.policies.tileDisabled")}
          value={String(items.length - enabledCount)}
          icon="minus"
          to="/backup/policies?enabled=false"
          tone="neutral"
          zeroLabel={
            items.length === 0 ? t("backup.policies.noPolicyToDisable") : t("backup.policies.everyEnabled")
          }
          foot={
            items.length - enabledCount === 0
              ? undefined
              : t("backup.policies.schedulingNothing")
          }
        />
      </StatsGrid>

      <Panel bare>
        <FilterBar>
          <select
            className="select-field"
            value={enabledFilter}
            onChange={(event) => {
              const next = new URLSearchParams(params);
              if (event.target.value === "") next.delete("enabled");
              else next.set("enabled", event.target.value);
              setParams(next);
            }}
            aria-label={t("backup.policies.filterAria")}
          >
            <option value="">{t("backup.policies.allPolicies")}</option>
            <option value="true">{t("backup.policies.tileEnabled")}</option>
            <option value="false">{t("backup.policies.tileDisabled")}</option>
          </select>
          <span className="filter-spacer" />
        </FilterBar>
        <QueryState
          loading={policies.isLoading}
          error={policies.error}
          empty={visible.length === 0}
          emptyLabel={
            items.length === 0
              ? t("backup.policies.emptyNone")
              : t("backup.policies.emptyFiltered")
          }
          emptyIcon="calendar-week"
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("backup.policies.colScope")}</Th>
                <Th>{t("backup.policies.colRepository")}</Th>
                <Th>{t("backup.policies.colWindow")}</Th>
                <Th>{t("backup.policies.colState")}</Th>
                <Th>{""}</Th>
              </tr>
            </thead>
            <tbody>
              {visible.map((policy) => (
                <tr key={policy.id}>
                  <Td lead={<Icon name="calendar-week" size={18} />}>
                    <div className="cell-title">
                      {policy.device_group_id
                        ? (nameForOption(groupOptions, policy.device_group_id) ?? policy.device_group_id)
                        : t("backup.policies.clientDefault")}
                    </div>
                    {policy.device_group_id ? null : (
                      <div className="cell-sub">{t("backup.policies.clientDefaultHint")}</div>
                    )}
                  </Td>
                  <Td>{nameForOption(repositoryOptions, policy.repository_id)}</Td>
                  {/* THE WINDOW IS PAINTED, SO IT IS PRINTED THE WAY IT IS
                      PAINTED. `windowLabel` puts the stored expression into
                      the same words the grid below writes under itself; an
                      expression the grid cannot draw has no sentence to print
                      and stays the expression it is. */}
                  <Td>
                    {policy.window_cron ? (
                      (windowLabel(policy.window_cron) ?? (
                        <span className="mono">{policy.window_cron}</span>
                      ))
                    ) : (
                      <span className="cell-sub">{t("backup.policies.alwaysOpen")}</span>
                    )}
                  </Td>
                  <Td>
                    <StatusPill
                      value={policy.enabled ? "enabled" : "disabled"}
                      kind={policy.enabled ? "ok" : "mut"}
                    />
                  </Td>
                  <Td>
                    {canManage && <Btn label={t("backup.policies.edit")} icon="edit" small onClick={() => setEditing(policy)} />}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </QueryState>
      </Panel>

      {/* KEYED, and it is a correctness fix rather than a nicety. This dialog
          is never unmounted (the parent keeps it rendered and toggles
          `policy`) so `useState(existing?.repository_id ?? "")` ran ONCE,
          with `policy === null`. Clicking Edit on a real policy therefore
          opened a dialog showing blank fields, and saving it wrote
          `window_cron: null` and an empty device group over the stored
          policy. The key remounts per target, so the fields show the policy
          the operator picked, and a previous failure cannot linger. */}
      <PolicyDialog
        key={editing === "new" ? "new" : (editing?.id ?? "closed")}
        policy={editing}
        onClose={() => setEditing(null)}
      />
    </div>
  );
}

/**
 * What a policy may not be saved without.
 *
 * `PUT /backup/policies` declares `repository_id` required (and typed as a
 * repository UUID): a policy with no repository has nowhere to write. The
 * picker's value is empty until something is chosen, so absence is the only
 * shape check needed here.
 */
const policyRules = (): readonly FieldRule[] => [
  { name: "repositoryId", label: translate("backup.policyDialog.repository") },
];

function PolicyDialog({
  policy,
  onClose,
}: {
  policy: BackupPolicy | "new" | null;
  onClose: () => void;
}) {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const repositories = useBackupRepositories();
  const groups = useDeviceGroups();
  const upsert = useUpsertBackupPolicyMutation();

  const existing = policy === "new" || policy == null ? null : policy;
  const [deviceGroupId, setDeviceGroupId] = useState(existing?.device_group_id ?? "");
  const [repositoryId, setRepositoryId] = useState(existing?.repository_id ?? "");
  const [windowCron, setWindowCron] = useState(existing?.window_cron ?? "");
  const [enabled, setEnabled] = useState(existing?.enabled ?? true);
  // Populated ON SUBMIT, never while typing. Held as data (rules and values) so
  // a language switch re-words the sentences.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  // The client this policy is, from the row where there is one: the policy
  // being edited, or the group chosen.
  // PHASE 3: the client/site selector. A new client default has no row to
  // take its client from, so until the selector exists it is the
  // organisation's internal client -- `/me`'s `org_id` (`lib/internalClient.ts`).
  const client = policyClient(existing, deviceGroupId, groups.data ?? [], internalClientId(me.data));
  const clientRepositories = repositoriesOfClient(repositories.data ?? [], client);
  const repositoryOptions = repositoryPickerOptions(clientRepositories);
  // A repository of another client is not this policy's to name -- one a
  // stored policy kept from before the rule, or one picked before the group
  // changed -- so it is not selected, and Save asks for one of this client's.
  const chosenRepository = clientRepositories.some((repo) => repo.id === repositoryId)
    ? repositoryId
    : "";
  // Gated on a settled query so a slow fetch never reads as an empty one.
  const noRepositories = repositories.isSuccess && clientRepositories.length === 0;
  // PHASE 3: the client/site selector. The Repositories page files a new
  // repository under the internal client only, so it is no place to send a
  // person whose policy is another client's: for that one the hint says where
  // its repository is made, and there is no link.
  const elsewhere = me.data !== undefined && client !== null && client !== internalClientId(me.data);
  // The client default is a real, explicit choice -- not an unlabelled blank
  // -- so it is an entry in the list itself, with the id a group-less policy
  // actually carries (none, i.e. "").
  const groupOptions = [
    {
      id: "",
      label: t("backup.policies.clientDefault"),
      sublabel: t("backup.policies.clientDefaultHint"),
    },
    ...groupPickerOptions(groups.data ?? []),
  ];

  function submit() {
    // The previous outcome must not be readable as this one.
    upsert.reset();
    const next: Attempt = { rules: policyRules, values: { repositoryId: chosenRepository } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    upsert.mutate(
      () => ({
        device_group_id: deviceGroupId || null,
        // A group's policy is the group's client's, which the server takes
        // from the group; a client default has nothing else to name it.
        ...(deviceGroupId ? {} : { client_id: requireKnown(client) }),
        repository_id: chosenRepository,
        window_cron: windowCron || null,
        enabled,
        // PUT /backup/policies is a full-body upsert (mirrors
        // modules/patch's policy upsert): omitting these would fall back
        // to the backend's empty-dict defaults and silently wipe an
        // existing policy's sources/retention_gfs on every edit, so carry
        // the current values forward untouched (this editor has no UI for
        // editing them directly; there's no documented sources/retention_gfs
        // schema to build a form against yet).
        sources: existing?.sources ?? {},
        retention_gfs: existing?.retention_gfs ?? {},
      }),
      {
        onSuccess: () => {
          onClose();
        },
      },
    );
  }

  return (
    <Dialog
      open={policy != null}
      onClose={onClose}
      title={policy === "new" ? t("backup.policyDialog.titleNew") : t("backup.policyDialog.titleEdit")}
    >
      {/* A real <form>, so Enter in any field saves the policy. It did nothing
          before: there was no <form> on this dialog at all. */}
      <Form onSubmit={submit}>
        <div className="form-grid">
          {/* No `emptyHint`: `groupOptions` always carries the client-default
              entry, so this list can never be empty (picker.tsx review
              finding 8). */}
          <EntityPicker
            label={t("backup.policyDialog.appliesTo")}
            options={groupOptions}
            value={deviceGroupId}
            onChange={setDeviceGroupId}
            loading={groups.isLoading}
            placeholder={t("backup.policyDialog.searchGroups")}
          />
          {/* CYCLES 6 AND 7. On a fresh install this `<select>` held only its
              own "Select a repository…" placeholder, so a backup policy could
              not be completed: the same dead end the audit filed as a BLOCKER
              on /provisioning, on the module where an incomplete policy means
              nothing is being backed up at all. `EntityPicker` renders
              `emptyHint` in place of the empty list and the link names the
              surface that fills it; the sublabel carries the storage ref and
              state, because two repositories differ by the bucket they write to
              and a `disabled` one is a schedule that writes nowhere. */}
          <EntityPicker
            label={t("backup.policyDialog.repository")}
            options={repositoryOptions}
            value={chosenRepository}
            onChange={setRepositoryId}
            loading={repositories.isLoading}
            emptyHint={elsewhere ? repositoryPickerEmptyHintElsewhere() : repositoryPickerEmptyHint()}
            placeholder={t("backup.policyDialog.searchRepositories")}
          />
          {noRepositories && !elsewhere ? (
            <p className="form-hint">
              <Link to="/backup/repositories">{t("backup.policyDialog.createRepository")}</Link>
            </p>
          ) : null}
          {/* NOT A `<label>`: the grid is twenty-four times seven buttons, and
              a label wrapping it would name only the first. The caption is the
              design's `.field > span`; the grid names itself, and the raw
              expression is the painter's "Cron expression" field. */}
          <div className="field full">
            <span>{t("backup.policyDialog.windowCaption")}</span>
            <WeekPainter
              label={t("backup.policyDialog.windowLabel")}
              value={windowCron || null}
              onChange={(cron) => setWindowCron(cron ?? "")}
              emptyHint={t("backup.policyDialog.windowEmpty")}
            />
          </div>
          <label className="field full">
            <span className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={enabled}
                onChange={(e) => setEnabled(e.target.checked)}
              />
              {t("backup.policyDialog.enabled")}
            </span>
          </label>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          {/* `type="button"`: `ui.tsx`'s Button sets no type, so an untyped
              button inside the <form> would submit it: Cancel would save. */}
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("common.actions.cancel")}
          </Button>
          {/* CYCLE 4. Was `{String(upsert.error)}` in an unannounced <p>. A
              bodyless 503 carries `message === ""`, so that rendered the string
              "Error: " and no screen reader was told anything happened;
              `describeSubmitError` (via FormActions) turns status and body into
              a sentence inside a role="alert". The Save control also no longer
              disables itself on a missing Repository: that was the silent
              no-op; the click now names the field. */}
          <FormActions
            label={t("backup.policyDialog.save")}
            pendingLabel={t("backup.policyDialog.saving")}
            pending={upsert.isPending}
            error={upsert.error}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
}
