/**
 * THE CLIENT 360 (redesign design §1.3; Atera's customer page): everything
 * about one client, in eight tabs that change the URL and fit one row --
 * Atera's eleven wrap into two, a flaw its own walkthrough files.
 *
 * EVERY READ IS THIS CLIENT'S (decision D6): `useNarrowedApi` narrows each request
 * to it while every client is shown above; with this client chosen above,
 * the console's own client already is.
 *
 * ANOTHER CLIENT CHOSEN ABOVE: with another client's id in `X-Client-Id`
 * this client does not exist for the request -- a 404 on every tab -- so the
 * page reads nothing and says which client the console is working on, with
 * the one act that fixes it. NOT BEFORE `/me` HAS ANSWERED (phase 3 final
 * review C): until then the page decides nothing.
 *
 * A TAB SHOWS ONLY WITH ITS CODE IN THIS CLIENT. Cosmetic (invariant 6): the
 * server refuses the read either way.
 */
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";

import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import { useClientScope } from "../../lib/clientScope";
import { Btn, DetailHead, QueryState, StatCard, StatsGrid, UrlTabs, useNarrowedApi, useUrlTab } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useRecordTitle } from "../../shell/title";
import { KIND_ICON } from "../../shell/viz";
import { AlertsTab } from "./AlertsTab";
import { useClientRecord } from "./api";
import { AssetsTab } from "./AssetsTab";
import { ClientActions } from "./ClientActions";
import { ContactsTab } from "./ContactsTab";
import { DevicesTab } from "./DevicesTab";
import { clientKindLabel, clientStatusKind, clientStatusLabel } from "./labels";
import { ModulesTab } from "./ModulesTab";
import { SitesTab } from "./SitesTab";
import { SummaryTab } from "./SummaryTab";
import { TicketsTab } from "./TicketsTab";

const TABS = ["summary", "sites", "contacts", "devices", "tickets", "alerts", "assets", "modules"] as const;
type Tab = (typeof TABS)[number];

const TAB_LABEL: Record<Tab, MessageKey> = {
  summary: "clients.detail.tabs.summary",
  sites: "clients.detail.tabs.sites",
  contacts: "clients.detail.tabs.contacts",
  devices: "clients.detail.tabs.devices",
  tickets: "clients.detail.tabs.tickets",
  alerts: "clients.detail.tabs.alerts",
  assets: "clients.detail.tabs.assets",
  modules: "clients.detail.tabs.modules",
};

/** The codes a tab needs IN THIS CLIENT (any one of them); `null`: whoever can open the client. */
const TAB_CODES: Record<Tab, readonly string[] | null> = {
  summary: null,
  sites: null,
  contacts: null,
  devices: ["devices.read"],
  tickets: ["ticket.read"],
  alerts: ["health.read"],
  assets: ["licenses.read", "subscriptions.read", "ipam.read"],
  modules: null,
};

