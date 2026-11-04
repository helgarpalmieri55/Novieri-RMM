/**
 * A device's site, and moving it to another site of ITS client (MSP clients
 * phase 3, design §4.3; `PATCH /devices/{id}` with `site_id`). Moving a device
 * to another client is out of scope (MSP clients design §6), so `SiteField`
 * offers the client's own sites and the server refuses any other (422).
 *
 * OFFERED to a holder of `assets.manage` IN THE DEVICE'S CLIENT, from
 * `/me.clients`. A courtesy (invariant 6): the route checks the code.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { SiteField } from "../../components/SiteField";
import { Form, FormActions } from "../../components/form";
import { Button, Dialog } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { Device } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { Btn } from "../../shell/components";
import { useSitesOf } from "../devices/siteNames";

export function DeviceSite({ device, canMove }: { device: Device; canMove: boolean }) {
  const t = useT();
  const sites = useSitesOf(device.client_id ? [device.client_id] : []);
  const [open, setOpen] = useState(false);
  const name = sites.siteName(device.site_id);
  return (
    <>
      {name ? <span className="cell-sub">{t("inventory.site.label", { site: name })}</span> : null}
      {canMove ? <Btn small label={t("inventory.site.change")} onClick={() => setOpen(true)} /> : null}
      {/* MOUNTED ONLY WHILE OPEN, so every opening starts from the device's
          own site rather than from a choice an earlier opening left behind. */}
      {open ? <DeviceSiteDialog device={device} onClose={() => setOpen(false)} /> : null}
    </>
  );
}

export function DeviceSiteDialog({ device, onClose }: { device: Device; onClose: () => void }) {
  const t = useT();
  const api = useApi();
  const queryClient = useQueryClient();
  // `""` is no site chosen -- `SiteField`'s contract (Task 9).
  const [siteId, setSiteId] = useState(device.site_id ?? "");
  const move = useMutation({
    mutationFn: (target: string) => api.changeDeviceSite(device.id, target),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["device", device.id] });
      void queryClient.invalidateQueries({ queryKey: ["devices"] });
      // Two sites' device counts moved.
      void queryClient.invalidateQueries({ queryKey: ["client-sites"] });
      onClose();
    },
  });
  const unchanged = siteId === "" || siteId === device.site_id;

  return (
    <Dialog open onClose={onClose} title={t("inventory.site.dialogTitle")}>
      <Form
        onSubmit={() => {
          if (!unchanged) move.mutate(siteId);
        }}
      >
        <p className="note mb-3">{t("inventory.site.dialogNote")}</p>
        <div className="form-grid">
          {device.client_id ? <SiteField clientId={device.client_id} value={siteId} onChange={setSiteId} /> : null}
        </div>
        <div className="flex items-center justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("common.actions.cancel")}
          </Button>
          <FormActions
            label={t("inventory.site.move")}
            pendingLabel={t("inventory.site.moving")}
            pending={move.isPending}
            error={move.isError ? move.error : undefined}
            failedMessage={t("inventory.site.moveFailed")}
            disabledReason={unchanged ? t("inventory.site.unchanged") : undefined}
          />
        </div>
      </Form>
    </Dialog>
  );
}
