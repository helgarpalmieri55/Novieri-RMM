// One site server: what it declares, what its agent is doing, what inventory
// has actually seen, and the wake action.
//
// The detail pane is where the DECLARATION and the EVIDENCE sit next to each
// other, which is the only place an operator can see that a promotion accepted a
// claim nobody has verified (§3.3, §0.8).
//
// ON THE v3 KIT: the facts strip the design draws under the head (state,
// segments, last sweep, push installs) carries the facts this API actually
// serves -- state, agent, its last contact, the declared segments and the
// roles -- and the two panels under it are the segment check and the wake
// form, each with the title v3 gives a panel and the sentence under it.

import { NoValue } from "../../components/emptystate";
import { useT } from "../../i18n/context";
import type { Device, Job } from "../../lib/api";
import { RelTime } from "../../components/reltime";
import { Panel, StatusPill } from "../../shell/components";
import { Facts, OsIcon } from "../../shell/viz";
import { SegmentCheck } from "./SegmentCheck";
import { WakeAction } from "./WakeAction";
import {
  roleLabels,
  type SegmentCheckOut,
  type SiteServerOut,
  type SiteWakeOut,
  type SiteWakeRequest,
} from "./types";

export function SiteServerDetail({
  server,
  device,
  check,
  checkLoading,
  canWake,
  onWake,
  wakeResult,
  wakeJob,
  wakeError,
  zone = null,
}: {
  server: SiteServerOut | undefined;
  /** The device this server is, when the page could read it. Optional: the
   *  declaration renders without it. */
  device?: Device;
  check: SegmentCheckOut | undefined;
  checkLoading?: boolean;
  canWake?: boolean;
  onWake: (body: SiteWakeRequest) => void;
  wakeResult?: SiteWakeOut | null;
  /**
   * The wake job as the control plane records it (`useWakeJob`). Absent means
   * "not looked up" and renders as `queued`, never as "sent".
   */
  wakeJob?: Job | null;
  wakeError?: { message?: string } | null;
  /** The org's zone, for every time shown here. */
  zone?: string | null;
}) {
  const t = useT();
  if (!server) {
    return (
      <div className="empty">
        <b>{t("siteServers.detail.loading")}</b>
      </div>
    );
  }

  return (
    <article aria-label={t("siteServers.detail.article", { name: server.name })}>
      <Facts
        items={[
          {
            k: t("siteServers.detail.state"),
            v: (
              <span data-testid="detail-state">
                {server.enabled ? (
                  <StatusPill value="enabled" kind="ok" label={t("siteServers.list.enabledPill")} />
                ) : (
                  <StatusPill value="disabled" kind="mut" label={t("siteServers.list.disabledPill")} />
                )}
              </span>
            ),
          },
          {
            k: t("siteServers.detail.agent"),
            v: (
              <span data-testid="detail-agent">
                {server.agent_status === null ? (
                  <StatusPill value="missing" kind="crit" label={t("siteServers.list.agentMissing")} />
                ) : (
                  <StatusPill value={server.agent_status} />
                )}
              </span>
            ),
          },
          {
            k: t("siteServers.detail.lastSeen"),
            v: server.agent_last_seen_at ? (
              <RelTime iso={server.agent_last_seen_at} zone={zone} />
            ) : (
              <NoValue label={t("siteServers.detail.neverSeen")} />
            ),
          },
          {
            k: t("siteServers.detail.device"),
            v: device ? (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <OsIcon os={device.os_family ?? ""} size={16} />
                <span className="mono">{device.hostname ?? device.id}</span>
              </span>
            ) : (
              <span className="mono">{server.device_id}</span>
            ),
          },
          {
            k: t("siteServers.detail.declared"),
            v: (
              <span className="mono" data-testid="detail-segments">
                {server.segments.join(", ")}
              </span>
            ),
          },
          {
            k: t("siteServers.detail.roles"),
            v: (
              // The same words the list uses, for the same reason: "wake" is
              // the wire token, not what this server does.
              <span data-testid="detail-roles">{roleLabels(server.roles)}</span>
            ),
          },
        ]}
      />

      <Panel title={t("siteServers.detail.segmentCheck")}>
        <SegmentCheck check={check} isLoading={checkLoading} />
      </Panel>

      {canWake ? (
        <Panel
          title={t("siteServers.detail.wakeTitle")}
          subtitle={t("siteServers.detail.wakeSubtitle")}
        >
          <WakeAction
            server={server}
            onWake={onWake}
            result={wakeResult}
            job={wakeJob}
            error={wakeError}
            zone={zone}
          />
        </Panel>
      ) : null}
    </article>
  );
}