export function ClientDetailPage() {
  const t = useT();
  const { clientId = "" } = useParams();
  const { selected, select, clients, ready } = useClientScope();
  const elsewhere = selected !== null && selected !== clientId;
  const client = useClientRecord(clientId, !elsewhere);
  const codes = new Set(clients.find((item) => item.id === clientId)?.permissions ?? []);
  const visible = TABS.filter((key) => TAB_CODES[key]?.some((code) => codes.has(code)) ?? true);
  const [tab, setTab] = useUrlTab<Tab>(visible, "summary");
  // THE READS ARE THIS CLIENT'S AND NOBODY ELSE'S (decisions D6, D17): narrowed
  // to it with X-Client-Id, or the top bar has chosen it. Nothing before /me
  // has answered (`ready`; until then `api` rejects with NarrowingPending), so
  // every client's rows never sit under this client's name.
  const { api, client: sent, ready: narrowingReady } = useNarrowedApi(clientId);
  const onThis = narrowingReady && (sent === clientId || selected === clientId);
  const reads = !elsewhere && client.data !== undefined && onThis;
  const openTickets = useQuery({
    queryKey: ["client360", clientId, "openTickets", sent],
    queryFn: () => api.listTickets({ client_id: clientId, scope: "open", limit: 1 }),
    enabled: reads && codes.has("ticket.read"),
  });
  const alerts = useQuery({
    queryKey: ["client360", clientId, "alertSummary", sent],
    queryFn: () => api.getHealthAlertSummary(),
    enabled: reads && codes.has("health.read"),
  });
  useRecordTitle(client.data?.name);
  const back = { to: "/clients", label: t("clients.detail.allClients") };

  if (elsewhere && !ready) {
    return (
      <>
        <DetailHead back={back} title={t("clients.detail.title")} />
        <QueryState loading error={null} empty={false} emptyLabel="">
          {null}
        </QueryState>
      </>
    );
  }

  if (elsewhere) {
    const reachable = clients.some((item) => item.id === clientId);
    return (
      <>
        <DetailHead back={back} title={t("clients.detail.title")} />
        <div role="status" className="notice mb-4">
          <Icon name={KIND_ICON.info} size={17} />
          <div>
            {t("clients.detail.otherSelected", {
              selected: clients.find((item) => item.id === selected)?.name ?? selected,
            })}{" "}
            <Btn
              small
              label={reachable ? t("clients.detail.workOnThis") : t("clients.detail.workOnAll")}
              onClick={() => select(reachable ? clientId : null)}
            />
          </div>
        </div>
      </>
    );
  }

  const data = client.data;
  const here = (key: Tab) => `/clients/${clientId}${key === "summary" ? "" : `?tab=${key}`}`;
  return (
    <>
      <DetailHead
        back={back}
        title={data?.name ?? t("clients.detail.title")}
        status={data ? { kind: clientStatusKind(data.status), label: clientStatusLabel(data.status) } : undefined}
        context={data ? clientKindLabel(data.kind) : undefined}
        actions={data ? <ClientActions client={data} /> : undefined}
      />
      <QueryState loading={client.isLoading} error={client.error} empty={false} emptyLabel="">
        {data ? (
          <>
            {data.status === "archived" ? (
              <div role="status" className="notice mb-4">
                <Icon name={KIND_ICON.info} size={17} />
                <div>{t("clients.detail.archived")}</div>
              </div>
            ) : null}
            <StatsGrid columns={4}>
              {visible.includes("devices") ? (
                <StatCard label={t("clients.detail.tiles.devices")} value={String(data.device_count)} to={here("devices")} icon="devices" />
              ) : null}
              <StatCard label={t("clients.detail.tiles.sites")} value={String(data.site_count)} to={here("sites")} icon="map-pin" />
              {visible.includes("tickets") ? (
                <StatCard label={t("clients.detail.tiles.openTickets")} value={String(openTickets.data?.total ?? 0)} pending={openTickets.data === undefined} to={here("tickets")} icon="ticket" />
              ) : null}
              {visible.includes("alerts") ? (
                <StatCard label={t("clients.detail.tiles.openAlerts")} value={String(alerts.data?.open ?? 0)} pending={alerts.data === undefined} to={here("alerts")} icon="health" />
              ) : null}
            </StatsGrid>
            <UrlTabs
              label={t("clients.detail.title")}
              items={visible.map((key) => ({ key, label: t(TAB_LABEL[key]) }))}
              active={tab}
              onSelect={setTab}
            >
              {tab === "summary" ? (
                <SummaryTab client={data} />
              ) : tab === "sites" ? (
                <SitesTab client={data} />
              ) : tab === "contacts" ? (
                <ContactsTab client={data} />
              ) : tab === "devices" ? (
                <DevicesTab client={data} enabled={reads} />
              ) : tab === "tickets" ? (
                <TicketsTab client={data} enabled={reads} />
              ) : tab === "alerts" ? (
                <AlertsTab client={data} enabled={reads} />
              ) : tab === "assets" ? (
                <AssetsTab client={data} codes={codes} enabled={reads} />
              ) : (
                <ModulesTab client={data} />
              )}
            </UrlTabs>
          </>
        ) : null}
      </QueryState>
    </>
  );
}
