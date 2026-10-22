/**
 * Remote sessions, on the v3 kit: the design's "Remote control" screen
 * (`P['remote']` in docs/design/itms-prototype-v3.html) is a tile row over
 * one bare panel of rows led by the device's glyph, each carrying its
 * operator, when it ran and a Play action.
 *
 * WHAT THE DESIGN ASKS FOR THAT THE API DOES NOT SERVE. The design's row
 * names its operator ("Adaeze Okonkwo") and its "Sessions 7d" tile counts a
 * window. `RemoteSession` carries `technician_id` (a user id, no name join)
 * and nothing here scopes sessions to a week without inventing a boundary,
 * so both are omitted rather than guessed: no `Person` is drawn for a UUID.
 * The tiles below are computed from what IS fetched:
 *
 * - "Online devices" -> `listDevices()`, already fetched for the hostname
 *   join, filtered on `agent_status === "online"` (mirrors DevicesPage).
 * - "Sessions" / "Active now" / "Recorded" -> counted from the sessions page
 *   actually returned.
 */

import { useQuery } from "@tanstack/react-query";
import { useCallback, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { NoValue } from "../../components/emptystate";
import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import { humaniseEnum } from "../../lib/labels";
import type { Me, RemoteSession } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { Icon } from "../../shell/icons";
import { OsIcon, type Kind } from "../../shell/viz";
import { variantKind } from "../jobs/jobState";
import { consentModeLabel, formatDuration, remoteStateVariant } from "./sessionState";
import { RemoteReadiness } from "./RemoteReadiness";
import { StartSessionDialog } from "./StartSessionDialog";
import { Btn, FilterBar, PageHead, Panel, QueryState, SearchField, StatCard, StatsGrid, StatusPill, Tabs } from "../../shell/components";

/**
 * THE FILTER LIVES IN THE URL, and it lives there because the tiles link to it.
 *
 * "All tiles and all numbers need to be clickable and direct the user to the
 * specific menus" — a tile counting the sessions that have not ended has
 * nowhere honest to point unless "the sessions that have not ended" is a URL.
 * The param names are `state` and `q`, the exact pair DevicesPage already
 * reads (`useSearchParams` there), so a filtered remote view is pasteable into
 * an incident channel the same way a filtered device view is.
 */
const TABS = [
  { key: "all", label: "remote.sessions.tabAll" },
  // Not "Active" and "Recorded" bare: those are also a session STATE badge and
  // a recording badge inside the table, and a strip whose labels are the same
  // words as the cells beneath it is ambiguous to read and to test.
  { key: "active", label: "remote.sessions.tabActive" },
  { key: "recorded", label: "remote.sessions.tabRecorded" },
] as const satisfies readonly { key: string; label: MessageKey }[];

/** The row's edge: a session somebody is on right now is the running blue. */
function sessionKind(session: RemoteSession): Kind {
  // A session that is still coming up is work in flight, which v3 draws as
  // `run` (the progress glyph), not as `info`; the variant table has no word
  // for that, so it is said here, as `jobStatusKind` says it for jobs.
  if (session.state === "connecting") return "run";
  return variantKind(remoteStateVariant(session.state));
}

export function SessionsPage() {
  const t = useT();
  const api = useApi();
  const [startOpen, setStartOpen] = useState(false);

  const [params, setParams] = useSearchParams();
  const tab = params.get("state") ?? "all";
  const search = params.get("q") ?? "";

  function update(patch: Record<string, string>, replace = false) {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(patch)) {
      // An empty value is ABSENT, not `?q=` — a URL carrying an empty filter
      // reads as a filter set to nothing (DevicesPage's note, same rule).
      if (value === "") next.delete(key);
      else next.set(key, value);
    }
    setParams(next, { replace });
  }

  const setTab = (key: string) => update({ state: key === "all" ? "" : key });
  // Replaces rather than pushes: one history entry per keystroke would make
  // Back useless for the person trying to undo the filter.
  const setSearch = (value: string) => update({ q: value }, true);

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canControl = permissions.includes("remote.control");
  const canReadRecordings = permissions.includes("remote.recordings.read");

  // Sessions are TanStack Query state, not a live-event stream: the list is
  // fetched fresh on mount and refreshed by invalidating ["remote-sessions"]
  // after the start/stop mutations succeed (see StartSessionDialog and
  // ScreenViewer).
  const sessions = useQuery({
    queryKey: ["remote-sessions"],
    queryFn: () => api.listRemoteSessions(),
  });

  const devices = useQuery({ queryKey: ["devices"], queryFn: () => api.listDevices() });
  const deviceById = useMemo(
    () => new Map((devices.data?.items ?? []).map((device) => [device.id, device])),
    [devices.data],
  );
  const hostnameFor = useCallback(
    (deviceId: string): string => deviceById.get(deviceId)?.hostname ?? deviceId,
    [deviceById],
  );
  const osFor = useCallback(
    (deviceId: string): string => deviceById.get(deviceId)?.os_family ?? "",
    [deviceById],
  );
  const onlineDevices = (devices.data?.items ?? []).filter(
    (device) => device.agent_status === "online",
  ).length;

  const items = useMemo<RemoteSession[]>(() => sessions.data?.items ?? [], [sessions.data]);
  const activeCount = items.filter((session) => session.state !== "ended").length;
  const recordedCount = items.filter((session) => session.has_recording).length;

  const searched = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (needle === "") return items;
    return items.filter((session) =>
      [hostnameFor(session.device_id), session.protocol, session.state]
        .join(" ")
        .toLowerCase()
        .includes(needle),
    );
  }, [items, search, hostnameFor]);

  const matchesTab = (session: RemoteSession, key: string): boolean =>
    key === "active" ? session.state !== "ended" : key === "recorded" ? session.has_recording : true;

  const filtered = useMemo(
    () => searched.filter((session) => matchesTab(session, tab)),
    [searched, tab],
  );

  // Counted over the SEARCHED rows, not the whole page: a strip whose counts
  // contradict the table under it reads as a table that failed to render rows
  // it has (the defect DevicesPage documents against its own tab counts).
  const tabCounts: Record<string, number> = {
    all: searched.length,
    active: searched.filter((session) => matchesTab(session, "active")).length,
    recorded: searched.filter((session) => matchesTab(session, "recorded")).length,
  };

  return (
    <>
      <PageHead
        title={t("remote.sessions.title")}
        description={t("remote.sessions.description")}
        actions={
          canControl ? (
            <Btn variant="primary" icon="user-screen" label={t("remote.sessions.start")} onClick={() => setStartOpen(true)} />
          ) : undefined
        }
      />

      <RemoteReadiness />

      {/* AUDIT [LOW], /remote → stat cards: "'Online devices' is a live fleet
          condition, 'Sessions' is an all-time total, and 'Active now' is a
          point-in-time count — three different temporal meanings rendered
          identically, so the row cannot be read at a glance." Each card now
          says which it is, and the two counted from the fetched page say THAT
          rather than implying they cover the whole history. */}
      {/* EVERY TILE OPENS THE ROWS IT COUNTED. "Online devices" is the only
          one whose rows are not on this page — it is a fleet condition, and it
          hands over `?state=online`, the tab /devices already reads. The other
          three are this page's own tab strip, so the tile and the tab cannot
          drift apart: the tile links to the tab.

          "Sessions" goes to the unfiltered list rather than to a filter of its
          own, because the all-time total IS the unfiltered list. Arriving from
          `?state=active` that link is what clears the filter; it is the
          weakest link in the row, and inventing a `?state=all-time` this page
          does not read would have been worse.

          Only "Active now" is `urgent`: a session that has not ended is
          somebody on an employee's machine right now, and it is the one number
          here a person may have to end. */}
      <StatsGrid columns={4}>
        <StatCard
          label={t("remote.sessions.onlineDevices")}
          value={String(onlineDevices)}
          icon="devices"
          to="/devices?state=online"
          tone="green"
          foot={t("remote.sessions.onlineFoot")}
          zeroLabel={t("remote.sessions.onlineZero")}
        />
        <StatCard
          label={t("remote.sessions.sessions")}
          value={String(sessions.data?.total ?? items.length)}
          icon="history"
          to="/remote"
          tone="neutral"
          foot={t("remote.sessions.sessionsFoot")}
          zeroLabel={t("remote.sessions.sessionsZero")}
        />
        <StatCard
          label={t("remote.sessions.activeNow")}
          value={String(activeCount)}
          icon="user-screen"
          to="/remote?state=active"
          tone="violet"
          urgent
          foot={t("remote.sessions.activeFoot")}
          /* WHICH zero: the list loaded and nothing in it is live. Not "no
             session has ever run" — that is the tile to the left. */
          zeroLabel={t("remote.sessions.activeZero")}
        />
        <StatCard
          label={t("remote.sessions.recorded")}
          value={String(recordedCount)}
          icon="archive"
          to="/remote?state=recorded"
          tone="blue"
          foot={t("remote.sessions.recordedFoot")}
          zeroLabel={t("remote.sessions.recordedZero")}
        />
      </StatsGrid>

      <h2 className="sr-only">{t("remote.sessions.listHeading")}</h2>
      <Tabs
        items={TABS.map((item) => ({ ...item, label: t(item.label), count: tabCounts[item.key] }))}
        active={tab}
        onSelect={setTab}
        panelId="remote-tabpanel"
      />

      <Panel bare>
        <div
          id="remote-tabpanel"
          role="tabpanel"
          aria-label={t(TABS.find((item) => item.key === tab)?.label ?? "remote.sessions.tabAll")}
          tabIndex={-1}
        >
        <FilterBar>
          <SearchField
            placeholder={t("remote.sessions.searchPlaceholder")}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            aria-label={t("remote.sessions.searchLabel")}
          />
          {/* AND NO TRAILING SPACER (design review I5). `.filter-spacer` is
              `flex: 1`, and so is the kit's search wrapper, so a spacer with
              NOTHING AFTER IT does not push anything to the right -- it takes
              half the row from the field beside it. Measured at 1440 the box
              stopped at ~675px inside a 1140px card with bare panel to its
              right. /licenses and /subscriptions run their search the full
              width of the bar; /service-desk pairs it with chips; the only
              honest use of a spacer is between a growing field and something
              it has to push, which /licenses has (a result count) and this
              row does not. AuditPage's filter bar carries the same note for
              the same reason. */}
        </FilterBar>

        <QueryState
          loading={sessions.isLoading}
          error={sessions.error}
          empty={filtered.length === 0}
          emptyLabel={items.length === 0 ? t("remote.sessions.emptyNone") : t("remote.sessions.emptyFiltered")}
          emptyIcon="user-screen"
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("remote.sessions.colDevice")}</Th>
                <Th>{t("remote.sessions.colConsent")}</Th>
                <Th>{t("remote.sessions.colState")}</Th>
                <Th>{t("remote.sessions.colDuration")}</Th>
                <Th>{t("remote.sessions.colRecording")}</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {filtered.map((session) => (
                <tr key={session.id}>
                  {/* v3's row: the device's OS glyph as the lead, the
                      hostname over the protocol. `OsIcon` names an unknown
                      platform rather than painting a Windows glyph on it. */}
                  <Td lead={<OsIcon os={osFor(session.device_id)} />} gutter={sessionKind(session)}>
                    <div className="cell-title" style={{ whiteSpace: "nowrap" }}>
                      {hostnameFor(session.device_id)}
                    </div>
                    <div className="cell-sub mono">{session.protocol.toUpperCase()}</div>
                  </Td>
                  <Td>{consentModeLabel(session.consent_mode)}</Td>
                  <Td>
                    {/* `awaiting_consent` reached the screen as
                        `awaiting_consent`: the product-wide raw-enum defect
                        (lib/labels.ts) in the one column that says whether
                        somebody is currently on an employee's machine. */}
                    <StatusPill
                      value={session.state}
                      kind={sessionKind(session)}
                      label={humaniseEnum(session.state, "RemoteSessionState")}
                    />
                  </Td>
                  <Td>
                    <span className="mono">{formatDuration(session.started_at, session.ended_at)}</span>
                  </Td>
                  <Td>
                    {session.has_recording ? (
                      <StatusPill value="recorded" kind="info" label={t("remote.sessions.pillRecorded")} />
                    ) : (
                        <NoValue label={t("remote.sessions.notRecorded")} />
                    )}
                  </Td>
                  <Td>
                    {/* REAL LINKS, not buttons inside anchors: an interactive
                        control nested in another is an axe
                        `nested-interactive` failure, and a link styled as the
                        design's button is what v3's "Play" is.

                        BOTH TAKE THE BORDERED BUTTON, and the second one
                        used to take `ghost`. `ghost` paints as bare accent
                        text -- the shape a link has -- so this one cell drew
                        one bordered control and one bare one side by side,
                        which is the mixed-treatment half of design review M2.
                        The console's row-control convention is already
                        written down at `subscriptions/SubscriptionDetail.tsx`:
                        a row control is a control, drawn with an edge,
                        whether the element under it is a `<button>` or a
                        `<Link>`. Navigation vs mutation is carried by the
                        ELEMENT, which stays honest either way; it is not a
                        second visual language. */}
                    <div style={{ display: "flex", gap: 6 }}>
                      {canControl && session.state !== "ended" && (
                        <Link className="btn small" to={`/remote/${session.id}`}>
                          <Icon name="user-screen" size={14} />
                          <span>{t("remote.sessions.open")}</span>
                        </Link>
                      )}
                      {canReadRecordings && session.has_recording && (
                        <Link className="btn small" to={`/remote/${session.id}/recording`}>
                          <Icon name="player-play" size={14} />
                          <span>{t("remote.sessions.playback")}</span>
                        </Link>
                      )}
                    </div>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </QueryState>
        </div>
      </Panel>

      <StartSessionDialog open={startOpen} onClose={() => setStartOpen(false)} />
    </>
  );
}
