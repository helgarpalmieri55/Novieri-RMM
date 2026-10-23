// Send a Wake-on-LAN packet through a site server.
//
// THE RESULT SAYS `sent`, NEVER `woke`, AND THIS COMPONENT SAYS THE SAME. A
// magic packet is an unauthenticated, unacknowledged, fire-and-forget L2
// broadcast: there is no reply to wait for. Rendering "machine woken" would be
// a claim the platform cannot make, and a spinner that waited for one would be
// the reachability poll the module deliberately does not do. Confirmation that
// a machine woke is its agent connecting, which the device list already shows.
//
// **AND NEITHER MAY IT SAY "PACKET SENT" ON A 202.** This component used to
// render "Packet sent to {broadcast}" the moment `POST /wake` returned. That
// response is 202 ACCEPTED and `SiteWakeOut` carries `job_id`, `broadcast`,
// `target_mac` and `expires_at` — the description of a job that has been
// created, signed and published. NOT of a datagram. At that instant the job is
// `queued`; if the site server is offline it stays queued until it reconnects
// (`GatewayServer._drain_queued`) or expires at `expires_at` — five minutes,
// `WAKE_TTL_S`, because a late wake is a wrong wake. So on a 202 the true
// statement is "we have asked", and the UI now says exactly that.
//
// This repository has fixed this defect before, in `HealthReportPanel`: a UI
// sentence asserting as fact something the data does not support. The shape
// recurs because the hopeful sentence is always the shorter one.
//
// THREE STATES, NOT ONE, because they are three different facts:
//
//   queued     the control plane signed and published a job. Nothing has left
//              any NIC. The site server may not even be connected.
//   dispatched the site server has the job (`dispatched`/`running`). It has not
//              reported back.
//   reported   the agent returned a `SiteWakeResult`, the server checked its
//              `broadcast` against the SIGNED params (`handle_wake_result` —
//              the agent's report is untrusted input), and the job succeeded.
//              This is the earliest moment "a packet was sent" is true, and it
//              is still the AGENT'S REPORT that it was sent, not proof.
//
// A collapsed two-state UI ("asked" / "done") would put `queued` and `reported`
// in the same box, which is the original defect with more steps.
//
// THE BROADCAST ADDRESS IS SERVER-COMPUTED AND IS SHOWN BACK. The operator
// supplies a MAC and (when the server declares more than one segment) which
// segment; the control plane picks the containing segment and computes the
// address. Showing it back is how an operator notices they named the wrong wire
// -- and it is the value they would otherwise have no way to see.
//
// This form may be hidden without `site.wake`, but THE FRONTEND GATES NOTHING
// (invariant #6): the server returns 403 regardless.

