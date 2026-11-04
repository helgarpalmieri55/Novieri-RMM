/**
 * One client (MSP clients phase 3, design §4.1): Summary, Sites, Contacts and
 * Modules, the tab in the URL (`?tab=`) so another page can open one.
 *
 * ANOTHER CLIENT CHOSEN ABOVE. Every read here carries `X-Client-Id` (Task 7),
 * and with another client's id this client does not exist for the request:
 * a 404 on every tab. So the page reads nothing and says which client the
 * console is working on, with the one act that fixes it.
 *
 * NOT BEFORE `/me` HAS ANSWERED (phase 3 final review C, T12 minor). On a
 * cold deep link the top bar's choice is known first, and the page named that
 * client by its raw id and offered "Work on all my clients" for one the
 * person may well choose. While the scope is not ready the page reads nothing
 * and decides nothing.
 */
import { Link, useParams, useSearchParams } from "react-router-dom";

import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import { useClientScope } from "../../lib/clientScope";
import { Btn, PageHead, QueryState, Tabs } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { KIND_ICON } from "../../shell/viz";
import { useClientRecord } from "./api";
import { ContactsTab } from "./ContactsTab";
import { clientKindLabel } from "./labels";
import { ModulesTab } from "./ModulesTab";
import { SitesTab } from "./SitesTab";
import { SummaryTab } from "./SummaryTab";

type Tab = "summary" | "sites" | "contacts" | "modules";

const TABS: readonly { key: Tab; label: MessageKey }[] = [
  { key: "summary", label: "clients.detail.tabs.summary" },
  { key: "sites", label: "clients.detail.tabs.sites" },
  { key: "contacts", label: "clients.detail.tabs.contacts" },
  { key: "modules", label: "clients.detail.tabs.modules" },
];

function parseTab(value: string | null): Tab {
  return TABS.find((tab) => tab.key === value)?.key ?? "summary";
}

export function ClientDetailPage() {
  const t = useT();
  const { clientId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const tab = parseTab(params.get("tab"));
  const { selected, select, clients, ready } = useClientScope();
  const elsewhere = selected !== null && selected !== clientId;
  const client = useClientRecord(clientId, !elsewhere);

  const back = (
    <Link className="btn" to="/clients">
      {t("clients.detail.allClients")}
    </Link>
  );

  if (elsewhere && !ready) {
    // Which client is chosen, and whether this one could be, are `/me`'s.
    return (
      <>
        <PageHead title={t("clients.detail.title")} actions={back} />
        <QueryState loading error={null} empty={false} emptyLabel={t("clients.list.empty")}>
          {null}
        </QueryState>
      </>
    );
  }

  if (elsewhere) {
    const reachable = clients.some((item) => item.id === clientId);
    return (
      <>
        <PageHead title={t("clients.detail.title")} actions={back} />
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

  function setTab(key: string) {
    const next = new URLSearchParams(params);
    if (key === "summary") next.delete("tab");
    else next.set("tab", key);
    setParams(next, { replace: true });
  }

  const data = client.data;
  return (
    <>
      <PageHead
        title={data?.name ?? t("clients.detail.title")}
        description={data ? clientKindLabel(data.kind) : undefined}
        actions={back}
      />
      <QueryState
        loading={client.isLoading}
        error={client.error}
        empty={false}
        emptyLabel={t("clients.list.empty")}
      >
        {data ? (
          <>
            {data.status === "archived" ? (
              <div role="status" className="notice mb-4">
                <Icon name={KIND_ICON.info} size={17} />
                <div>{t("clients.detail.archived")}</div>
              </div>
            ) : null}
            <Tabs
              items={TABS.map((item) => ({ key: item.key, label: t(item.label) }))}
              active={tab}
              onSelect={setTab}
              panelId="client-tabpanel"
            />
            <div
              id="client-tabpanel"
              role="tabpanel"
              aria-label={t(TABS.find((item) => item.key === tab)?.label ?? "clients.detail.tabs.summary")}
              tabIndex={-1}
            >
              {tab === "summary" ? (
                <SummaryTab client={data} />
              ) : tab === "sites" ? (
                <SitesTab client={data} />
              ) : tab === "contacts" ? (
                <ContactsTab client={data} />
              ) : (
                <ModulesTab client={data} />
              )}
            </div>
          </>
        ) : null}
      </QueryState>
    </>
  );
}
