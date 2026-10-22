// Promote a device to site server.
//
// TWO RENDERING RULES, EACH OF WHICH EXISTS BECAUSE THE ALTERNATIVE IS A BUG
// REPORT:
//
//   * ONLY `wake` IS OFFERED. `cache` and `pxe` are storable in the database --
//     that vocabulary is the forward seam for the deferred slices -- and are
//     REFUSED BY THE SERVICE with a 400 naming spec §4.4, whose invariant
//     amendment has not been accepted. Offering an option the server rejects
//     wastes an operator's afternoon and teaches them the UI lies.
//   * A DEVICE WITH NO AGENT IS SHOWN AND DISABLED, WITH THE REASON, rather
//     than silently absent. Promotion refuses it (a site server receives its
//     work as an ordinary signed job on its own mutual-TLS connection, so a
//     device with no agent can never be sent one) -- and an operator looking for
//     a machine they can see in the device list needs to be told WHY it is not
//     here, not left to conclude the page is broken.
//
// The dialog gates nothing. Invariant #6: the server re-checks `site.manage` on
// every request.

import { useState } from "react";
import { Button, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { lazyLabels } from "../../i18n/lazy";
import type { Device } from "../../lib/api";
import { OFFERABLE_ROLES } from "./types";
import type { SiteServerCreate, SiteServerRole } from "./types";

/**
 * The word an operator reads beside the role checkbox.
 *
 * Auditor A (MEDIUM), /site-servers -> "Promote a device to site server":
 * "the checkbox's label is the raw lowercase string `wake`". `wake` is the
 * wire value the service stores and validates; it is not a sentence, and a
 * checkbox in a dialog that promotes a machine to infrastructure has to say
 * what ticking it does. The value posted is unchanged -- only the rendering.
 *
 * `humaniseEnum` is not used here on purpose: it would produce "Wake", which
 * is neither the wire value nor an explanation. The map is exhaustive over
 * `OFFERABLE_ROLES` by type, so adding a role that has no sentence here is a
 * compile error rather than a lowercase enum leaking onto the screen again.
 */
const ROLE_LABELS: Record<SiteServerRole, string> = lazyLabels<SiteServerRole>({
  wake: "siteServers.promote.enableWake",
});

export function PromoteDialog({
  devices,
  promotedDeviceIds,
  onSubmit,
  onCancel,
  error,
}: {
  devices: Device[];
  /** Devices already promoted -- a device is a site server at most once. */
  promotedDeviceIds?: string[];
  onSubmit: (body: SiteServerCreate) => void;
  /**
   * The non-destructive way out. Auditor A: "there is *no* Cancel button,
   * unlike every other dialog in the product ... with no Escape handler the
   * only way out is the icon button." REQUIRED, not optional: an optional
   * prop is how the missing Cancel comes back without anything going red.
   */
  onCancel: () => void;
  error?: { message?: string } | null;
}) {
  const t = useT();
  const [deviceId, setDeviceId] = useState("");
  const [segments, setSegments] = useState("");
  const alreadyPromoted = new Set(promotedDeviceIds ?? []);

  return (
    <form
      aria-label={t("siteServers.promote.form")}
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit({
          device_id: deviceId,
          segments: segments
            .split(",")
            .map((value) => value.trim())
            .filter(Boolean),
          roles: ["wake"],
        });
      }}
    >
      <div className="form-grid">
        <div className="field full">
          <label htmlFor="promote-device">{t("siteServers.promote.device")}</label>
          <select
            id="promote-device"
            className={inputClass}
            value={deviceId}
            onChange={(event) => setDeviceId(event.target.value)}
          >
            <option value="">{t("siteServers.promote.select")}</option>
            {devices.map((device) => {
              const noAgent = device.agent_status === null;
              const promoted = alreadyPromoted.has(device.id);
              const blocked = noAgent || promoted;
              return (
                <option
                  key={device.id}
                  value={device.id}
                  disabled={blocked}
                  data-testid={`device-option-${device.id}`}
                >
                  {device.hostname}
                  {noAgent ? t("siteServers.promote.noAgent") : ""}
                  {promoted && !noAgent ? t("siteServers.promote.alreadyPromoted") : ""}
                </option>
              );
            })}
          </select>
        </div>

        <div className="field full">
          <label htmlFor="promote-segments">{t("siteServers.promote.segments")}</label>
          <input
            id="promote-segments"
            className={inputClass}
            value={segments}
            onChange={(event) => setSegments(event.target.value)}
            placeholder="10.20.0.0/16, 10.20.7.0/24"
          />
        </div>

        {/* A `role="group"`, not a `<fieldset>`: the browser's default fieldset
            border is a system colour outside the token set, and the group
            semantics are what a screen reader needs from it. */}
        <div className="field full" role="group" aria-labelledby="promote-roles-label">
          <span id="promote-roles-label">{t("siteServers.promote.roles")}</span>
          {OFFERABLE_ROLES.map((role) => (
            <label
              key={role}
              htmlFor={`role-${role}`}
              style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 400 }}
            >
              <input id={`role-${role}`} type="checkbox" checked readOnly value={role} />
              {ROLE_LABELS[role]}
            </label>
          ))}
          <p className="help" data-testid="roles-note">
            {t("siteServers.promote.rolesNote")}
          </p>
        </div>
      </div>

      {error ? (
        <p role="alert" className="form-error">
          {error.message ?? t("siteServers.promote.failed")}
        </p>
      ) : null}
      {/* Cancel FIRST and `type="button"`: an untyped button inside a form
          submits it, which would promote the device instead of backing out --
          the exact inversion the audit's missing-Cancel finding invites.
          `Button` defaults to `type="button"` for exactly that reason, so the
          inversion is now the primitive's problem rather than this file's. */}
      <div className="form-actions">
        <Button variant="ghost" onClick={onCancel}>
          {t("siteServers.promote.cancel")}
        </Button>
        <Button type="submit">{t("siteServers.promote.submit")}</Button>
      </div>
    </form>
  );
}
