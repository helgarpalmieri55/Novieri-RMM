// The site-server list PAGE: the container that was missing.
//
// WHY THIS FILE EXISTS AT ALL. `SiteServerList` and `SiteServerDetail` are
// presentational -- they take data as props -- and every hook they need has
// existed in `api.ts` since the module shipped. What never existed was a
// container joining the two, so nothing imported them and `App.tsx` had no
// route to point at. The result: seven files and ~1,300 lines of a finished
// feature that no operator could reach, in a module whose backend routes,
// tests and permission codes were all in place.
//
// It went unnoticed because every check this project runs was satisfied. The
// module has a folder, its components have unit tests, `tsc` is clean, and the
// backend suite is green. "Reachable from the app shell" was asserted by
// nothing, which is why `App.routing.test.tsx` now asserts it.
//
// ON THE v3 KIT (`P['site-servers']` in docs/design/itms-prototype-v3.html):
// the head with its one primary action, the tiles the console keeps over the
// design's list (each is a link into the filter below), and one bare panel of
// rows. `SiteServerList` renders the three states this port cares about (a
// loading placeholder, a `role="alert"` failure, and a distinct empty state),
// so it is wrapped, not rewritten: every `data-testid` `siteServers.test.tsx`
// depends on is untouched.
//
// WHAT THE DESIGN ASKS FOR THAT THE API DOES NOT SERVE. The prototype's rows
// carry "41 sweeps" as activity and an "Uncovered" row for a segment with no
// site server. Neither is served: there is no per-server sweep aggregate (a
// sweep run belongs to a subnet, in IPAM) and the list is of servers, not of
// segments. Both are OMITTED rather than faked.

import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import { useT } from "../../i18n/context";

import {
  usePromotableDevices,
  usePromoteSiteServerMutation,
  useSiteServerPermissions,
  useSiteServers,
} from "./api";
import { SiteServerList } from "./SiteServerList";
import { PromoteDialog } from "./PromoteDialog";
import { SiteServerPrerequisites } from "./SiteServerPrerequisites";
import { EmptyState } from "../../components/emptystate";
import { Dialog } from "../../components/ui";
import { Btn, FilterBar, PageHead, Panel, StatCard, StatsGrid } from "../../shell/components";

