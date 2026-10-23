import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ConfirmDialog } from "../../components/confirm";
import { attemptProblems, describeSubmitError } from "../../components/form";
import type { Attempt } from "../../components/form";
import { Dialog, Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { Btn } from "../../shell/components";
import { ApiError } from "../../lib/api";
import {
  useDeleteIpamSubnetMutation,
  useIpamSites,
  useIpamSubnets,
  useIpamSubnetUtilization,
  useUpdateIpamSubnetMutation,
  useIpamVlans,
} from "./api";
import { gatewayRule } from "./SubnetTreePage";
import { hasUtilization } from "./ipamState";
import type { SubnetOut, SubnetUpdate } from "./types";

// EDIT AND DELETE for a registered subnet — the two things the operator filed a
// subnet and then could not do ("i create a subnet but i cannot edit it or
// delete it", 2026-08-26). Rendered on the detail page, where a single subnet
// lives, only when the caller appears to hold `ipam.manage`. That gate is
// cosmetic (invariant #6): the PUT and DELETE routes both carry
// `require_permission("ipam.manage")` and refuse regardless of what renders.
//
// NEITHER `prefix`, `vrf_id` NOR `kind` IS EDITABLE, and that is not an
// omission. `prefix` and `vrf_id` are the row's identity, and `parent_id` — the
// hierarchy — is DERIVED from containment and recomputed on every write (§0.2),
// so `SubnetUpdate` carries none of the three. The editable set is exactly what
// the API accepts: name, description, site, VLAN, gateway, DNS.
export function SubnetActions({ subnet }: { subnet: SubnetOut }) {
  const t = useT();
  const [editing, setEditing] = useState(false);

  return (
    <>
      <Btn label={t("ipam.fields.edit")} icon="edit" onClick={() => setEditing(true)} />
      <DeleteSubnetControl subnet={subnet} />
      {editing && <EditSubnetDialog subnet={subnet} onClose={() => setEditing(false)} />}
    </>
  );
}

function EditSubnetDialog({ subnet, onClose }: { subnet: SubnetOut; onClose: () => void }) {
  const t = useT();
  const update = useUpdateIpamSubnetMutation();
  const sites = useIpamSites();
  const vlans = useIpamVlans();
  const [name, setName] = useState(subnet.name);
  const [description, setDescription] = useState(subnet.description);
  const [siteId, setSiteId] = useState(subnet.site_id ?? "");
  const [vlanId, setVlanId] = useState(subnet.vlan_id ?? "");
  const [gateway, setGateway] = useState(subnet.gateway ?? "");
  const [dns, setDns] = useState(subnet.dns_servers.join(", "));
  // Populated ON SUBMIT, never while typing — an unfinished gateway is not yet
  // wrong, and a form that says so mid-keystroke is its own kind of noise. Held
  // as the rules and the values they refused, and worded at render, so a
  // language switch re-words a refusal that is already on screen.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  function submit() {
    const snapshot = gateway.trim()
      ? { rules: () => [gatewayRule()], values: { gateway } }
      : null;
    setAttempt(snapshot);
    if (snapshot !== null && attemptProblems(snapshot).length > 0) return;
    const body: SubnetUpdate = {
      name: name.trim(),
      description: description.trim(),
      site_id: siteId || null,
      vlan_id: vlanId || null,
      gateway: gateway.trim() || null,
      // Split on commas or whitespace, dropping the empties a trailing comma
      // leaves — an empty box clears DNS, which is a legitimate edit.
      dns_servers: dns
        .split(/[\s,]+/)
        .map((entry) => entry.trim())
        .filter((entry) => entry !== ""),
    };
    update.mutate({ id: subnet.id, body }, { onSuccess: onClose });
  }

  const problems = attemptProblems(attempt);
  const error = update.error ?? (problems[0]?.message ? new Error(problems[0].message) : null);

  return (
    <Dialog open onClose={onClose} title={t("ipam.subnetActions.editTitle", { prefix: subnet.prefix })}>
      <p className="note" style={{ marginBottom: 14 }}>
        {t("ipam.subnetActions.editNote")}
      </p>
      <div className="form-grid">
        <Field label={t("ipam.fields.name")}>
          <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label={t("ipam.fields.gateway")}>
          <input
            className={inputClass}
            placeholder="10.4.7.1"
            value={gateway}
            onChange={(e) => setGateway(e.target.value)}
          />
        </Field>
        <Field label={t("ipam.fields.site")}>
          <select className={inputClass} value={siteId} onChange={(e) => setSiteId(e.target.value)}>
            <option value="">{t("ipam.fields.none")}</option>
            {sites.data?.map((site) => (
              <option key={site.id} value={site.id}>
                {site.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t("ipam.fields.vlan")}>
          <select className={inputClass} value={vlanId} onChange={(e) => setVlanId(e.target.value)}>
            <option value="">{t("ipam.fields.none")}</option>
            {vlans.data?.map((vlan) => (
              <option key={vlan.id} value={vlan.id}>
                {t("ipam.vlan.named", { vid: vlan.vid, name: vlan.name })}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t("ipam.subnetActions.dnsServers")} full>
          <input
            className={inputClass}
            placeholder="10.4.7.1, 10.4.7.2"
            value={dns}
            onChange={(e) => setDns(e.target.value)}
          />
        </Field>
        <Field label={t("ipam.fields.description")} full>
          <input
            className={inputClass}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </Field>
      </div>

      {error && (
        <p role="alert" className="form-error" style={{ marginTop: 12 }}>
          {describeSubmitError(error)}
        </p>
      )}

      <div className="form-actions" style={{ justifyContent: "flex-end" }}>
        <Btn label={t("common.actions.cancel")} variant="ghost" onClick={onClose} />
        <Btn
          label={update.isPending ? t("ipam.subnetActions.saving") : t("ipam.subnetActions.save")}
          icon="check"
          variant="primary"
          disabled={update.isPending}
          onClick={submit}
        />
      </div>
    </Dialog>
  );
}

// DELETING IS NOT ONE ACT. An empty subnet is removed outright; one that still
// holds addresses, ranges or sweep history is REFUSED by the server (409) until
// `cascade` is asked for, because the two are different decisions (§0.4): a
// cascade detaches the address rows as orphans — their evidence survives — and
// deletes the subnet's ranges and sweep runs. So the safe delete is tried
// first, and the destructive one is reached only through a second confirmation
// that carries the server's own count of what it would release.
function DeleteSubnetControl({ subnet }: { subnet: SubnetOut }) {
  const t = useT();
  const navigate = useNavigate();
  const remove = useDeleteIpamSubnetMutation();
  const subnets = useIpamSubnets();
  const utilization = useIpamSubnetUtilization(subnet.id, hasUtilization(subnet));
  const [confirming, setConfirming] = useState(false);
  // The server's refusal, held so the cascade confirmation can restate exactly
  // what it would release rather than a guess assembled on the client. Held as
  // the failure and worded at render: a sentence kept here would stay in the
  // language it was written in.
  const [conflict, setConflict] = useState<{ error: unknown } | null>(null);

  const children = (subnets.data ?? []).filter((row) => row.parent_id === subnet.id).length;
  const util = utilization.data;
  const documented = util ? util.assigned + util.reserved + util.deprecated : 0;
  const observed = util?.observed ?? 0;

  // What we can SEE holds inside it, in the numbers already on the page. The
  // server is the authority on whether cascade is required (it also counts
  // ranges, drift and sweep runs); this only tells the operator what to expect.
  const knownImpact = (() => {
    const parts: string[] = [];
    if (documented > 0) parts.push(t("ipam.subnetActions.documented", { count: documented }));
    if (observed > 0) parts.push(t("ipam.subnetActions.observed", { count: observed }));
    if (children > 0) parts.push(t("ipam.subnetActions.children", { count: children }));
    if (parts.length === 0) return t("ipam.subnetActions.impactNothing");
    return t("ipam.subnetActions.impactSome", { parts: parts.join(", ") });
  })();

  function attemptDelete(cascade: boolean) {
    remove.mutate(
      { id: subnet.id, cascade },
      {
        onSuccess: () => navigate("/ipam"),
        onError: (err) => {
          // A 409 is "not empty, and cascade was not asked for" — escalate to
          // the explicit cascade confirmation. Any other failure is shown as a
          // plain message and nothing escalates.
          if (err instanceof ApiError && err.status === 409) {
            setConflict({ error: err });
          }
        },
      },
    );
  }

  const escalating = conflict !== null;
  // The first refusal is handled by the cascade dialog, so it is not ALSO shown
  // as a red error line; a later failure (including a failed cascade) is.
  const showError = remove.isError && !escalating;

  return (
    <>
      <Btn
        label={t("ipam.fields.delete")}
        icon="trash"
        variant="danger"
        onClick={() => {
          remove.reset();
          setConflict(null);
          setConfirming(true);
        }}
      />

      {/* The safe delete. Non-cascade: an empty subnet goes; a non-empty one is
          refused and escalates below. */}
      <ConfirmDialog
        open={confirming}
        title={t("ipam.subnetActions.deleteTitle", { prefix: subnet.prefix })}
        subject={subnet.prefix}
        body={t("ipam.subnetActions.deleteBody", { impact: knownImpact })}
        confirmLabel={t("ipam.subnetActions.deleteConfirm")}
        destructive
        onCancel={() => setConfirming(false)}
        onConfirm={() => {
          setConfirming(false);
          attemptDelete(false);
        }}
      />

      {/* The escalation, reached only when the server refused the safe delete.
          It carries the server's own accounting of what cascade releases. */}
      <ConfirmDialog
        open={escalating}
        title={t("ipam.subnetActions.cascadeTitle", { prefix: subnet.prefix })}
        subject={subnet.prefix}
        body={t("ipam.subnetActions.cascadeBody", {
          reason: conflict === null ? "" : describeSubmitError(conflict.error),
        })}
        confirmLabel={t("ipam.subnetActions.cascadeConfirm")}
        destructive
        onCancel={() => {
          setConflict(null);
          remove.reset();
        }}
        onConfirm={() => {
          setConflict(null);
          attemptDelete(true);
        }}
      />

      {showError && (
        <p role="alert" className="form-error">
          {t("ipam.subnetActions.deleteFailed", { reason: describeSubmitError(remove.error) })}
        </p>
      )}
    </>
  );
}
