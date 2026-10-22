// The site-server list: v3's rows (`P['site-servers']` in
// docs/design/itms-prototype-v3.html), led by the server glyph, the name over
// its declared segments, the agent's state as a pill and the enable state as a
// second one.
//
// `enabled: false` RENDERS DISTINCTLY FROM A LIVE ROW, and that is the whole
// reason this component exists rather than a bare table. A demote-without-delete
// is a deliberate state, the row keeps its history and its audit trail, and a
// page where a disabled site server looks like a live one is a page that gets
// trusted. A disabled server takes no sweep preference (the preference subquery
// requires `s.enabled`) and refuses wakes, so an operator reading the list must
// be able to see that at a glance: the pill says "not serving" in words, with
// the muted glyph, never by colour alone.

import { Fragment } from "react";
import { Link } from "react-router-dom";

import { EmptyState } from "../../components/emptystate";
import { useT } from "../../i18n/context";
import { Table, Td, Th } from "../../components/ui";
import { KIND_ICON, StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { roleLabels, type SiteServerOut } from "./types";

function AgentStatus({ server }: { server: SiteServerOut }) {
  const t = useT();
  if (server.agent_status === null) {
    // A site server always has an `agents` row -- promotion refuses a device
    // without one -- so a null here means the agent row went away, not that it
    // was never there.
    return (
      <span data-testid={`agent-${server.id}`}>
        <StatusPill value="missing" kind="crit" label={t("siteServers.list.agentMissing")} />
      </span>
    );
  }
  return (
    <span data-testid={`agent-${server.id}`}>
      <StatusPill value={server.agent_status} />
    </span>
  );
}

export function SiteServerList({
  servers,
  isLoading,
  error,
}: {
  servers: SiteServerOut[] | undefined;
  isLoading?: boolean;
  error?: { message?: string } | null;
}) {
  const t = useT();
  if (isLoading) {
    return (
      <div className="empty">
        <Icon name="clock" size={26} />
        <b>{t("siteServers.list.loading")}</b>
      </div>
    );
  }
  if (error) {
    return (
      <div className="empty" role="alert">
        <Icon name={KIND_ICON.crit} size={26} />
        <b>{t("siteServers.list.error")}</b>
      </div>
    );
  }
  if (!servers || servers.length === 0) {
    return (
      <div data-testid="no-site-servers">
        <EmptyState
          icon="server"
          title={t("siteServers.list.emptyTitle")}
          body={t("siteServers.list.emptyBody")}
        />
      </div>
    );
  }

  return (
    <Table label={t("siteServers.list.table")}>
      <thead>
        <tr>
          <Th>{t("siteServers.list.name")}</Th>
          <Th>{t("siteServers.list.agent")}</Th>
          <Th>{t("siteServers.list.state")}</Th>
          <Th>{t("siteServers.list.covers")}</Th>
          <Th>{t("siteServers.list.roles")}</Th>
        </tr>
      </thead>
      <tbody>
        {servers.map((server) => (
          <tr key={server.id} data-testid={`row-${server.id}`}>
            <Td lead={<Icon name="server" size={18} />}>
              <div className="cell-title">
                <Link to={`/site-servers/${server.id}`}>{server.name}</Link>
              </div>
              {/* THE SEPARATOR BELONGS TO THE PREFIX BEFORE IT. `" · "` has an
                  ordinary space on both sides, so at 390px the line broke on
                  the left one and left the dot stranded on a line of its own
                  between two prefixes — a three-line cell with a bullet
                  floating in the middle of it. A non-breaking space before the
                  dot pins it to the prefix it follows; the ordinary space
                  after it is where the line is still free to break. */}
              <div className="cell-sub mono">
                {server.segments.map((segment, at) => (
                  <Fragment key={segment}>
                    {at > 0 ? "\u00a0· " : null}
                    <span data-testid={`segment-${server.id}`}>{segment}</span>
                  </Fragment>
                ))}
              </div>
            </Td>
            <Td>
              <AgentStatus server={server} />
            </Td>
            <Td>
              <span data-testid={`state-${server.id}`}>
                {server.enabled ? (
                  <StatusPill value="enabled" kind="ok" label={t("siteServers.list.enabledPill")} />
                ) : (
                  <StatusPill value="disabled" kind="mut" label={t("siteServers.list.disabledPill")} />
                )}
              </span>
            </Td>
            <Td>
              <span className="mono">
                {t("siteServers.list.segmentCount", { count: server.segments.length })}
              </span>
            </Td>
            <Td>
              {/* IN WORDS, NOT IN THE WIRE TOKEN. `roles.join(", ")` in mono
                  read "wake"; the operator's word for that capability is
                  Wake-on-LAN, and it is prose, so it is not mono either. */}
              <span data-testid={`roles-${server.id}`}>{roleLabels(server.roles)}</span>
            </Td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}
