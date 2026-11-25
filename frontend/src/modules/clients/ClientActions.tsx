/**
 * A client's two acts, in the head of its 360 page (redesign design §2,
 * "Ficha": the record's actions on the right): its edit (`clients.manage` ON
 * THIS CLIENT, from `/me.clients`) and its archive (`clients.manage` ON EVERY
 * CLIENT, a global route; never offered for the internal or the unassigned
 * client). Moved here from the summary tab's Details panel unchanged.
 *
 * AN ARCHIVE REFUSED IS EXPLAINED. The server's 409 says why in the person's
 * language -- "this client has 3 active devices; retire them before archiving
 * it" -- and the page puts it under the buttons, after a sentence saying the
 * archive did not happen.
 */
import { useState } from "react";

import { ConfirmDialog } from "../../components/confirm";
import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import type { ClientOut } from "../../lib/api";
import { useClientScope } from "../../lib/clientScope";
import { useEverywherePermissions } from "../../lib/mePermissions";
import { Btn } from "../../shell/components";
import { useArchiveClientMutation } from "./api";
import { ClientFormDialog } from "./ClientForm";

export function ClientActions({ client }: { client: ClientOut }) {
  const t = useT();
  const { clients } = useClientScope();
  const everywhere = useEverywherePermissions();
  const mine = clients.find((item) => item.id === client.id);
  const canEdit = client.status === "active" && (mine?.permissions.includes("clients.manage") ?? false);
  const canArchive =
    client.kind === "customer" && client.status === "active" && everywhere.has("clients.manage");
  const archive = useArchiveClientMutation(client.id);
  const [editing, setEditing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  if (!canEdit && !canArchive) return null;
  return (
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
      {archive.isError ? (
        <p role="alert" className="form-error">
          {t("clients.summary.archiveFailed")} {describeSubmitError(archive.error)}
        </p>
      ) : null}
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
