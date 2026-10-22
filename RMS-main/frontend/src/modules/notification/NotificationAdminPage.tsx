import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo } from "react";
import { useLocation, useSearchParams } from "react-router-dom";
import { useT } from "../../i18n/context";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { AllowlistPanel } from "./AllowlistPanel";
import { ChannelsPanel } from "./ChannelsPanel";
import { DeliveriesPanel } from "./DeliveriesPanel";
import {
  useNotificationAllowlist,
  useNotificationChannels,
  useNotificationDeadCount,
  useNotificationRules,
} from "./api";
import { NotificationPrerequisites } from "./NotificationPrerequisites";
import { QueueMembersPanel } from "./QueueMembersPanel";
import { RulesPanel } from "./RulesPanel";
import type { ChannelKind } from "./types";
import { PageHead, StatCard, StatsGrid } from "../../shell/components";
import { intlLocale } from "../../lib/datetime";

// Spec §11: "One admin surface under the existing module nav, gated on
// notification.read with mutations gated on notification.admin" -- both
// permissions are granted only to super-admin/org-admin (spec §8), so a
// technician sees nothing here at all (App.tsx's Shell nav omits the link
// entirely when `notification.read` is absent, mirroring every other
// module's nav-gating precedent).
//
// The UI never gates security (invariant #6): `canRead`/`canAdmin` below
// only decide what renders. The server's `require_permission` on every route
// is the actual authority, and rejects an unauthorized call regardless of
// what this page shows.
//
// WHAT THE DESIGN ASKS FOR THAT NO ENDPOINT SERVES (UI port spec §7). The
// prototype's stat row reads "Delivered today 1,842", "Delivery rate 99.4%",
// "Retries 8", "Bounces 2" -- none of those are real. `GET
// /notifications/deliveries` supports a `status` filter and returns a page
// `total`, which is how the existing dead-letter count is already computed
// (`useNotificationDeadCount`, spec §7), but there is no daily bucket and no
// rolling delivery-rate aggregate anywhere in the backend -- producing either
// would mean fetching every delivery ever recorded and counting client-side,
// which is exactly the client-side aggregation this port's other modules
// (reporting, §11) forbid outright. The cards below are therefore full
// counts of configuration and outstanding failures -- the numbers this page
// actually has -- not the design's daily activity figures.
/**
 * The stat row's two halves: where a tile points, and making it land.
 *
 * WHERE IT POINTS. `/notifications` is this module's only route in App.tsx, so every
 * tile above links to a view OF this page — which is honest, because the rows
 * behind each number are on this screen. `link()` PRESERVES whatever is
 * already in the query string and only overwrites the keys it is given: a tile
 * is a place to look, not a reset button, and a tile that rebuilt the URL from
 * scratch would silently discard the reader's current filter or selection on
 * the way past.
 *
 * MAKING IT LAND. React Router pushes a fragment and stops there — it does not
 * scroll, and `App.tsx` wires no scroll restoration. Without the effect below,
 * a fragment tile navigates and moves nothing on screen, which is the dead
 * tile the operator asked us to remove, wearing an href.
 */
