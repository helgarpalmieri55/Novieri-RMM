import { useState } from "react";

import { ConfirmDialog } from "../../components/confirm";
import { describeSubmitError } from "../../components/form";
import { Button, Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { AgentRelease } from "../../lib/api";
import { Panel, StatusPill } from "../../shell/components";
import { usePromoteAgentVersionMutation, useRemoveAgentVersionMutation } from "./api";
import { intlLocale } from "../../lib/datetime";

/**
 * Release channels, one row per VERSION, for a publisher.
 *
 * PER VERSION BECAUSE THE ROUTE IS. `POST /agent/releases/versions/{v}/promote`
 * sets the channel of every (os, arch) row of the version at once, so a button
 * on each platform row of the catalogue would read as though it changed one
 * package and would change six.
 *
 * WHAT THE CHANNEL MEANS, which is what the confirmation has to say:
 * `agent_update.service._require_stable` refuses to start or advance any ring
 * other than the pilot ring for a version that is not stable. It does not
 * touch devices that have already updated.
 */

export interface VersionSummary {
  version: string;
  /** `mixed` when some rows are stable and some are not. */
  channel: "pilot" | "stable" | "mixed";
  packages: number;
  /** Packages with bytes uploaded but no release signature recorded. */
  unsigned: number;
}

function semverParts(version: string): { core: number[]; pre: string[] } {
  const [core = "", pre] = version.split("+")[0]!.split(/-(.*)/s);
  return {
    core: core.split(".").map((n) => Number.parseInt(n, 10) || 0),
    pre: pre ? pre.split(".") : [],
  };
}

/**
 * Newest first, by semantic-version precedence -- the order the server's own
 * `agent_update.semver` uses. A pre-release sorts BELOW its release
 * (`1.5.0-rc.1` < `1.5.0`): plain string order put the rc first, so the page
 * showed it as "the newest" and folded the stable build away (Codex review).
 */
export function compareVersions(a: string, b: string): number {
  const x = semverParts(a);
  const y = semverParts(b);
  for (let i = 0; i < Math.max(x.core.length, y.core.length); i++) {
    const d = (y.core[i] ?? 0) - (x.core[i] ?? 0);
    if (d !== 0) return d;
  }
  if (x.pre.length === 0 || y.pre.length === 0) return x.pre.length - y.pre.length;
  for (let i = 0; i < Math.max(x.pre.length, y.pre.length); i++) {
    const p = x.pre[i];
    const q = y.pre[i];
    if (p === undefined) return 1;
    if (q === undefined) return -1;
    const pn = /^\d+$/.test(p);
    const qn = /^\d+$/.test(q);
    if (pn && qn && Number(p) !== Number(q)) return Number(q) - Number(p);
    if (pn !== qn) return pn ? 1 : -1;
    if (p !== q) return q.localeCompare(p, intlLocale());
  }
  return 0;
}

export function summariseVersions(releases: AgentRelease[]): VersionSummary[] {
  const byVersion = new Map<string, AgentRelease[]>();
  for (const release of releases) {
    byVersion.set(release.version, [...(byVersion.get(release.version) ?? []), release]);
  }
  return [...byVersion.entries()]
    .map(([version, rows]) => {
      const channels = new Set(rows.map((row) => row.channel ?? "pilot"));
      const channel: VersionSummary["channel"] =
        channels.size > 1 ? "mixed" : channels.has("stable") ? "stable" : "pilot";
      return {
        version,
        channel,
        packages: rows.length,
        unsigned: rows.filter((row) => row.has_artifact && !row.release_signed).length,
      };
    })
    .sort((a, b) => compareVersions(a.version, b.version));
}

export function ChannelPill({ channel }: { channel: VersionSummary["channel"] }) {
  const t = useT();
  if (channel === "stable") return <StatusPill value="stable" kind="ok" label={t("agentReleases.channel.stable")} />;
  if (channel === "mixed") return <StatusPill value="mixed" kind="warn" label={t("agentReleases.channel.mixed")} />;
  return <StatusPill value="pilot" kind="info" label={t("agentReleases.channel.pilot")} />;
}

function confirmBody(target: VersionSummary, channel: "pilot" | "stable"): string {
  const scope = translate("agentReleases.channels.scope", {
    packages: translate("agentReleases.channels.packageCount", { count: target.packages }),
    version: target.version,
  });
  if (channel === "stable") {
    const warning =
      target.unsigned > 0 ? ` ${translate("agentReleases.channels.unsignedWarning", { count: target.unsigned })}` : "";
    return `${translate("agentReleases.channels.stableBody")} ${scope}${warning}`;
  }
  return `${translate("agentReleases.channels.pilotBody")} ${scope}`;
}

function removeBody(target: VersionSummary): string {
  return translate("agentReleases.channels.removeBody", {
    packages: translate("agentReleases.channels.packageCount", { count: target.packages }),
    version: target.version,
  });
}

export function ReleaseChannels({ releases }: { releases: AgentRelease[] }) {
  const t = useT();
  const promote = usePromoteAgentVersionMutation();
  const remove = useRemoveAgentVersionMutation();
  const [removing, setRemoving] = useState<VersionSummary | null>(null);
  const [pending, setPending] = useState<{ target: VersionSummary; channel: "pilot" | "stable" } | null>(
    null,
  );
  const versions = summariseVersions(releases);
  if (versions.length === 0) return null;

  return (
    <Panel
      title={t("agentReleases.channels.title")}
      subtitle={t("agentReleases.channels.subtitle")}
      bare
    >
      {promote.isError && (
        <p role="alert" className="form-error">
          {describeSubmitError(promote.error)}
        </p>
      )}
      {remove.isError && (
        <p role="alert" className="form-error">
          {t("agentReleases.channels.removeFailed", { detail: describeSubmitError(remove.error) })}
        </p>
      )}
      {remove.isSuccess && remove.data.files_not_removed.length > 0 && (
        <p role="alert" className="form-error">
          {t("agentReleases.channels.filesLeft", {
            version: remove.data.version,
            count: remove.data.files_not_removed.length,
            files: remove.data.files_not_removed.join(", "),
          })}
        </p>
      )}
      <Table label={t("agentReleases.channels.title")}>
        <thead>
          <tr>
            <Th>{t("agentReleases.channels.version")}</Th>
            <Th>{t("agentReleases.channels.channel")}</Th>
            <Th>{t("agentReleases.channels.packages")}</Th>
            <Th>{t("agentReleases.channels.signatures")}</Th>
            <Th>{""}</Th>
          </tr>
        </thead>
        <tbody>
          {versions.map((v) => (
            <tr key={v.version}>
              <Td className="mono whitespace-nowrap">{v.version}</Td>
              <Td>
                <ChannelPill channel={v.channel} />
              </Td>
              <Td>{t("agentReleases.channels.platformCount", { count: v.packages })}</Td>
              <Td>
                {v.unsigned === 0 ? (
                  <span className="cell-sub">{t("agentReleases.channels.allRecorded")}</span>
                ) : (
                  <StatusPill
                    value="missing"
                    kind="warn"
                    label={t("agentReleases.channels.missingFor", { unsigned: v.unsigned, packages: v.packages })}
                  />
                )}
              </Td>
              <Td>
                <div className="flex justify-end gap-2">
                  {v.channel !== "stable" && (
                    <Button
                      variant="outline"
                      disabled={promote.isPending}
                      onClick={() => {
                        promote.reset();
                        setPending({ target: v, channel: "stable" });
                      }}
                    >
                      {t("agentReleases.channels.markStable")}
                    </Button>
                  )}
                  {v.channel !== "pilot" && (
                    <Button
                      variant="outline"
                      disabled={promote.isPending}
                      onClick={() => {
                        promote.reset();
                        setPending({ target: v, channel: "pilot" });
                      }}
                    >
                      {t("agentReleases.channels.moveToPilot")}
                    </Button>
                  )}
                  {/* Removal, for an old version cluttering the catalogue.
                      Per version for the same reason promotion is: six
                      platform rows are one release. */}
                  <Button
                    variant="outline"
                    disabled={remove.isPending}
                    onClick={() => {
                      remove.reset();
                      setRemoving(v);
                    }}
                  >
                    {t("agentReleases.channels.remove")}
                  </Button>
                </div>
              </Td>
            </tr>
          ))}
        </tbody>
      </Table>

      <ConfirmDialog
        open={removing != null}
        title={t("agentReleases.channels.removeTitle")}
        subject={removing ? t("agentReleases.channels.removeSubject", { version: removing.version }) : ""}
        body={removing ? removeBody(removing) : ""}
        confirmLabel={t("agentReleases.channels.removeConfirm")}
        destructive
        onCancel={() => setRemoving(null)}
        onConfirm={() => {
          const chosen = removing;
          setRemoving(null);
          if (chosen) remove.mutate({ version: chosen.version });
        }}
      />

      <ConfirmDialog
        open={pending != null}
        title={pending?.channel === "pilot" ? t("agentReleases.channels.toPilotTitle") : t("agentReleases.channels.toStableTitle")}
        subject={pending ? t("agentReleases.channels.removeSubject", { version: pending.target.version }) : ""}
        body={pending ? confirmBody(pending.target, pending.channel) : ""}
        confirmLabel={pending?.channel === "pilot" ? t("agentReleases.channels.toPilotTitle") : t("agentReleases.channels.toStableTitle")}
        onCancel={() => setPending(null)}
        onConfirm={() => {
          const chosen = pending;
          setPending(null);
          if (chosen) promote.mutate({ version: chosen.target.version, channel: chosen.channel });
        }}
      />
    </Panel>
  );
}
