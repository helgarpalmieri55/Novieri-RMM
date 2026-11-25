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
import { useCallback, useMemo } from "react";
import { Link } from "react-router-dom";
import { NoValue } from "../../components/emptystate";
import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { NO_DATE } from "../../lib/datetime";
import type { MessageKey } from "../../i18n/core";
import { humaniseEnum } from "../../lib/labels";
import type { Me, RemoteSession } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { Icon } from "../../shell/icons";
import { OsIcon } from "../../shell/viz";
import { consentModeLabel, formatDuration, sessionKind } from "./sessionState";
import { RemoteReadiness } from "./RemoteReadiness";
import { StartSessionDialog } from "./StartSessionDialog";
import {
  Btn,
  ClientFilter,
  FilterChips,
  ListEmpty,
  ListToolbar,
  PageHead,
  Panel,
  QueryState,
  SearchField,
  ShowingCount,
  StatCard,
  StatsGrid,
  StatusPill,
  useNarrowedApi,
  useNewParam,
  useUrlFilters,
  withClient,
} from "../../shell/components";

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


export function SessionsPage() {
  const t = useT();
  const api = useApi();
  // New → Remote session (decision D8): `?new=session` opens the dialog.
  const started = useNewParam("session");

  // THE LIST KIT (redesign plan, O3 3.6): `state` is the strip that filtered
  // the list, now chips on the same parameter; the search replaces its
  // history entry (the kit's `q`); `client` is the Client chip's.
  const filters = useUrlFilters(["q", "state", "client"] as const);
  const tab = filters.values.state === "" ? "all" : filters.values.state;
  const search = filters.values.q;
  const setTab = (key: string) => filters.set("state", key === "all" ? "" : key);
  const setSearch = (value: string) => filters.set("q", value);

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canControl = permissions.includes("remote.control");
  const canReadRecordings = permissions.includes("remote.recordings.read");

  // Sessions are TanStack Query state, not a live-event stream: the list is
  // fetched fresh on mount and refreshed by invalidating ["remote-sessions"]
  // after the start/stop mutations succeed (see StartSessionDialog and
  // ScreenViewer).
  //
  // UNDER THE CLIENT CHIP (decisions D6, D20) the sessions and the devices
  // that name them are that client's: the same calls, read through
  // `useNarrowedApi` and kept under the client's key (today's key without a
  // chip). `["remote-sessions"]` still reaches every copy. The client goes in
  // as `{ client }`, never bare: `["remote-sessions", id]` is one session
  // (useSessionDevice) and `["devices", id]` one device.
  const { api: readApi, client, ready } = useNarrowedApi(filters.values.client);
  // A TILE COUNTED UNDER THE CHIP OPENS THAT CLIENT'S ROWS (general ruling 2):
  // every tile below counts the chip's client, so each links with `withClient`.
  const sessions = useQuery({
    queryKey: client === null ? ["remote-sessions"] : ["remote-sessions", { client }],
    queryFn: () => readApi.listRemoteSessions(),
    enabled: ready,
  });

  const devices = useQuery({
    queryKey: client === null ? ["devices"] : ["devices", { client }],
    queryFn: () => readApi.listDevices(),
    enabled: ready,
  });
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


  return (
    <>
      <PageHead
        title={t("remote.sessions.title")}
        description={t("remote.sessions.description")}
        actions={
          canControl ? (
            <Btn variant="primary" icon="user-screen" label={t("remote.sessions.start")} onClick={started.start} />
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
      {/* NO NUMBER AND NO CLAIM BEFORE THE READ (clarification 10a): until the
          read a tile counts has answered -- under the Client chip that
          includes the wait for `/me` -- it reads "—" with no line under it,
          as Alerts' tiles do. */}
      <StatsGrid columns={4}>
        <StatCard
          label={t("remote.sessions.onlineDevices")}
          value={devices.isPending ? NO_DATE : String(onlineDevices)}
          icon="devices"
          to={withClient("/devices?state=online", client)}
          tone="green"
          foot={devices.isPending ? undefined : t("remote.sessions.onlineFoot")}
          zeroLabel={t("remote.sessions.onlineZero")}
        />
        <StatCard
          label={t("remote.sessions.sessions")}
          value={sessions.isPending ? NO_DATE : String(sessions.data?.total ?? items.length)}
          icon="history"
          to={withClient("/remote", client)}
          tone="neutral"
          foot={sessions.isPending ? undefined : t("remote.sessions.sessionsFoot")}
          zeroLabel={t("remote.sessions.sessionsZero")}
        />
        <StatCard
          label={t("remote.sessions.activeNow")}
          value={sessions.isPending ? NO_DATE : String(activeCount)}
          icon="user-screen"
          to={withClient("/remote?state=active", client)}
          tone="violet"
          // Not loud before the read: "—" is no session on anyone's machine.
          urgent={!sessions.isPending}
          foot={sessions.isPending ? undefined : t("remote.sessions.activeFoot")}
          /* WHICH zero: the list loaded and nothing in it is live. Not "no
             session has ever run" — that is the tile to the left. */
          zeroLabel={t("remote.sessions.activeZero")}
        />
        <StatCard
          label={t("remote.sessions.recorded")}
          value={sessions.isPending ? NO_DATE : String(recordedCount)}
          icon="archive"
          to={withClient("/remote?state=recorded", client)}
          tone="blue"
          foot={sessions.isPending ? undefined : t("remote.sessions.recordedFoot")}
          zeroLabel={t("remote.sessions.recordedZero")}
        />
      </StatsGrid>

      <h2 className="sr-only">{t("remote.sessions.listHeading")}</h2>
      <Panel bare>
        <ListToolbar
          search={
            <SearchField
              placeholder={t("remote.sessions.searchPlaceholder")}
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              aria-label={t("remote.sessions.searchLabel")}
            />
          }
          chips={
            <>
              {/* THE STRIP THAT FILTERED THE LIST IS CHIPS (redesign plan,
                  Receta L point 5), on the same `?state=` the tiles link to.
                  No numbers: the server sends a total and no count by state,
                  and one counted over the rows in hand is the browser's. */}
              <FilterChips
                label={t("remote.sessions.stateLabel")}
                options={TABS.map((item) => ({ value: item.key === "all" ? "" : item.key, label: t(item.label) }))}
                value={tab === "all" ? "" : tab}
                onChange={(value) => setTab(value === "" ? "all" : value)}
              />
              <ClientFilter filters={filters} />
            </>
          }
          end={sessions.data ? <ShowingCount shown={filtered.length} total={items.length} /> : null}
        />

        <QueryState loading={sessions.isPending} error={sessions.error} empty={false} emptyLabel="">
          {filtered.length === 0 ? (
            <ListEmpty
              filtered={filters.active}
              onClear={filters.clear}
              icon="user-screen"
              nothingYet={t("remote.sessions.emptyNone")}
              action={
                canControl ? (
                  <Btn small icon="user-screen" label={t("remote.sessions.start")} onClick={started.start} />
                ) : undefined
              }
            />
          ) : (
          <Table label={t("remote.sessions.title")}>
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
          )}
        </QueryState>
      </Panel>

      {/* MOUNTED ONLY WHILE OPEN (general ruling 4): however it opens -- the
          button, a link, Back or Forward -- the form starts empty, and nothing
          from the last one (a refusal included) is carried over. */}
      {started.open && canControl ? <StartSessionDialog open guardNew onClose={started.close} /> : null}
    </>
  );
}
