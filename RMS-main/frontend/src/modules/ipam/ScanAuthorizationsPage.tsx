import { useState } from "react";
import { ConfirmDialog } from "../../components/confirm";
import { NoValue } from "../../components/emptystate";
import { RowMenu } from "../../components/rowmenu";
import { Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { Trans } from "../../i18n/Trans";
import {
  useCreateIpamScanAuthorizationMutation,
  useIpamPermissions,
  useIpamSubnets,
  useIpamVrfs,
  useIpamScanAuthorizations,
  useRevokeIpamScanAuthorizationMutation,
  useUserTimezone,
} from "./api";
import { ErrorLine } from "./ErrorLine";
import { IpamNav } from "./IpamNav";
import {
  describeScope,
  formatAge,
  formatCount,
  sweepScopeGrantMeaning,
  sweepScopeSafeguard,
  parsePrefix,
  prefixContains,
  prefixSize,
  SCOPE_STATUS_PRESENTATION,
  scopeStatus,
} from "./ipamState";
import { variantKind } from "./kinds";
import type { ScopeOut, SubnetOut } from "./types";
import { formatDate, toInstant } from "../../lib/datetime";
import { Icon } from "../../shell/icons";
import {
  Btn,
  FilterBar,
  PageHead,
  Panel,
  QueryState,
  StatusPill,
} from "../../shell/components";

// THE MODULE'S CONTROL SURFACE.
//
// ON THE v3 KIT (`P['ipam/scan-authorizations']` in
// docs/design/itms-prototype-v3.html): the head with its one primary action,
// one bare panel of rows led by the shield glyph, the prefix over who
// authorised it and when, the state as a pill. The grant form opens from the
// head's button, as the design draws it.
//
// `net.sweep` is the first capability in this platform that puts packets on
// SOMEBODY ELSE'S WIRE, and a row in this table is what decides where. So this
// page is built around one question, what does this actually permit?, rather
// than around the CRUD of it:
//
//   * EVERY ROW STATES ITS REACH IN ADDRESSES. "10.0.0.0/8" is four characters
//     and 16,777,216 addresses, and an operator scanning a list of prefixes
//     does not convert mask lengths in their head. Misreading a scope is how
//     packets end up on a segment nobody meant to touch.
//   * THE CREATE FORM PREVIEWS THE GRANT BEFORE IT IS MADE, including which
//     registered subnets fall inside it. Deny-by-default only helps if the
//     person granting knows what they are opening.
//   * REVOCATION STAMPS, IT DOES NOT DELETE. Revoked rows stay visible on
//     request so "who authorised probing that segment in March" stays
//     answerable.
//   * DOCUMENTING A SUBNET IS NOT AUTHORISING IT. Stated on the page, because
//     the assumption "we documented it, so presumably we own it" is exactly
//     what deny-by-default exists to refuse.
//
// This page renders `ipam.scope.manage` controls only when the caller appears
// to hold the code. That is cosmetic: the server refuses regardless, and it is
// the only thing that refuses.
export function ScanAuthorizationsPage() {
  const t = useT();
  const permissions = useIpamPermissions();
  const [includeRevoked, setIncludeRevoked] = useState(false);
  const [granting, setGranting] = useState(false);
  const [revoking, setRevoking] = useState<{ id: string; prefix: string } | null>(null);
  const scopes = useIpamScanAuthorizations(includeRevoked);
  const subnets = useIpamSubnets();
  const revoke = useRevokeIpamScanAuthorizationMutation();

  return (
    <div>
      <PageHead
        title={t("ipam.scans.title")}
        description={t("ipam.scans.description")}
        actions={
          permissions.scopeManage ? (
            <Btn
              label={t("ipam.scans.authorizeSubnet")}
              icon="plus"
              variant="primary"
              onClick={() => setGranting(true)}
            />
          ) : undefined
        }
      />
      <IpamNav />

      <Panel bare>
        <FilterBar>
          <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <input
              type="checkbox"
              checked={includeRevoked}
              onChange={(e) => setIncludeRevoked(e.target.checked)}
            />
            {t("ipam.scans.showRevoked")}
          </label>
        </FilterBar>
        {/* WHAT A GRANT MEANS, AND THE SAFEGUARD, ONCE EACH. Both are the same
            sentences under every prefix, and in a table cell they made each row
            about 500px tall at 390px, pushing the state, the note and the row's
            only action out of the viewport behind a horizontal scroll — and
            then, still outside that viewport, went on setting a row height with
            nothing visible in it. Stated here they are read before the first
            row and cost no row height. What a row does NOT share with its
            neighbours stays in the row. */}
        <p className="note" style={{ padding: "12px 18px 0", margin: 0 }}>
          {sweepScopeGrantMeaning()} {sweepScopeSafeguard()}
          {/* DENY BY DEFAULT, WHERE IT IS ACTED ON. It was in the page
              description; the operator who cannot see the grant button is the
              one who needs it, and the separation of the two permissions is
              why the button is absent. */}
          {permissions.scopeManage ? null : (
            <>
              {" "}
              {t("ipam.scans.scopeManageNote")}
            </>
          )}
        </p>
        <QueryState
          loading={scopes.isLoading}
          error={scopes.error}
          empty={(scopes.data ?? []).length === 0}
          emptyLabel={t("ipam.scans.empty")}
          emptyIcon="shield"
        >
          <Table>
            <thead>
              <tr>
                {/* STATE BEFORE THE PROSE, as v3 draws this row (its cells
                    are `[state(...), ...]` right after the title). It is also
                    what survives a narrow viewport: at 390px the table scrolls
                    inside its card, and the column an operator most needs to
                    read was the one cut in half by the fold. */}
                <Th>{t("ipam.scans.colPrefix")}</Th>
                <Th>{t("ipam.scans.colState")}</Th>
                <Th>{t("ipam.scans.colPermits")}</Th>
                <Th>{t("ipam.scans.colNote")}</Th>
                {permissions.scopeManage && <Th>{t("ipam.fields.actions")}</Th>}
              </tr>
            </thead>
            <tbody>
              {scopes.data?.map((scope) => (
                <ScopeRow
                  key={scope.id}
                  scope={scope}
                  subnets={subnets.data ?? []}
                  canManage={permissions.scopeManage}
                  onRevoke={() => setRevoking({ id: scope.id, prefix: scope.prefix })}
                />
              ))}
            </tbody>
          </Table>
        </QueryState>
        {revoke.isError && (
          <div className="panel-b">
            <ErrorLine error={revoke.error} />
          </div>
        )}
      </Panel>

      <ConfirmDialog
        open={revoking !== null}
        title={t("ipam.scans.revokeTitle")}
        body={t("ipam.scans.revokeBody")}
        subject={revoking?.prefix ?? ""}
        confirmLabel={t("ipam.fields.revoke")}
        destructive
        onConfirm={() => {
          if (revoking) revoke.mutate(revoking.id);
          setRevoking(null);
        }}
        onCancel={() => setRevoking(null)}
      />

      {permissions.scopeManage && (
        <Dialog open={granting} onClose={() => setGranting(false)} title={t("ipam.scans.grantTitle")}>
          <GrantForm subnets={subnets.data ?? []} />
        </Dialog>
      )}
    </div>
  );
}

function ScopeRow({
  scope,
  subnets,
  canManage,
  onRevoke,
}: {
  scope: ScopeOut;
  subnets: SubnetOut[];
  canManage: boolean;
  onRevoke: () => void;
}) {
  const t = useT();
  const description = describeScope(scope);
  const presentation = SCOPE_STATUS_PRESENTATION[description.status];
  const parsed = parsePrefix(scope.prefix);
  const covered = subnets.filter((subnet) => {
    if (subnet.vrf_id !== scope.vrf_id || parsed === null) return false;
    const subnetPrefix = parsePrefix(subnet.prefix);
    return subnetPrefix !== null && prefixContains(parsed, subnetPrefix);
  });
  const active = scopeStatus(scope) === "active";

  return (
    <tr>
      <Td lead={<Icon name="shield-check" size={18} />} gutter={active ? "warn" : undefined}>
        <div className="cell-title mono">{scope.prefix}</div>
        <div className="cell-sub">
          {description.addressCount !== null ? (
            <>
              <span>
                {t("ipam.scans.addresses", {
                  total: description.addressCount,
                  count: description.addressCount === "1" ? 1 : 2,
                })}
              </span>
              {" · "}
            </>
          ) : null}
          <Trans
            k="ipam.scans.authorizedBy"
            vars={{ age: formatAge(scope.authorized_at), by: scope.authorized_by }}
            components={{ mono: <span className="mono" /> }}
          />
        </div>
      </Td>
      <Td>
        <span title={presentation.meaning}>
          <StatusPill
            value={description.status}
            kind={variantKind(presentation.variant)}
            label={presentation.label}
          />
        </span>
        <div className="note" style={{ marginTop: 4 }}>
          {description.expiry}
        </div>
      </Td>
      {/* ONLY WHAT THIS ROW DOES NOT SHARE WITH ITS NEIGHBOURS.
          An active grant's reach was spelled out here per row — "Sweeps may put
          ICMP/ARP packets on any address inside 10.20.4.0/24, 256 addresses, in
          this VRF" — beside a Prefix cell already reading "10.20.4.0/24 · 256
          addresses". Four wrapped lines saying the neighbour's words back. The
          sentence is now above the table once; what varies is which registered
          subnets fall inside the prefix, and that stays. An INACTIVE row keeps
          its own summary, because "it covered this while it was active" is a
          fact about that row and about no other. */}
      <Td>
        {active ? (
          covered.length > 0 ? (
            <p className="note">
              <Trans
                k="ipam.scans.covers"
                vars={{ count: covered.length, prefixes: covered.map((subnet) => subnet.prefix).join(", ") }}
                components={{ mono: <span className="mono" /> }}
              />
            </p>
          ) : (
            <p className="note">{t("ipam.scans.coversNone")}</p>
          )
        ) : (
          <p className="note" style={{ maxWidth: "36ch" }}>
            {description.summary}
          </p>
        )}
      </Td>
      <Td>{scope.note || <NoValue label={t("ipam.scans.noNote")} />}</Td>
      {canManage && (
        <Td>
          {active ? (
            <RowMenu
              subject={scope.prefix}
              items={[{ label: t("ipam.fields.revoke"), destructive: true, onSelect: onRevoke }]}
            />
          ) : (
            <NoValue label={t("ipam.scans.inactive")} />
          )}
        </Td>
      )}
    </tr>
  );
}

function GrantForm({ subnets }: { subnets: SubnetOut[] }) {
  const t = useT();
  const create = useCreateIpamScanAuthorizationMutation();
  const zone = useUserTimezone();
  const vrfs = useIpamVrfs();
  const [prefix, setPrefix] = useState("");
  const [vrfId, setVrfId] = useState("");
  const [note, setNote] = useState("");
  const [expiresAt, setExpiresAt] = useState("");

  const parsed = parsePrefix(prefix);
  // Which registered subnet, if any, the Prefix currently names, so the picker
  // shows it selected and falls back to "type a prefix" the moment the operator
  // edits the box to something no subnet matches.
  const selectedSubnetId =
    subnets.find(
      (subnet) => subnet.prefix === prefix && (vrfId === "" || subnet.vrf_id === vrfId),
    )?.id ?? "";
  // The preview: how many addresses, and which registered subnets fall inside.
  // A grant nobody can size is a grant nobody can review.
  const covered =
    parsed === null
      ? []
      : subnets.filter((subnet) => {
          if (vrfId !== "" && subnet.vrf_id !== vrfId) return false;
          const subnetPrefix = parsePrefix(subnet.prefix);
          return subnetPrefix !== null && prefixContains(parsed, subnetPrefix);
        });

  return (
    <div>
      <p className="note" style={{ marginBottom: 14 }}>
        {t("ipam.scans.grantNote")}
      </p>

      <div className="form-grid">
        {/* AUTHORISE A SUBNET YOU ALREADY FILED WITHOUT RETYPING ITS PREFIX
            (operator, 2026-08-26: "double work there"). Picking a registered
            subnet fills the Prefix below and matches its VRF. It does NOT
            grant anything, because filing a subnet is deliberately not
            authorising a scan of it (§0.12). The free-typed Prefix stays: a
            scan authorization is a prefix, not a subnet row, so a scope that is
            not a registered subnet is authorised by typing it. */}
        <Field label={t("ipam.scans.fillFrom")} full>
          <select
            className={inputClass}
            aria-label={t("ipam.scans.fillFrom")}
            value={selectedSubnetId}
            onChange={(e) => {
              const chosen = subnets.find((subnet) => subnet.id === e.target.value);
              if (!chosen) return;
              setPrefix(chosen.prefix);
              // Match the subnet's VRF so the grant actually covers it. A
              // named VRF is selectable; the implicit global default (a
              // vrf_id absent from the list) maps back to "Default (global)".
              setVrfId(vrfs.data?.some((vrf) => vrf.id === chosen.vrf_id) ? chosen.vrf_id : "");
            }}
          >
            <option value="">{t("ipam.scans.typePrefix")}</option>
            {subnets.map((subnet) => (
              <option key={subnet.id} value={subnet.id}>
                {subnet.prefix}
                {subnet.name ? ` (${subnet.name})` : ""}
              </option>
            ))}
          </select>
          {/* THE DENY-BY-DEFAULT SENTENCE, ON THE CONTROL THAT INVITES THE
              ASSUMPTION. Filing a subnet is deliberately not authorising a
              scan of it; this picker is where somebody would expect otherwise. */}
          <p className="note" style={{ marginTop: 6 }}>
            {t("ipam.scans.fillNote")}
          </p>
        </Field>
        <Field label={t("ipam.fields.prefix")}>
          <input
            className={inputClass}
            placeholder="10.4.7.0/24"
            value={prefix}
            onChange={(e) => setPrefix(e.target.value)}
          />
        </Field>
        <Field label={t("ipam.fields.vrf")}>
          <select className={inputClass} value={vrfId} onChange={(e) => setVrfId(e.target.value)}>
            <option value="">{t("ipam.fields.defaultGlobal")}</option>
            {vrfs.data?.map((vrf) => (
              <option key={vrf.id} value={vrf.id}>
                {vrf.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t("ipam.scans.noteWhy")}>
          <input className={inputClass} value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>
        <Field label={t("ipam.scans.expiresOptional")}>
          <input
            className={inputClass}
            type="date"
            value={expiresAt}
            onChange={(e) => setExpiresAt(e.target.value)}
          />
          {/* THE DATE, READ BACK IN THE CONSOLE'S OWN FORMAT, in the same
              words as the push grant on `/agent-push/authorizations` --
              which is the same decision about the same kind of expiry, and
              was the one page in the console already drawing this echo. A
              native date input draws the USER AGENT'S mask and no CSS
              reorders it, so this field reads `09/22/2026` to a US-locale
              browser on a console that renders every other date
              `22 Sept 2026` (design review M1). */}
        </Field>
      </div>
      {expiresAt !== "" ? (
        <p className="note" style={{ marginTop: 14 }}>
          {t("ipam.scans.lapses", { date: formatDate(toInstant(`${expiresAt}T00:00`, zone), zone) })}
        </p>
      ) : null}

      {parsed !== null && (
        <div className="notice gold" style={{ marginTop: 14 }}>
          <Icon name="alert-triangle" size={16} />
          {/* ONE CHILD, because `.notice` is a flex row shaped icon-then-body.
              Three stacked paragraphs handed to it directly would lay out as
              three columns. */}
          <div>
            <p>
              <b>
                {t("ipam.scans.probes", {
                  total: formatCount(prefixSize(parsed)),
                  count: prefixSize(parsed) === 1n ? 1 : 2,
                })}
              </b>
            </p>
            <p style={{ marginTop: 4 }}>
              {covered.length === 0
                ? t("ipam.scans.coveredNone")
                : t("ipam.scans.coveredSome", {
                    count: covered.length,
                    list: covered.map((subnet) => subnet.prefix).join(", "),
                  })}
            </p>
            {expiresAt === "" && (
              <p style={{ marginTop: 4 }}>{t("ipam.scans.noExpiryNotice")}</p>
            )}
          </div>
        </div>
      )}
      {prefix.trim() !== "" && parsed === null && (
        <p className="form-error" style={{ marginTop: 10 }}>
          {t("ipam.scans.notPrefix")}
        </p>
      )}

      <div className="form-actions">
        <Btn
          label={t("ipam.scans.authorize")}
          icon="shield-check"
          variant="primary"
          disabled={create.isPending || parsed === null}
          onClick={() =>
            create.mutate(
              {
                prefix,
                vrf_id: vrfId || null,
                note,
                // THE FIELD IS A WALL CLOCK; the API stores an instant.
                // `new Date("2026-08-25").toISOString()` reads it as UTC
                // midnight, so an operator at UTC-5 gets a grant that expires
                // five hours before the date they typed begins. This one is
                // security-critical in the other direction too: east of UTC
                // the same arithmetic EXTENDS the window past the date on the
                // screen, and a scan authorization is deny-by-default
                // precisely so that nobody has to guess when it lapses.
                expires_at: expiresAt === "" ? null : toInstant(`${expiresAt}T00:00`, zone),
              },
              {
                onSuccess: () => {
                  setPrefix("");
                  setNote("");
                  setExpiresAt("");
                },
              },
            )
          }
        />
        {create.isSuccess && (
          <span className="form-saved" role="status">
            {t("ipam.scans.authorized")}
          </span>
        )}
        {create.isError && <ErrorLine error={create.error} />}
      </div>
    </div>
  );
}
