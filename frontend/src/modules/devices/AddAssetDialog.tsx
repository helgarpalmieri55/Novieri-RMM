/**
 * Record an asset by hand (docs/modules/asset-inventory/spec.md §2).
 *
 * The operator's ask, verbatim: "I should be able to manually add an asset with
 * name, description, IP address" — for stock in the warehouse and for devices
 * that can never run an agent, like networking gear.
 *
 * SO EVERY FIELD HERE IS OPTIONAL, including the name. A laptop in a box has
 * no hostname, and a form that demanded one would make the warehouse case —
 * half the reason this feature exists — impossible to enter. The server accepts
 * a record whose entire identity is a description, and this form has to as
 * well.
 *
 * WHAT IS NOT ON THIS FORM: `source`. It is stamped `manual` server-side, so a
 * row cannot claim an agent wrote it. Nor is there an "is managed" control —
 * that is a join, and offering it as a field would let an operator assert
 * something the database derives.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Form, FormActions } from "../../components/form";
import { Button, Dialog, Field, inputClass } from "../../components/ui";
import type { AssetCreateInput, Device } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { requireKnown } from "../../lib/internalClient";
import { humaniseEnum } from "../../lib/labels";
import { useT } from "../../i18n/context";
import { PushDetailsFields, type PushDetails } from "./PushDetailsFields";

const ASSET_TYPES: Device["asset_type"][] = [
  "computer",
  "server",
  "network",
  "printer",
  "phone",
  "appliance",
  "other",
];

/**
 * The lifecycle vocabulary, with the operator-facing wording that keeps the two
 * "un-" words apart. "Unmanaged" (no agent) is a to-do list; "unmanageable"
 * (this will never have one) must never appear on it — so the label says what
 * the value MEANS rather than repeating the word. The words are
 * `enums.DeviceLifecycle`, looked up when the option is drawn.
 */
const LIFECYCLES: Device["lifecycle"][] = ["in_service", "stock", "unmanageable", "retired"];

