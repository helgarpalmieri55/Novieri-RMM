import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { describeSubmitError } from "../../components/form";
import { Button, Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate, type MessageKey } from "../../i18n/core";
import { Trans } from "../../i18n/Trans";
import type {
  AgentRelease,
  Me,
  UpdatePreview,
  UpdateRollout,
  UpdateRolloutAction,
  UpdateRolloutRequest,
} from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDateTime, intlLocale } from "../../lib/datetime";
import { describeSeconds } from "../../lib/format";
import { humaniseEnum } from "../../lib/labels";
import { Panel, QueryState, StatusPill, type Kind } from "../../shell/components";
import { useEverywherePermissions } from "../../lib/mePermissions";

/**
 * Agent self-update (spec-self-update.md §8.6): fleet versions, the devices
 * that need a manual update, and rollouts in rings a person advances.
 *
 * Buttons render from `/me` as a convenience only; every route enforces its
 * own permission server-side.
 */

const MANUAL_REASON: Record<string, MessageKey> = {
  version_unknown: "agentReleases.updates.reasonVersionUnknown",
  no_agent_update: "agentReleases.updates.reasonNoAgentUpdate",
};

/**
 * A code the server stores as a reason, shown as it arrives in English and
 * worded in Spanish where the console knows it; an unknown code stays as sent.
 */
const KNOWN_REASONS: Record<string, MessageKey> = {
  failure_threshold: "agentReleases.updates.pausedFailureThreshold",
  ring_closed: "agentReleases.updates.reasonRingClosed",
  cancelled: "agentReleases.updates.reasonCancelled",
};

/**
 * The ring names the console itself proposes. They are STORED DATA, sent to the
 * server in exactly these words whatever the language; only their display is
 * worded, and only for these two known names (any other name is shown as is).
 */
const PILOT_RING = "Pilot";
const EVERYONE_ELSE_RING = "Everyone else";

function ringLabel(name: string): string {
  if (name === PILOT_RING) return translate("agentReleases.newRollout.pilotName");
  if (name === EVERYONE_ELSE_RING) return translate("agentReleases.newRollout.everyoneElseName");
  return name;
}

function reasonText(code: string): string {
  const key = KNOWN_REASONS[code];
  return key ? translate(key) : code;
}

const ROLLOUT_KIND: Record<UpdateRollout["state"], Kind> = {
  draft: "mut",
  running: "run",
  paused: "warn",
  completed: "ok",
  cancelled: "mut",
};

function ringProgress(counts: Record<string, number>): string {
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const done = (counts.succeeded ?? 0) + (counts.skipped ?? 0);
  const failed =
    (counts.failed ?? 0) + (counts.rolled_back ?? 0) + (counts.refused ?? 0) + (counts.lost ?? 0);
  const manual = counts.needs_manual_update ?? 0;
  return translate("agentReleases.updates.ringProgress", { done, total, failed, manual });
}