export function SiteServersPage() {
  const t = useT();
  const { manage } = useSiteServerPermissions();
  const { data, isLoading, error } = useSiteServers();
  const [promoteOpen, setPromoteOpen] = useState(false);
  // THE FILTER IS IN THE URL because the "Enabled" tile links into it. A
  // demoted-but-not-deleted server is the distinction this whole list exists
  // to make (see SiteServerList), so "Enabled 1 of 2" is a number an operator
  // will want to click, and it has to land on the one row it counted.
  const [params, setParams] = useSearchParams();
  const stateFilter = params.get("state") ?? "";

  const devices = usePromotableDevices();
  const promote = usePromoteSiteServerMutation();

  const servers = data ?? [];
  const enabledCount = servers.filter((server) => server.enabled).length;
  const segmentCount = new Set(servers.flatMap((server) => server.segments)).size;
  const visible =
    stateFilter === "enabled"
      ? servers.filter((server) => server.enabled)
      : stateFilter === "disabled"
        ? servers.filter((server) => !server.enabled)
        : servers;
  // "No site servers. Promote a device to add one." is the WRONG sentence for
  // a filter that matched none of several: it says the estate is empty when
  // it is not. `SiteServerList` cannot tell the two apart (it takes an array),
  // so the page that owns the filter answers for it.
  const filteredToNothing = stateFilter !== "" && servers.length > 0 && visible.length === 0;

  return (
    <section>
      <PageHead
        title={t("siteServers.page.title")}
        description={t("siteServers.page.description")}
        actions={
          // PERMISSION-GATED RENDERING ONLY, and `api.ts` says why in full: this
          // gates nothing. Every route carries its own `require_permission` and
          // the server re-checks on every request (invariant #6). Hiding the
          // button saves an operator a 403; it prevents nothing.
          manage ? (
            <Btn
              label={t("siteServers.page.promote")}
              icon="plus"
              variant="primary"
              onClick={() => setPromoteOpen(true)}
            />
          ) : undefined
        }
      />

      <SiteServerPrerequisites />

      {/* Nothing here is `urgent`, and that is a statement rather than an
          omission: a site server is an optimisation, and none of these three
          numbers has a deadline attached to it.

          "Served segments" goes to the unfiltered list because that is where
          the segments are: every row carries its own, and no other screen in
          the product enumerates them. */}
      <StatsGrid columns={3}>
        <StatCard
          label={t("siteServers.page.registered")}
          value={String(servers.length)}
          icon="server"
          to="/site-servers"
          tone="neutral"
          zeroLabel={t("siteServers.page.registeredZero")}
          foot={<span>{t("siteServers.page.registeredFoot")}</span>}
        />
        <StatCard
          label={t("siteServers.page.enabled")}
          value={String(enabledCount)}
          icon="check"
          to="/site-servers?state=enabled"
          tone="green"
          zeroLabel={
            servers.length === 0
              ? t("siteServers.page.enabledZeroNone")
              : t("siteServers.page.enabledZeroAll")
          }
          /* THE DENOMINATOR THE COMMENT ABOVE ALREADY ASSUMED. "Enabled 1"
             beside "Registered servers 2" is two numbers a reader has to
             subtract; the sentence this tile was written around is
             "Enabled 1 of 2", and the figure cannot carry it (`value` is the
             KPI). Absent while the list is still in flight: "of 0 registered"
             is a claim about an estate nothing has fetched. */
          foot={
            servers.length > 0 ? (
              <span>{t("siteServers.page.enabledFoot", { count: servers.length })}</span>
            ) : undefined
          }
        />
        <StatCard
          label={t("siteServers.page.segments")}
          value={String(segmentCount)}
          icon="network"
          to="/site-servers"
          tone="blue"
          zeroLabel={t("siteServers.page.segmentsZero")}
          /* WHAT IT DOES NOT COUNT, because the number invites the opposite
             reading. Nothing in this release enumerates the segments that have
             no site server, so the tile says so rather than letting "Served
             segments 3" be read as coverage. */
          foot={<span>{t("siteServers.page.segmentsFoot")}</span>}
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
            aria-label={t("siteServers.page.filterLabel")}
          >
            <option value="">{t("siteServers.page.filterAll")}</option>
            <option value="enabled">{t("siteServers.page.filterEnabled")}</option>
            <option value="disabled">{t("siteServers.page.filterDisabled")}</option>
          </select>
        </FilterBar>
        {/* WHAT THE ROLES COLUMN CANNOT SELL. Auditor A (HIGH): "Internal spec
            reference shipped in production UI copy" -- the limitation is still
            disclosed, in words a buyer can act on, beside the Roles column it
            is about rather than in the page description. */}
        <p className="note" style={{ padding: "12px 18px 0", margin: 0 }}>
          {t("siteServers.page.rolesNote")}
        </p>
        {filteredToNothing ? (
          <EmptyState
            icon="filter"
            title={t("siteServers.page.noMatchTitle")}
            body={t("siteServers.page.noMatchBody")}
          />
        ) : (
          <SiteServerList
            servers={data && stateFilter ? visible : data}
            isLoading={isLoading}
            error={error}
          />
        )}
      </Panel>

      {manage ? (
        <Dialog
          open={promoteOpen}
          onClose={() => setPromoteOpen(false)}
          title={t("siteServers.page.promoteTitle")}
        >
          <PromoteDialog
            devices={devices.data?.items ?? []}
            promotedDeviceIds={servers.map((server) => server.device_id)}
            error={promote.error}
            onCancel={() => setPromoteOpen(false)}
            onSubmit={(body) =>
              promote.mutate(body, { onSuccess: () => setPromoteOpen(false) })
            }
          />
        </Dialog>
      ) : null}
    </section>
  );
}
