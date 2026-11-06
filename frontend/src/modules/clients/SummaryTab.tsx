/**
 * A client's details, its edit (`clients.manage` ON THIS CLIENT, from
 * `/me.clients`), its archive (`clients.manage` ON EVERY CLIENT, a global
 * route; never offered for the internal or the unassigned client) and the
 * health profile its devices fall back to (design §5.1), read-only.
 *
 * AN ARCHIVE REFUSED IS EXPLAINED. The server's 409 says why in the person's
 * language -- "this client has 3 active devices; retire them before archiving
 * it" -- and the page puts it under the button, after a sentence saying the
 * archive did not happen.
 */
import { type ReactNode, useState } from "react";
import { Link } from "react-router-dom";

import { ConfirmDialog } from "../../components/confirm";
import { NoValue } from "../../components/emptystate";
import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import type { ClientOut } from "../../lib/api";
import { useClientScope } from "../../lib/clientScope";
import { useEverywherePermissions } from "../../lib/mePermissions";
import { Btn, Panel, QueryState } from "../../shell/components";
import { KV } from "../../shell/viz";
import { useHealthAssignments } from "../health/api";
import { useArchiveClientMutation } from "./api";
import { ClientFormDialog } from "./ClientForm";
import { clientHealthDefault, clientKindLabel, clientStatusLabel } from "./labels";

export function SummaryTab({ client }: { client: ClientOut }) {
  const t = useT();
  const { clients } = useClientScope();
  const everywhere = useEverywherePermissions();
  const mine = clients.find((item) => item.id === client.id);
  const canEdit = client.status === "active" && (mine?.permissions.includes("clients.manage") ?? false);
  const canArchive =
    client.kind === "customer" && client.status === "active" && everywhere.has("clients.manage");
  const canReadHealth = mine?.permissions.includes("health.read") ?? false;
  const archive = useArchiveClientMutation(client.id);
  const [editing, setEditing] = useState(false);
  const [confirming, setConfirming] = useState(false);
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
      <Panel
        title={t("clients.summary.title")}
        actions={
          canEdit || canArchive ? (
            <>
              {canEdit ? <Btn label={t("clients.summary.edit")} icon="edit" onClick={() => setEditing(true)} /> : null}
              {canArchive ? (
                <Btn
                  label={t("clients.summary.archive")}
                  icon="archive"
                  variant="danger"
                  onClick={() => {
                    archive.reset();
                    setConfirming(true);
                  }}
                />
              ) : null}
            </>
          ) : undefined
        }
      >
        <KV items={facts} />
        {archive.isError ? (
          <p role="alert" className="form-error">
            {t("clients.summary.archiveFailed")} {describeSubmitError(archive.error)}
          </p>
        ) : null}
      </Panel>

      {canReadHealth ? <HealthDefault clientId={client.id} /> : null}

      <ClientFormDialog
        key={editing ? "open" : "closed"}
        open={editing}
        client={client}
        onClose={() => setEditing(false)}
        onSaved={() => setEditing(false)}
      />
      <ConfirmDialog
        open={confirming}
        title={t("clients.summary.archiveTitle")}
        subject={client.name}
        body={t("clients.summary.archiveBody")}
        confirmLabel={t("clients.summary.archiveConfirm")}
        destructive
        onCancel={() => setConfirming(false)}
        onConfirm={() => {
          setConfirming(false);
          archive.mutate();
        }}
      />
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
