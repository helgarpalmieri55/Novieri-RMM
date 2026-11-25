/**
 * A client's details and the health profile its devices fall back to (design
 * §5.1), read-only. Its edit and its archive are in the page's head
 * (`ClientActions.tsx`, redesign design §2).
 */
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { NoValue } from "../../components/emptystate";
import { useT } from "../../i18n/context";
import type { ClientOut } from "../../lib/api";
import { useClientScope } from "../../lib/clientScope";
import { Panel, QueryState } from "../../shell/components";
import { KV } from "../../shell/viz";
import { useHealthAssignments } from "../health/api";
import { clientHealthDefault, clientKindLabel, clientStatusLabel } from "./labels";

export function SummaryTab({ client }: { client: ClientOut }) {
  const t = useT();
  const { clients } = useClientScope();
  const mine = clients.find((item) => item.id === client.id);
  const canReadHealth = mine?.permissions.includes("health.read") ?? false;
  const notSet = <NoValue label={t("clients.summary.notSet")} />;
  // Typed as tuples: an array literal of pairs would be inferred as arrays,
  // which `KV`'s `readonly [ReactNode, ReactNode]` does not accept.
  const facts: (readonly [string, ReactNode])[] = [
    [t("clients.summary.kind"), clientKindLabel(client.kind)],
    [t("clients.summary.status"), clientStatusLabel(client.status)],
    [t("clients.summary.emailDomains"), client.email_domains.length > 0 ? client.email_domains.join(", ") : notSet],
    [t("clients.summary.phone"), client.phone || notSet],
    [t("clients.summary.address"), client.address || notSet],
    [t("clients.summary.website"), client.website || notSet],
    [t("clients.summary.notes"), client.notes || notSet],
    [t("clients.summary.devices"), String(client.device_count)],
    [t("clients.summary.sites"), String(client.site_count)],
  ];

  return (
    <>
      <Panel title={t("clients.summary.title")}>
        <KV items={facts} />
      </Panel>
      {canReadHealth ? <HealthDefault clientId={client.id} /> : null}
    </>
  );
}

/** Read-only: the profile is assigned on the Health page (Task 14). */
function HealthDefault({ clientId }: { clientId: string }) {
  const t = useT();
  const assignments = useHealthAssignments();
  const { own, org } = clientHealthDefault(assignments.data ?? [], clientId);
  return (
    <Panel
      title={t("clients.summary.healthTitle")}
      actions={
        <Link className="btn" to="/health#profiles">
          {t("clients.summary.healthWhere")}
        </Link>
      }
    >
      <QueryState
        loading={assignments.isLoading}
        error={assignments.error}
        empty={false}
        emptyLabel={t("clients.summary.healthNone")}
      >
        <p>
          {own
            ? t("clients.summary.healthOwn", { profile: own.profile_name })
            : org
              ? t("clients.summary.healthOrg", { profile: org.profile_name })
              : t("clients.summary.healthNone")}
        </p>
      </QueryState>
    </Panel>
  );
}