export function AddAssetDialog({
  open,
  onClose,
  siteId,
}: {
  open: boolean;
  onClose: () => void;
  /**
   * The site the asset is filed under, and with it its client (MSP clients
   * spec §1.5: `POST /devices` requires it). `null` until the page knows it;
   * a submit then says so and sends nothing.
   */
  siteId: string | null;
}) {
  const t = useT();
  const api = useApi();
  const queryClient = useQueryClient();
  const [hostname, setHostname] = useState("");
  const [description, setDescription] = useState("");
  const [primaryIp, setPrimaryIp] = useState("");
  const [assetType, setAssetType] = useState<Device["asset_type"]>("computer");
  const [lifecycle, setLifecycle] = useState<Device["lifecycle"]>("in_service");
  const [location, setLocation] = useState("");
  const [pushDetails, setPushDetails] = useState<PushDetails>({ fqdn: "", os: "", arch: "" });

  const create = useMutation({
    mutationFn: () => {
      // Empty strings are omitted rather than sent. The API rejects a malformed
      // IP, and "" is malformed -- so a form that sent every field would refuse
      // every asset whose IP the operator did not know.
      const body: AssetCreateInput = {
        // PHASE 3: the client/site selector. The Devices page passes the
        // internal client's "Principal" site (`lib/internalClient.ts`).
        site_id: requireKnown(siteId),
        asset_type: assetType,
        lifecycle,
      };
      if (hostname.trim()) body.hostname = hostname.trim();
      if (description.trim()) body.description = description.trim();
      if (primaryIp.trim()) body.primary_ip = primaryIp.trim();
      if (location.trim()) body.location = location.trim();
      if (pushDetails.fqdn.trim()) body.fqdn = pushDetails.fqdn.trim();
      if (pushDetails.os) body.os_family = pushDetails.os;
      if (pushDetails.arch) body.architecture = pushDetails.arch;
      return api.createAsset(body);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["devices"] });
      setHostname("");
      setDescription("");
      setPrimaryIp("");
      setLocation("");
      setPushDetails({ fqdn: "", os: "", arch: "" });
      onClose();
    },
  });

  // CLOSING CLEARS THE LAST FAILURE. This component stays mounted while
  // `open` is false, so the mutation's error outlived the dialog: cancel after
  // a 422 and reopen, and a fresh, untouched form was already accusing the
  // operator of something they had not yet done.
  function close() {
    create.reset();
    onClose();
  }

  return (
    <Dialog open={open} onClose={close} title={t("devices.addAsset.title")}>
      {/* A REAL `<form>`, WHICH IS MOST OF WHAT WAS WRONG HERE. This dialog
          was the last create form in the module that never adopted the form
          kernel (`components/form.tsx`): no `<form>`, so Enter -- the natural
          way to finish six fields -- did nothing, while the same keystroke in
          "New device group" one dialog along worked; and a hand-drawn failure
          line beside a hand-rolled submit button. */}
      <Form onSubmit={() => create.mutate()}>
        <p className="note mb-3">
          {t("devices.addAsset.note")}
        </p>
        <div className="form-grid">
          <Field label={t("devices.addAsset.name")}>
            <input
              className={inputClass}
              value={hostname}
              onChange={(event) => setHostname(event.target.value)}
              placeholder={t("devices.addAsset.namePlaceholder")}
            />
          </Field>
          <Field label={t("devices.addAsset.description")}>
            <input
              className={inputClass}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder={t("devices.addAsset.descriptionPlaceholder")}
            />
          </Field>
          <Field label={t("devices.addAsset.ip")}>
            <input
              className={inputClass}
              value={primaryIp}
              onChange={(event) => setPrimaryIp(event.target.value)}
              placeholder="172.22.104.1"
            />
          </Field>
          <Field label={t("devices.addAsset.type")}>
            <select
              className={inputClass}
              value={assetType}
              onChange={(event) => setAssetType(event.target.value as Device["asset_type"])}
            >
              {ASSET_TYPES.map((value) => (
                <option key={value} value={value}>
                  {humaniseEnum(value, "DeviceAssetType")}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("devices.addAsset.lifecycle")}>
            <select
              className={inputClass}
              value={lifecycle}
              onChange={(event) => setLifecycle(event.target.value as Device["lifecycle"])}
            >
              {LIFECYCLES.map((value) => (
                <option key={value} value={value}>
                  {humaniseEnum(value, "DeviceLifecycle")}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("devices.addAsset.location")}>
            <input
              className={inputClass}
              value={location}
              onChange={(event) => setLocation(event.target.value)}
              placeholder={t("devices.addAsset.locationPlaceholder")}
            />
          </Field>
          {/* What a push needs to reach a machine that has never run the
              agent (`PushDetailsFields`). Optional like everything here. */}
          <PushDetailsFields value={pushDetails} onChange={setPushDetails} />
        </div>
        {/* THE FAILURE LINE WAS HAND-DRAWN, AND THAT IS THE THEME MISMATCH.
            It was `<p role="alert" className="mb-2 text-sm text-[var(--red)]">`
            -- 14px, which is the ARTICLE ramp's H4, in `--red`, the FILL token.
            The product has a component for exactly this sentence and it is
            `.form-error`: 12px in `--red-ink`, the tone the ink family exists
            to carry, and what "New device group" three feet away renders.
            DESIGN.md's console ramp is 17/15/13/12 and says of the three ramps
            that they do not merge.

            `FormActions` brings the whole surface, not just the size: one
            announced alert rather than several, `failedMessage` naming what did
            NOT happen before the reason, and `type="submit"` so the button and
            the Enter key are the same control rather than two.

            NO `problems`. Every field here is optional on purpose -- a boxed
            laptop may have nothing but a description -- so there is no
            client-side check to report, and the kernel's rule about never
            disabling for missing input is satisfied by there being none. */}
        {/* `items-center`, as `notification/ChannelsPanel` does it. The kernel's
            `.form-actions` carries a 16px `margin-top` of its own, so a bare
            `flex justify-end` stretches Cancel to the taller box beside it and
            the two controls stop lining up. */}
        <div className="flex items-center justify-end gap-2">
          {/* `type="button"` is load-bearing inside a real `<form>`: an untyped
              button submits, so the control whose whole purpose is to ABANDON
              the form would send it. */}
          <Button type="button" variant="ghost" onClick={close}>
            {t("common.actions.cancel")}
          </Button>
          <FormActions
            label={t("devices.addAsset.submit")}
            pendingLabel={t("devices.addAsset.adding")}
            pending={create.isPending}
            error={create.isError ? create.error : undefined}
            failedMessage={t("devices.addAsset.failed")}
          />
        </div>
      </Form>
    </Dialog>
  );
}