import { useState } from "react";
import { Button, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { Job } from "../../lib/api";
import { formatDateTime } from "../../lib/datetime";
import { KIND_ICON, type Kind } from "../../shell/components";
import { Icon } from "../../shell/icons";
import type { SiteServerOut, SiteWakeOut, SiteWakeRequest } from "./types";

/** What the control plane knows about a requested wake, right now. */
export type WakeState = "queued" | "dispatched" | "reported" | "failed" | "expired";

/**
 * `job` is the wake job as the control plane currently records it, or
 * `undefined` when nothing has been looked up since the 202.
 *
 * **UNKNOWN COLLAPSES TO `queued`, NEVER TO `reported`.** "We have not looked"
 * and "the agent reported a packet" are opposite answers, and defaulting an
 * absent status to the optimistic one is how the claim this component exists to
 * avoid gets back in through the fallback branch.
 */
export function wakeStateOf(job: Job | null | undefined): WakeState {
  switch (job?.status) {
    case "succeeded":
      return "reported";
    case "dispatched":
    case "running":
      return "dispatched";
    case "failed":
    case "refused":
      return "failed";
    case "expired":
      return "expired";
    default:
      return "queued";
  }
}

/** The severity each wake state wears on its notice; the glyph travels with it. */
const WAKE_KIND: Record<WakeState, Kind> = {
  queued: "info",
  dispatched: "run",
  reported: "ok",
  failed: "crit",
  expired: "warn",
};

const NOTICE_CLASS: Record<Kind, string> = {
  crit: "notice red",
  warn: "notice gold",
  ok: "notice",
  run: "notice",
  mut: "notice",
  info: "notice",
};

function reportedSentAt(job: Job | null | undefined): string | null {
  const value = job?.result?.["sent_at"];
  return typeof value === "string" ? value : null;
}

export function WakeAction({
  server,
  onWake,
  result,
  job,
  error,
  isPending,
  zone = null,
}: {
  server: SiteServerOut;
  onWake: (body: SiteWakeRequest) => void;
  result?: SiteWakeOut | null;
  /** The wake job's current row. Absent means "not looked up", not "done". */
  job?: Job | null;
  error?: { message?: string } | null;
  isPending?: boolean;
  /** The org's zone, for the times this reports. */
  zone?: string | null;
}) {
  const t = useT();
  const [targetMac, setTargetMac] = useState("");
  const [segment, setSegment] = useState("");
  const needsSegment = server.segments.length > 1;
  const state = wakeStateOf(job);
  const raw = reportedSentAt(job);
  const sentAt = raw ? formatDateTime(raw, zone) : null;

  if (!server.enabled) {
    return (
      <div className="notice gold">
        <Icon name={KIND_ICON.warn} size={16} />
        <p data-testid="wake-disabled">
          {t("siteServers.wake.disabled")}
        </p>
      </div>
    );
  }

  return (
    <form
      aria-label={t("siteServers.wake.form")}
      onSubmit={(event) => {
        event.preventDefault();
        onWake({ target_mac: targetMac, segment: segment || null });
      }}
    >
      <div className="form-grid">
        <div className="field">
          <label htmlFor="wake-mac">{t("siteServers.wake.mac")}</label>
          <input
            id="wake-mac"
            className={inputClass}
            value={targetMac}
            onChange={(event) => setTargetMac(event.target.value)}
            // i18n-exempt: an example MAC address, an identifier written the same in every language
            placeholder="a4:bb:6d:11:22:33"
          />
        </div>

        <div className="field">
          <label htmlFor="wake-segment">
            {needsSegment ? t("siteServers.wake.segmentRequired") : t("siteServers.wake.segmentOptional")}
          </label>
          <select
            id="wake-segment"
            className={inputClass}
            value={segment}
            onChange={(event) => setSegment(event.target.value)}
          >
            <option value="">
              {needsSegment ? t("siteServers.wake.selectSegment") : t("siteServers.wake.onlySegment", { segment: server.segments[0] ?? "" })}
            </option>
            {server.segments.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* `Button`, not a bare element: a wake changes the fleet, so it wears
          the primary treatment every other commit in the console wears. It
          cannot be `Btn`, which has no way to express `disabled` and so cannot
          say "this request is already in flight". */}
      <div className="form-actions">
        <Button type="submit" disabled={isPending}>
          {t("siteServers.wake.send")}
        </Button>
      </div>

      {error ? (
        <p role="alert" className="form-error">
          {error.message ?? t("siteServers.wake.failed")}
        </p>
      ) : null}
      {result ? (
        <div className={NOTICE_CLASS[WAKE_KIND[state]]} style={{ marginTop: 14 }}>
          <Icon name={KIND_ICON[WAKE_KIND[state]]} size={16} />
          <p data-testid="wake-result" data-wake-state={state} role="status">
          {state === "queued"
            ? t("siteServers.wake.queued", {
                name: server.name,
                broadcast: result.broadcast,
                mac: result.target_mac,
                date: formatDateTime(result.expires_at, zone),
              })
            : null}
          {state === "dispatched"
            ? t("siteServers.wake.dispatched", { name: server.name, broadcast: result.broadcast, mac: result.target_mac })
            : null}
          {state === "reported"
            ? t("siteServers.wake.reported", {
                name: server.name,
                broadcast: result.broadcast,
                mac: result.target_mac,
                at: sentAt ? ` ${t("siteServers.wake.at", { time: sentAt })}` : "",
              })
            : null}
          {state === "failed"
            ? t("siteServers.wake.failedState", { name: server.name, broadcast: result.broadcast, mac: result.target_mac })
            : null}
          {state === "expired"
            ? t("siteServers.wake.expired", { date: formatDateTime(result.expires_at, zone), name: server.name })
            : null}
          </p>
        </div>
      ) : null}
    </form>
  );
}
