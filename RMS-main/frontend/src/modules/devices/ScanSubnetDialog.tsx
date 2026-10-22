import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { describeSubmitError } from "../../components/form";
import { Button, Dialog, Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { Trans } from "../../i18n/Trans";
import { useDispatchIpamSweepMutation, useIpamSubnets } from "../ipam/api";
import { formatAge } from "../ipam/ipamState";
import type { SubnetOut } from "../ipam/types";

/**
 * Scan a subnet from the Discovered page, without walking to IPAM first.
 *
 * WHAT THIS IS AND IS NOT. It is an ENTRY POINT to a capability that already
 * exists in full: `POST /ipam/subnets/{id}/sweep`, the same signed `net.sweep`
 * the Sweep panel dispatches. It reuses that mutation verbatim -- it is not a
 * second dispatch path, and it cannot be, because a sweep is a signed job and
 * there is exactly one place that signs one. What it adds is the button on the
 * page an operator is already looking at when they think "I should find the
 * machines on this segment".
 *
 * IT DOES NOT RE-DECIDE AUTHORIZATION, AND IT MUST NOT LOOK LIKE IT COULD.
 * `net.sweep` puts packets on somebody's wire, so it is deny-by-default and
 * gated by an `ipam_scan_authorization` a person filed deliberately, plus a
 * designated agent that lives inside the segment (the server cannot reach into
 * a customer network -- invariant 2). `authorized_targets` recomputes the
 * authorized set server-side on every dispatch and refuses before any job row
 * is created. So this dialog does not pretend to know whether a subnet is
 * sweepable; it shows the two signals it CAN see honestly -- whether the subnet
 * has a designated agent, and when it was last swept -- names the two
 * prerequisites in a sentence, and lets the server be the authority. A refusal
 * comes back as a plain message, not a bare 403, and points at IPAM where the
 * missing grant is made.
 *
 * FRONTEND NEVER GATES SECURITY. The trigger is shown to anyone who can open
 * this page; a caller without `ipam.sweep.run` learns so from the refusal, in
 * the same place they would have learned it had the button been hidden -- and
 * hiding it would only move where they find out, not whether they may. */
export function ScanSubnetDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useT();
  const subnets = useIpamSubnets();
  const dispatch = useDispatchIpamSweepMutation();
  const [subnetId, setSubnetId] = useState("");
  const [dispatched, setDispatched] = useState<SubnetOut | null>(null);

  // IPv6 and container subnets are refused for enumeration server-side (an
  // IPv6 /64 holds 1.8e19 addresses), so a picker that offered them would only
  // teach the operator that by failing. `sweep_enabled` here is the address
  // family / kind gate the API already applies, not the authorization -- which
  // this cannot see and does not claim to.
  const options = useMemo(
    () => (subnets.data ?? []).filter((s) => s.sweep_enabled),
    [subnets.data],
  );
  const chosen = options.find((s) => s.id === subnetId) ?? null;

  function close() {
    setSubnetId("");
    setDispatched(null);
    dispatch.reset();
    onClose();
  }

  function run() {
    if (!chosen) return;
    dispatch.mutate(chosen.id, {
      onSuccess: () => setDispatched(chosen),
    });
  }

  return (
    <Dialog open={open} onClose={close} title={t("devices.scan.title")}>
      {dispatched ? (
        // THE SWEEP IS A JOB, NOT AN ANSWER. It was queued for the segment's
        // agent, which runs it on its own schedule and reports back; the rows
        // it finds are recorded automatically and land on the fleet list. So
        // the honest confirmation is "asked", with where to look, not "found".
        <div className="grid gap-3">
          <p>
            <Trans
              k="devices.scan.queued"
              vars={{ prefix: dispatched.prefix }}
              components={{ strong: <strong /> }}
            />
          </p>
          <p className="note">
            <Trans
              k="devices.scan.nothingConfirms"
              components={{ link: <Link to="/devices?provenance=sweep" onClick={close} /> }}
            />
          </p>
          {/* A BARE FLEX ROW, NOT A `.field` IN A `.form-grid`. `.field` is
              `flex-direction: column` and unlayered, so it BEATS Tailwind's
              layered `flex-row` and stacks the buttons full width -- the exact
              "cascade is the component" trap this codebase has hit before. The
              dialog footer is `flex items-center justify-end`, the way every
              other dialog renders it. */}
          <div className="flex items-center justify-end gap-2">
            <Button variant="ghost" onClick={close}>
              {t("devices.scan.done")}
            </Button>
            <Button
              variant="default"
              onClick={() => {
                setDispatched(null);
                dispatch.reset();
              }}
            >
              {t("devices.scan.another")}
            </Button>
          </div>
        </div>
      ) : (
        <div className="grid gap-3">
          <p>
            <Trans
              k="devices.scan.intro"
              components={{ strong: <strong />, link: <Link to="/ipam" onClick={close} /> }}
            />
          </p>

          <div className="form-grid">
            <Field label={t("devices.scan.subnet")} full>
              <select
                className={inputClass}
                value={subnetId}
                onChange={(event) => setSubnetId(event.target.value)}
              >
                <option value="">{t("devices.scan.choose")}</option>
                {options.map((subnet) => (
                  <option key={subnet.id} value={subnet.id}>
                    {subnet.prefix}
                    {subnet.name ? `, ${subnet.name}` : ""}
                  </option>
                ))}
              </select>
            </Field>
          </div>

          {subnets.isLoading ? (
            <p className="note">{t("devices.scan.loading")}</p>
          ) : options.length === 0 ? (
            <p className="notice gold">
              <Trans
                k="devices.scan.noSubnets"
                components={{ link: <Link to="/ipam" onClick={close} /> }}
              />
            </p>
          ) : chosen ? (
            // The two signals this dialog can honestly show. Neither is the
            // authorization -- that is the server's to check -- but a subnet
            // with no designated agent cannot be swept at all, and that is
            // worth saying before the button is pressed rather than after.
            <p className="note">
              {chosen.sweep_device_id ? t("devices.scan.hasAgent") : t("devices.scan.noAgent")}{" "}
              {chosen.last_swept_at
                ? t("devices.scan.lastSwept", { age: formatAge(chosen.last_swept_at) })
                : t("devices.scan.neverSwept")}
            </p>
          ) : null}

          {dispatch.isError ? (
            <p className="notice red" role="alert">
              {describeSubmitError(dispatch.error)}
            </p>
          ) : null}

          <div className="flex items-center justify-end gap-2">
            <Button variant="ghost" onClick={close}>
              {t("common.actions.cancel")}
            </Button>
            <Button
              variant="default"
              disabled={!chosen || dispatch.isPending}
              onClick={run}
            >
              {dispatch.isPending ? t("devices.scan.queuing") : t("devices.scan.scanNow")}
            </Button>
          </div>
        </div>
      )}
    </Dialog>
  );
}
