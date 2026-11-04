/**
 * Record an asset by hand (docs/modules/asset-inventory/spec.md §2).
 *
 * The operator's ask, verbatim: "I should be able to manually add an asset with
 * name, description, IP address" — for stock in the warehouse and for devices
 * that can never run an agent, like networking gear.
 *
 * SO EVERY FIELD HERE IS OPTIONAL BUT THE SITE, including the name. A laptop
 * in a box has no hostname, and a form that demanded one would make the
 * warehouse case — half the reason this feature exists — impossible to enter.
 * The server accepts a record whose entire identity is a description, and this
 * form has to as well. The site is where the record is filed, and with it its
 * client (MSP clients spec §1.5, phase 3 design §3.2).
 *
 * WHAT IS NOT ON THIS FORM: `source`. It is stamped `manual` server-side, so a
 * row cannot claim an agent wrote it. Nor is there an "is managed" control —
 * that is a join, and offering it as a field would let an operator assert
 * something the database derives.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { attemptProblems, Form, FormActions } from "../../components/form";
import type { Attempt } from "../../components/form";
import { ClientField } from "../../components/ClientField";
import { SiteField } from "../../components/SiteField";
import { Button, Dialog, Field, inputClass } from "../../components/ui";
import type { AssetCreateInput, Device } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { requireKnown, useNewRowClient } from "../../lib/clientSelection";
import { humaniseEnum } from "../../lib/labels";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
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

export function AddAssetDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  // MOUNTED ONLY WHILE OPEN (MSP clients phase 3). A closed dialog asks
  // nothing of the client selector, and every opening starts from an empty
  // form with no stale failure -- which `create.reset()` on close used to buy.
  return open ? <AddAssetForm onClose={onClose} /> : null;
}

function AddAssetForm({ onClose }: { onClose: () => void }) {
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
  // THE SITE IS THE ONE REQUIRED FIELD (MSP clients spec §1.5, design §3.2):
  // `POST /devices` takes it and derives the client from it. Its client is the
  // one chosen at the top, or with every client the Client field's.
  const target = useNewRowClient("assets.manage");
  const [pickedClient, setPickedClient] = useState("");
  const clientId = target.needsField ? pickedClient || null : target.clientId;
  const [siteId, setSiteId] = useState("");
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  const create = useMutation({
    mutationFn: () => {
      // Empty strings are omitted rather than sent. The API rejects a malformed
      // IP, and "" is malformed -- so a form that sent every field would refuse
      // every asset whose IP the operator did not know.
      const body: AssetCreateInput = {
        site_id: requireKnown(siteId || null),
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
      onClose();
    },
  });

  function close() {
    onClose();
  }

  function submit() {
    // Held as rules + values, worded at render, so a language switch rewords them.
    const next: Attempt = {
      rules: () => [
        ...(target.needsField ? [{ name: "client", label: translate("devices.addAsset.client") }] : []),
        { name: "site", label: translate("devices.addAsset.site") },
      ],
      values: { client: clientId, site: siteId },
    };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    create.mutate();
  }

  return (
    <Dialog open onClose={close} title={t("devices.addAsset.title")}>
      {/* A REAL `<form>`, WHICH IS MOST OF WHAT WAS WRONG HERE. This dialog
          was the last create form in the module that never adopted the form
          kernel (`components/form.tsx`): no `<form>`, so Enter -- the natural
          way to finish six fields -- did nothing, while the same keystroke in
          "New device group" one dialog along worked; and a hand-drawn failure
          line beside a hand-rolled submit button. */}
      <Form onSubmit={submit}>
        <p className="note mb-3">
          {t("devices.addAsset.note")}
        </p>
        <div className="form-grid">
          {target.needsField ? (
            <ClientField
              code="assets.manage"
              value={pickedClient}
              onChange={(id) => {
                setPickedClient(id);
                // Another client is another set of sites.
                setSiteId("");
              }}
            />
          ) : null}
          <SiteField clientId={clientId} value={siteId} onChange={setSiteId} />
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

            `problems` names the site, and the client with every client
            chosen: the only fields a hand-made asset cannot go without
            (design §3.2). Everything else stays optional -- a boxed laptop may
            have nothing but a description. */}
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
            problems={attemptProblems(attempt)}
            failedMessage={t("devices.addAsset.failed")}
          />
        </div>
      </Form>
    </Dialog>
  );
}
