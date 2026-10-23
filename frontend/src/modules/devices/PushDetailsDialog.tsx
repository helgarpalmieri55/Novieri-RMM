/**
 * "Push install details": set the DNS name, OS and processor on an asset that
 * has no agent, so a push can reach it (see `PushDetailsFields`).
 *
 * ONLY WHAT CHANGED IS SENT. The asset-update audit records the FIELD NAMES an
 * edit touched (`asset.updated`), and an edit that resent all three would
 * claim to have changed a DNS name nobody touched.
 *
 * Offered only for a device with no agent: on a managed device the agent
 * reports all three, and the server refuses a hand edit (409) rather than
 * accept one the next inventory run would silently revert. Hiding it there
 * gates nothing (invariant 6); the server's refusal is the control.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Form, FormActions } from "../../components/form";
import { Button, Dialog } from "../../components/ui";
import type { AssetInput, Device } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { useT } from "../../i18n/context";
import { PushDetailsFields, type PushDetails } from "./PushDetailsFields";

function initial(device: Device): PushDetails {
  const arch = device.architecture === "amd64" || device.architecture === "arm64" ? device.architecture : "";
  return { fqdn: device.fqdn ?? "", os: device.os_family ?? "", arch };
}

export function PushDetailsDialog({
  device,
  open,
  onClose,
}: {
  device: Device;
  open: boolean;
  onClose: () => void;
}) {
  const t = useT();
  const api = useApi();
  const queryClient = useQueryClient();
  const [details, setDetails] = useState<PushDetails>(() => initial(device));

  const save = useMutation({
    mutationFn: () => {
      const was = initial(device);
      const body: AssetInput = {};
      if (details.fqdn.trim() !== was.fqdn) body.fqdn = details.fqdn.trim() || null;
      if (details.os !== was.os) body.os_family = details.os || null;
      if (details.arch !== was.arch) body.architecture = details.arch || null;
      return api.updateAsset(device.id, body);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["devices"] });
      void queryClient.invalidateQueries({ queryKey: ["device", device.id] });
      onClose();
    },
  });

  function close() {
    save.reset();
    setDetails(initial(device));
    onClose();
  }

  return (
    <Dialog open={open} onClose={close} title={t("devices.pushDetails.title")}>
      <Form onSubmit={() => save.mutate()}>
        <p className="note mb-3">
          {t("devices.pushDetails.note")}
        </p>
        <div className="form-grid">
          <PushDetailsFields value={details} onChange={setDetails} />
        </div>
        <div className="flex items-center justify-end gap-2">
          <Button type="button" variant="ghost" onClick={close}>
            {t("common.actions.cancel")}
          </Button>
          <FormActions
            label={t("devices.pushDetails.save")}
            pendingLabel={t("devices.pushDetails.saving")}
            pending={save.isPending}
            error={save.isError ? save.error : undefined}
            failedMessage={t("devices.pushDetails.failed")}
          />
        </div>
      </Form>
    </Dialog>
  );
}