export function AgentUpdatesSection({ me }: { me: Me | undefined }) {
  const t = useT();
  const api = useApi();
  // Configuration every client shares (GLOBAL_WRITES): the code on EVERY client (phase 3 design §2).
  const canRollout = useEverywherePermissions().has("agent.update.rollout");
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const fleet = useQuery({
    queryKey: ["agent-fleet-versions"],
    queryFn: () => api.getFleetVersions(),
  });
  const rollouts = useQuery({
    queryKey: ["agent-update-rollouts"],
    queryFn: () => api.listUpdateRollouts(),
    refetchInterval: 30_000,
  });
  const current = rollouts.data?.find((r) => r.id === selected) ?? null;

  return (
    <>
      <div id="fleet-versions">
        <Panel
          title={t("agentReleases.updates.fleetTitle")}
          subtitle={
            fleet.data
              ? [
                  t("agentReleases.updates.agents", { count: fleet.data.total }),
                  t("agentReleases.updates.latestStable", {
                    version: fleet.data.latest_stable ?? t("agentReleases.updates.none"),
                  }),
                  t("agentReleases.updates.canUpdate", { count: fleet.data.update_available }),
                ].join(" · ")
              : undefined
          }
        >
          <QueryState loading={fleet.isLoading} error={fleet.error} empty={false} emptyLabel="">
            {fleet.data && (
              <>
                <p className="cell-sub">
                  {Object.entries(fleet.data.versions)
                    .sort(([a], [b]) => a.localeCompare(b, intlLocale()))
                    .map(([v, n]) => `${v === "unknown" ? t("agentReleases.updates.unknown") : v}: ${n}`)
                    .join(" · ") || t("agentReleases.updates.noAgents")}
                </p>
                <h3 className="mt-3">{t("agentReleases.updates.needsManual")}</h3>
                {fleet.data.needs_manual_update.length === 0 ? (
                  <p className="cell-sub">{t("agentReleases.updates.everyAgentCan")}</p>
                ) : (
                  <>
                    <p className="cell-sub">
                      <Trans k="agentReleases.updates.predate" components={{ link: <a href="#releases" /> }} />
                    </p>
                    <Table label={t("agentReleases.updates.manualTable")}>
                      <thead>
                        <tr>
                          <Th>{t("agentReleases.updates.device")}</Th>
                          <Th>{t("agentReleases.updates.version")}</Th>
                          <Th>{t("agentReleases.updates.why")}</Th>
                        </tr>
                      </thead>
                      <tbody>
                        {fleet.data.needs_manual_update.map((d) => (
                          <tr key={d.device_id}>
                            <Td>{d.hostname ?? d.device_id}</Td>
                            <Td className="mono">{d.version || t("agentReleases.updates.unknown")}</Td>
                            <Td>
                              <StatusPill value="manual" kind="warn" label={t("agentReleases.updates.manualPill")} />{" "}
                              <span className="cell-sub">
                                {/* The job type is data handed to the sentence, not copy. */}
                                {MANUAL_REASON[d.reason] ? t(MANUAL_REASON[d.reason]!, { job: "agent.update" }) : undefined}
                              </span>
                            </Td>
                          </tr>
                        ))}
                      </tbody>
                    </Table>
                  </>
                )}
              </>
            )}
          </QueryState>
        </Panel>
      </div>

      <Panel
        title={t("agentReleases.updates.rolloutsTitle")}
        subtitle={t("agentReleases.updates.rolloutsSubtitle")}
        actions={
          canRollout ? (
            <Button variant="outline" onClick={() => setCreating(true)}>
              {t("agentReleases.updates.newRollout")}
            </Button>
          ) : undefined
        }
        bare
      >
        <QueryState
          loading={rollouts.isLoading}
          error={rollouts.error}
          empty={(rollouts.data ?? []).length === 0}
          emptyLabel={t("agentReleases.updates.noRollouts")}
          emptyIcon="package"
        >
          <Table label={t("agentReleases.updates.rolloutsTable")}>
            <thead>
              <tr>
                <Th>{t("agentReleases.updates.version")}</Th>
                <Th>{t("agentReleases.updates.state")}</Th>
                <Th>{t("agentReleases.updates.rings")}</Th>
                <Th>{t("agentReleases.updates.created")}</Th>
                <Th>{""}</Th>
              </tr>
            </thead>
            <tbody>
              {(rollouts.data ?? []).map((r) => (
                <tr key={r.id}>
                  <Td className="mono">
                    {r.is_rollback
                      ? t("agentReleases.updates.withRollback", { version: r.target_version })
                      : r.target_version}
                  </Td>
                  <Td>
                    <StatusPill value={r.state} kind={ROLLOUT_KIND[r.state]} label={humaniseEnum(r.state, "UpdateRolloutState")} />
                    {r.pause_reason ? <div className="cell-sub">{reasonText(r.pause_reason)}</div> : null}
                  </Td>
                  <Td>
                    {t("agentReleases.updates.ringsDone", {
                      done: r.rings.filter((g) => g.state === "done").length,
                      total: r.rings.length,
                    })}
                  </Td>
                  <Td>{formatDateTime(r.created_at, me?.timezone)}</Td>
                  <Td>
                    <Button
                      variant="ghost"
                      onClick={() => setSelected(r.id === selected ? null : r.id)}
                    >
                      {r.id === selected ? t("agentReleases.updates.hide") : t("agentReleases.updates.details")}
                    </Button>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </QueryState>
      </Panel>

      {current && <RolloutDetail rollout={current} canRollout={canRollout} />}
      {creating && <NewRolloutDialog onClose={() => setCreating(false)} />}
    </>
  );
}

function RolloutDetail({ rollout, canRollout }: { rollout: UpdateRollout; canRollout: boolean }) {
  const t = useT();
  const api = useApi();
  const qc = useQueryClient();
  const targets = useQuery({
    queryKey: ["agent-update-targets", rollout.id],
    queryFn: () => api.listUpdateTargets(rollout.id),
    refetchInterval: 30_000,
  });
  const act = useMutation({
    mutationFn: (a: { action: UpdateRolloutAction } | { close: string }) =>
      "close" in a
        ? api.closeUpdateRing(rollout.id, a.close)
        : api.updateRolloutAction(rollout.id, a.action),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["agent-update-rollouts"] });
      void qc.invalidateQueries({ queryKey: ["agent-update-targets", rollout.id] });
    },
  });
  const active = rollout.rings.find((g) => g.state === "active");
  const pendingRing = rollout.rings.some((g) => g.state === "pending");
  const s = rollout.state;
  const buttons: [MessageKey, () => void, boolean][] = [
    ["agentReleases.updates.start", () => act.mutate({ action: "start" }), s === "draft"],
    ["agentReleases.updates.pause", () => act.mutate({ action: "pause" }), s === "running"],
    ["agentReleases.updates.resume", () => act.mutate({ action: "resume" }), s === "paused"],
    [
      "agentReleases.updates.advance",
      () => act.mutate({ action: "advance" }),
      s === "running" && !active && pendingRing,
    ],
    ["agentReleases.updates.closeRing", () => active && act.mutate({ close: active.id }), active !== undefined],
    [
      "agentReleases.updates.cancel",
      () => act.mutate({ action: "cancel" }),
      s === "draft" || s === "running" || s === "paused",
    ],
  ];
  const ringName = new Map(rollout.rings.map((g) => [g.id, g.name]));

  return (
    <Panel
      title={t("agentReleases.updates.rolloutOf", { version: rollout.target_version })}
      subtitle={t("agentReleases.updates.rolloutSubtitle", {
        count: rollout.batch_size,
        pct: rollout.failure_threshold_pct,
        window: describeSeconds(rollout.health_window_s),
      })}
      actions={
        canRollout ? (
          <div className="flex gap-2">
            {buttons
              .filter(([, , shown]) => shown)
              .map(([label, onClick]) => (
                <Button key={label} variant="outline" disabled={act.isPending} onClick={onClick}>
                  {t(label)}
                </Button>
              ))}
          </div>
        ) : undefined
      }
    >
      {act.isError && (
        <p role="alert" className="form-error">
          {describeSubmitError(act.error)}
        </p>
      )}
      <ul>
        {rollout.rings.map((g) => (
          <li key={g.id}>
            <strong>{t("agentReleases.updates.ring", { ordinal: g.ordinal, name: ringLabel(g.name) })}</strong>{" "}
            {g.is_pilot ? `${t("agentReleases.updates.firstToUpdate")} ` : ""}
            <StatusPill value={g.state} label={humaniseEnum(g.state, "UpdateRingState")} />{" "}
            <span className="cell-sub">{ringProgress(g.counts)}</span>
          </li>
        ))}
      </ul>
      <QueryState
        loading={targets.isLoading}
        error={targets.error}
        empty={(targets.data ?? []).length === 0}
        emptyLabel={t("agentReleases.updates.noTargets")}
      >
        <Table label={t("agentReleases.updates.targetsTable")}>
          <thead>
            <tr>
              <Th>{t("agentReleases.updates.device")}</Th>
              <Th>{t("agentReleases.updates.ringColumn")}</Th>
              <Th>{t("agentReleases.updates.from")}</Th>
              <Th>{t("agentReleases.updates.state")}</Th>
              <Th>{t("agentReleases.updates.reason")}</Th>
            </tr>
          </thead>
          <tbody>
            {(targets.data ?? []).map((row) => (
              <tr key={row.id}>
                <Td>{row.hostname ?? row.device_id}</Td>
                <Td>{ringLabel(ringName.get(row.ring_id) ?? "")}</Td>
                <Td className="mono">{row.from_version || t("agentReleases.updates.unknown")}</Td>
                <Td>
                  {row.state === "needs_manual_update" ? (
                    <StatusPill value="manual" kind="warn" label={t("agentReleases.updates.manualPill")} />
                  ) : (
                    <StatusPill value={row.state} label={humaniseEnum(row.state, "UpdateTargetState")} />
                  )}
                </Td>
                <Td className="mono">{reasonText(row.reason)}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </QueryState>
    </Panel>
  );
}

/** The channel as the option list words it: the wire word in English, a Spanish word otherwise. */
function channelWord(channel: string): string {
  if (channel === "stable") return translate("agentReleases.channel.stableLower");
  if (channel === "pilot") return translate("agentReleases.channel.pilotLower");
  if (channel === "mixed") return translate("agentReleases.channel.mixedLower");
  return channel;
}

function versionsOf(releases: AgentRelease[]): { version: string; channel: string }[] {
  const byVersion = new Map<string, Set<string>>();
  for (const r of releases) {
    if (!r.has_artifact) continue;
    const set = byVersion.get(r.version) ?? new Set<string>();
    set.add(r.channel ?? "pilot");
    byVersion.set(r.version, set);
  }
  return [...byVersion.entries()].map(([version, channels]) => ({
    version,
    channel: channels.size === 1 ? ([...channels][0] ?? "pilot") : "mixed",
  }));
}

function NewRolloutDialog({ onClose }: { onClose: () => void }) {
  const t = useT();
  const api = useApi();
  const qc = useQueryClient();
  const releases = useQuery({
    queryKey: ["agent-releases"],
    queryFn: () => api.listAgentReleases(),
  });
  const groups = useQuery({ queryKey: ["device-groups"], queryFn: () => api.listDeviceGroups() });
  const [version, setVersion] = useState("");
  const [pilotGroup, setPilotGroup] = useState("");
  const [everyoneElse, setEveryoneElse] = useState(true);
  const [batch, setBatch] = useState(10);
  const [preview, setPreview] = useState<UpdatePreview | null>(null);

  const body = (): UpdateRolloutRequest => ({
    target_version: version,
    batch_size: batch,
    rings: [
      { name: PILOT_RING, device_group_id: pilotGroup, is_pilot: true },
      ...(everyoneElse ? [{ name: EVERYONE_ELSE_RING, device_group_id: null }] : []),
    ],
  });
  const previewing = useMutation({
    mutationFn: () => api.previewUpdateRollout(body()),
    onSuccess: setPreview,
  });
  const starting = useMutation({
    mutationFn: async () => {
      const created = await api.createUpdateRollout(body());
      return api.updateRolloutAction(created.id, "start");
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["agent-update-rollouts"] });
      onClose();
    },
  });
  const versions = versionsOf(releases.data ?? []);
  const ready = version !== "" && pilotGroup !== "";
  const error = previewing.error ?? starting.error;

  return (
    <Dialog open title={t("agentReleases.newRollout.title")} onClose={onClose}>
      <div className="form-grid">
        <Field label={t("agentReleases.newRollout.version")}>
          <select
            className={inputClass}
            value={version}
            onChange={(e) => {
              setVersion(e.target.value);
              setPreview(null);
            }}
          >
            <option value="">{t("agentReleases.newRollout.chooseVersion")}</option>
            {versions.map((v) => (
              <option key={v.version} value={v.version}>
                {t("agentReleases.newRollout.versionOption", { version: v.version, channel: channelWord(v.channel) })}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t("agentReleases.newRollout.pilotGroup")}>
          <select
            className={inputClass}
            value={pilotGroup}
            onChange={(e) => {
              setPilotGroup(e.target.value);
              setPreview(null);
            }}
          >
            <option value="">{t("agentReleases.newRollout.chooseGroup")}</option>
            {(groups.data?.items ?? []).map((g) => (
              <option key={g.id} value={g.id}>
                {g.name}
              </option>
            ))}
          </select>
        </Field>
        <label className="field full flex items-center gap-2">
          <input
            type="checkbox"
            checked={everyoneElse}
            onChange={(e) => {
              setEveryoneElse(e.target.checked);
              setPreview(null);
            }}
          />
          {t("agentReleases.newRollout.everyoneElse")}
        </label>
        <Field label={t("agentReleases.newRollout.batch")}>
          <input
            className={inputClass}
            type="number"
            min={1}
            max={100}
            value={batch}
            onChange={(e) => setBatch(Number(e.target.value))}
          />
        </Field>
      </div>
      {preview && (
        <Table label={t("agentReleases.newRollout.previewTable")}>
          <thead>
            <tr>
              <Th>{t("agentReleases.newRollout.ring")}</Th>
              <Th>{t("agentReleases.newRollout.eligible")}</Th>
              <Th>{t("agentReleases.newRollout.needsManual")}</Th>
              <Th>{t("agentReleases.newRollout.noArtefact")}</Th>
              <Th>{t("agentReleases.newRollout.alreadyCurrent")}</Th>
            </tr>
          </thead>
          <tbody>
            {preview.rings.map((r) => (
              <tr key={r.ordinal}>
                <Td>{ringLabel(r.name)}</Td>
                <Td>{r.eligible}</Td>
                <Td>{r.needs_manual_update}</Td>
                <Td>{r.no_artifact}</Td>
                <Td>{r.already_current}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      {error && (
        <p role="alert" className="form-error">
          {describeSubmitError(error)}
        </p>
      )}
      <div className="flex justify-end gap-2 mt-3">
        <Button variant="outline" onClick={onClose}>
          {t("agentReleases.newRollout.cancel")}
        </Button>
        <Button
          variant="outline"
          disabled={!ready || previewing.isPending}
          onClick={() => previewing.mutate()}
        >
          {t("agentReleases.newRollout.preview")}
        </Button>
        <Button disabled={!preview || starting.isPending} onClick={() => starting.mutate()}>
          {t("agentReleases.newRollout.createStart")}
        </Button>
      </div>
    </Dialog>
  );
}
