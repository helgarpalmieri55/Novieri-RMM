/**
 * Approve and Reject for a computer waiting for review (install-link spec §1.5).
 *
 * REJECT IS CONFIRMED, AND THE CONFIRMATION SAYS WHAT IT DOES: the agent is
 * told to uninstall itself, the certificate is revoked, the row leaves the
 * list, and nothing brings it back but installing the agent again. The notice
 * a Reject returns is handed UP (`onRejected`) because the row -- and this
 * component -- disappear the moment the list refreshes.
 *
 * INVARIANT 6: the page renders this only for a holder of `devices.review`;
 * both routes check it themselves.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ConfirmDialog } from "../../components/confirm";
import { describeSubmitError } from "../../components/form";
import type { Device } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { useT } from "../../i18n/context";
import { Btn } from "../../shell/components";

export const REVIEW_PERMISSION = "devices.review";

export function ReviewActions({
  device,
  label,
  onRejected,
}: {
  device: Device;
  /** The name the operator recognises the row by. */
  label: string;
  onRejected: (message: string) => void;
}) {
  const t = useT();
  const api = useApi();
  const client = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  // `["devices"]` is a prefix of both the list and the nav count's key.
  const refresh = () => void client.invalidateQueries({ queryKey: ["devices"] });
  const approve = useMutation({ mutationFn: () => api.approveDevice(device.id), onSuccess: refresh });
  const reject = useMutation({
    mutationFn: () => api.rejectDevice(device.id),
    onSuccess: (result) => {
      onRejected(result.message);
      refresh();
    },
  });
  const busy = approve.isPending || reject.isPending;
  const failure = approve.error ?? reject.error;

  return (
    // `.row-actions` has no rule in any stylesheet; the layout is the flex row
    // the console's other button groups use.
    <div className="flex flex-wrap items-center gap-2">
      <Btn small variant="primary" label={t("devices.review.approve")} ariaLabel={t("devices.review.approveAria", { label })} disabled={busy} onClick={() => approve.mutate()} />
      <Btn small variant="danger" label={t("devices.review.reject")} ariaLabel={t("devices.review.rejectAria", { label })} disabled={busy} onClick={() => setConfirming(true)} />
      {failure ? (
        <p role="alert" className="form-error">
          {describeSubmitError(failure)}
        </p>
      ) : null}
      <ConfirmDialog
        open={confirming}
        title={t("devices.review.confirmTitle")}
        subject={label}
        body={t("devices.review.confirmBody")}
        confirmLabel={t("devices.review.confirmLabel")}
        destructive
        onConfirm={() => {
          setConfirming(false);
          reject.mutate();
        }}
        onCancel={() => setConfirming(false)}
      />
    </div>
  );
}

/**
 * A rejected computer whose agent may still be installed: what to do, in
 * words, and -- for a holder of `devices.review` -- "I removed it", which
 * records that someone did and takes the row out of the list. Not confirmed:
 * it changes nothing on the computer, and says only what a person already did.
 */
export function ManualRemoval({
  device,
  label,
  status,
  canAcknowledge,
}: {
  device: Device;
  /** The name the operator recognises the row by. */
  label: string;
  /** `manualRemovalStatus(device)`: the sentence saying what to do. */
  status: string;
  canAcknowledge: boolean;
}) {
  const t = useT();
  const api = useApi();
  const client = useQueryClient();
  const acknowledge = useMutation({
    mutationFn: () => api.acknowledgeDeviceRemoval(device.id),
    // `["devices"]` is a prefix of both the list and the nav count's key.
    onSuccess: () => void client.invalidateQueries({ queryKey: ["devices"] }),
  });

  return (
    // A MINIMUM WIDTH: the Actions column is sized for two short buttons, and
    // a sentence squeezed into it ran to eight lines and a row four times
    // the height of its neighbours.
    <div className="flex min-w-72 flex-wrap items-center gap-2">
      <p className="note">{status}</p>
      {canAcknowledge ? (
        <Btn
          small
          label={t("devices.review.removed")}
          ariaLabel={t("devices.review.removedAria", { label })}
          disabled={acknowledge.isPending}
          onClick={() => acknowledge.mutate()}
        />
      ) : null}
      {acknowledge.error ? (
        <p role="alert" className="form-error">
          {describeSubmitError(acknowledge.error)}
        </p>
      ) : null}
    </div>
  );
}