function useStatLinks(route: string) {
  const { hash } = useLocation();
  const [searchParams] = useSearchParams();

  useEffect(() => {
    if (!hash) return;
    const target = document.getElementById(decodeURIComponent(hash.slice(1)));
    // Guarded: jsdom does no layout and defines no `scrollIntoView`, and a
    // TypeError here would take the whole page down.
    if (target && typeof target.scrollIntoView === "function") {
      target.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [hash]);

  return useCallback(
    (fragment: string, params: Readonly<Record<string, string>> = {}) => {
      const next = new URLSearchParams(searchParams);
      for (const [key, value] of Object.entries(params)) next.set(key, value);
      const query = next.toString();
      return `${route}${query ? `?${query}` : ""}#${fragment}`;
    },
    [route, searchParams],
  );
}

export function NotificationAdminPage() {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canAdmin = permissions.includes("notification.admin");

  // Each of these is the SAME query a panel below already issues (identical
  // query key), so TanStack Query serves this from the same cache entry
  // rather than doubling the request count.
  const channels = useNotificationChannels();
  const rules = useNotificationRules();
  const allowlist = useNotificationAllowlist();
  const deadCount = useNotificationDeadCount();

  const link = useStatLinks("/notifications");

  // A PENDING QUERY IS NOT A ZERO. `channels.data?.length ?? 0` renders "0"
  // for the whole of the first paint and for every failed load — "nothing is
  // configured" and "we have not been told" printed identically, which is the
  // exact defect `zeroLabel` exists to close one level further down. Undefined
  // data gets an em dash; a zero on screen is then always a measured zero, and
  // its `zeroLabel` always true.
  const count = (n: number | undefined): string => (n === undefined ? "—" : String(n));

  // WHAT THE FIGURE IS MADE OF. v3's tile for this route draws the count and
  // then its composition ("3 · Email · Teams · webhook"); the console drew a
  // bare figure and reserved the sub-line for a `zeroLabel` that, by
  // definition, never shows at a non-zero count -- so three of these four
  // tiles had a permanently empty second line. The two breakdowns below are
  // read off rows this page already holds (no extra request), and each is
  // `undefined` at zero so the zero-state label keeps the line to itself.
  //
  // The memos here hold COUNTS and codes, never sentences: a memoised sentence
  // would keep the language the page was in, so the words are put around the
  // counts below, at render.
  const channelKinds = useMemo(() => {
    const items = channels.data;
    if (!items || items.length === 0) return undefined;
    const byKind = new Map<ChannelKind, number>();
    for (const channel of items) byKind.set(channel.kind, (byKind.get(channel.kind) ?? 0) + 1);
    return [...byKind.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], intlLocale()));
  }, [channels.data]);
  const channelBreakdown = channelKinds
    ?.map(([kind, n]) =>
      t(kind === "webhook" ? "notification.admin.webhookCount" : "notification.admin.emailCount", { count: n }),
    )
    .join(" · ");

  // Rules break down by the one thing that changes what a rule DOES: whether
  // it is routing anything. "4" with three of them paused is a different
  // system from "4" with none paused, and the pause state is on the same rows
  // the panel below renders.
  const ruleCounts = useMemo(() => {
    const items = rules.data;
    if (!items || items.length === 0) return undefined;
    const active = items.filter((rule) => rule.active).length;
    return { active, paused: items.length - active };
  }, [rules.data]);
  const ruleBreakdown = !ruleCounts
    ? undefined
    : ruleCounts.paused === 0
      ? t("notification.admin.allRouting")
      : t("notification.admin.routingPaused", { active: ruleCounts.active, count: ruleCounts.paused });

  // THE ROW WAS RAGGED. Channels and Rules carried a breakdown and the two
  // beside them carried none, so half the row sat with an empty band under
  // the figure. Round 2 left them empty for want of a second dimension in
  // their data; these are the two that are actually there. Neither invents a
  // count: the allowlist one is read off the same rows the panel below
  // renders, and the dead-letter one states what `dead` MEANS (delivery.py
  // retries, then stops), which is the fact an operator needs beside that
  // number and cannot get from the figure alone.
  const allowlistHosts = useMemo(() => {
    const items = allowlist.data;
    if (!items || items.length === 0) return undefined;
    // An entry is a host AND a port (migration 0026), so several entries can
    // cover one host on different ports -- "4" over three hosts is a
    // different configuration from "4" over four.
    return new Set(items.map((entry) => entry.host)).size;
  }, [allowlist.data]);
  const allowlistBreakdown =
    allowlistHosts === undefined ? undefined : t("notification.admin.acrossHosts", { count: allowlistHosts });

  const deadBreakdown =
    deadCount.data === undefined || deadCount.data.total === 0
      ? undefined
      : t("notification.admin.deadFoot");

  return (
    <div>
      <PageHead
        title={t("notification.admin.title")}
        description={t("notification.admin.description")}
      />

      <NotificationPrerequisites />

      {/* COLOUR IS CATEGORY, WEIGHT IS URGENCY. Three of these four are
          configuration counts and share one hue; the fourth is the only
          number on this page that means something is wrong right now, and it
          is the only one carrying weight. The row this replaces gave the
          dead-letter count the same emphasis as "Rules", which is how a
          delivery pipeline that has stopped delivering hides in plain sight
          (spec §7, requirement #4). */}
      <StatsGrid columns={4}>
        <StatCard
          label={t("notification.admin.channelsLabel")}
          value={count(channels.data?.length)}
          icon="mail"
          to={link("channels")}
          tone="blue"
          foot={channelBreakdown}
          zeroLabel={t("notification.admin.channelsZero")}
        />
        <StatCard
          label={t("notification.admin.rulesLabel")}
          value={count(rules.data?.length)}
          icon="bell"
          to={link("rules")}
          tone="blue"
          foot={ruleBreakdown}
          zeroLabel={t("notification.admin.rulesZero")}
        />
        <StatCard
          label={t("notification.admin.allowlistLabel")}
          value={count(allowlist.data?.length)}
          icon="shield-lock"
          to={link("allowlist")}
          tone="primary"
          foot={allowlistBreakdown}
          /* NOT an alarm at zero, and the label says why: the egress guard
             denies internal and cloud-metadata addresses by default, so an
             empty allowlist is the safe state, not an unconfigured one. */
          zeroLabel={t("notification.admin.allowlistZero")}
        />
        <StatCard
          label={t("notification.admin.deadLabel")}
          value={count(deadCount.data?.total)}
          /* THE GLYPH IS THE KIND, NOT THE HUE. This tile is drawn crit
             (`tone="red" urgent`), and the plan's severity table pairs crit
             with `circle-x`; `alert-triangle` is warn's glyph. Carrying it
             here made a crit tile and a warn tile differ by colour alone,
             which is the one thing the severity icon exists to prevent. */
          icon="circle-x"
          /* The one destination on this page that is a genuinely different
             view: the deliveries log narrowed to the failures this number
             counts. `?status=dead` is the param `DeliveriesPanel` reads. */
          to={link("deliveries", { status: "dead" })}
          tone="red"
          urgent
          foot={deadBreakdown}
          zeroLabel={t("notification.admin.deadZero")}
        />
      </StatsGrid>

      {/* One list per panel, full width, the way the design draws its rules
          panel: every row here names a destination or a person, and a
          half-width column truncates exactly the part that matters. */}
      <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}>
        <RulesPanel canAdmin={canAdmin} />
        <ChannelsPanel canAdmin={canAdmin} />
        <QueueMembersPanel canAdmin={canAdmin} />
        <AllowlistPanel canAdmin={canAdmin} />
        {/* The profile zone (`/me`.timezone, cycle 2) reaches the one panel
            that renders a time. This page already holds `me` for its
            permission gate, so nothing new is fetched. */}
        <DeliveriesPanel zone={me.data?.timezone} />
      </div>
    </div>
  );
}
