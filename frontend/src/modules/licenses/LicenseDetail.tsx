import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ConfirmDialog } from "../../components/confirm";
import { NoValue } from "../../components/emptystate";
import { describeSubmitError } from "../../components/form";
import { Button, Dialog, Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDate, formatDateTime } from "../../lib/datetime";
import { humaniseEnum } from "../../lib/labels";
import {
  useAllocations,
  useDeleteLicenseMutation,
  useLicense,
  useOrgUsers,
  useProduct,
  useRevealLicenseKeyMutation,
} from "./api";
import { licenseTypeKind } from "./licenseState";
import { initialsOf, personIndex } from "../../lib/people";
import { PageHead, Panel, QueryState, StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { KV, Person } from "../../shell/viz";
import { PersonLink } from "../people/links";

// The revealed key lives only in local component state (`revealed`) and is
// never written into a react-query cache -- shown once and cleared on close.
// Mirrors modules/credentials/CredentialsPage.tsx's checkout/reveal pattern.
export function LicenseDetail() {
  const t = useT();
  const { licenseId } = useParams<{ licenseId: string }>();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  // "Reveal key" is HIDDEN entirely without licenses.keys.read (server-side
  // RBAC is authoritative; hiding here is purely cosmetic).
  const canReveal = (me.data?.permissions ?? []).includes("licenses.keys.read");
  const canManage = (me.data?.permissions ?? []).includes("licenses.manage");
  const navigate = useNavigate();
  // The operator's own zone (cycle 5). `/me` is already fetched on this route
  // for the permission check, so this costs no request; a null timezone falls
  // the helper back to the browser zone.
  const zone = me.data?.timezone;

  const license = useLicense(licenseId);
  const product = useProduct(license.data?.product_id);
  // Names for the user allocations below. A row that names a person shows the
  // person; the id is the fallback, never a blank, because an allocation that
  // HAS a target must not read as if it had none.
  const users = useOrgUsers();
  const userNames = new Map(
    (users.data?.users ?? []).map((user) => [user.id, user.display_name || user.email] as const),
  );
  const allocations = useAllocations(licenseId);
  const reveal = useRevealLicenseKeyMutation();
  // Cycle 7. `DELETE /licenses/{license_id}` has existed since the module
  // shipped (licenses/api.py's `delete_license_route`) and
  // `useDeleteLicenseMutation` was already written against it -- no page ever
  // called either, so a license entered against the wrong product could only
  // be corrected by someone with database access. Wired to the route that
  // exists; nothing invented.
  const remove = useDeleteLicenseMutation();

  const [revealed, setRevealed] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  // `navigator.clipboard.writeText` REJECTS on a document that is not focused
  // and on a denied permission, and the `void` that used to sit here threw
  // that away: the operator was told nothing, saw "Copy" and closed a dialog
  // that shows the key exactly once. Both outcomes are now reported. Held as
  // the outcome (and the caught value), worded at render, so a language
  // switch re-words it.
  const [copy, setCopy] = useState<
    { kind: "copied" } | { kind: "blocked" } | { kind: "failed"; error: unknown } | null
  >(null);

  if (!licenseId) return null;

  return (
    <div>
      <QueryState
        loading={license.isLoading}
        error={license.error}
        empty={!license.isLoading && !license.error && license.data == null}
        emptyLabel={t("licenses.detail.notFound")}
      >
        <PageHead
          title={product.data?.name ?? t("licenses.detail.titleFallback")}
          description={t("licenses.detail.description")}
          actions={
            <>
              {canReveal && license.data?.has_key && (
                <Button
                  disabled={reveal.isPending}
                  onClick={() =>
                    reveal.mutate(licenseId, {
                      onSuccess: (data) => setRevealed(data.key),
                    })
                  }
                >
                  {reveal.isPending ? t("licenses.detail.revealing") : t("licenses.detail.reveal")}
                </Button>
              )}
              {canManage && license.data && (
                <Button
                  variant="outline"
                  disabled={remove.isPending}
                  onClick={() => setConfirmingDelete(true)}
                >
                  {remove.isPending ? t("licenses.detail.deleting") : t("licenses.detail.delete")}
                </Button>
              )}
            </>
          }
        />

        {/* THE STATE BELONGS UNDER THE TITLE, NOT IN THE BUTTON ROW.

            The licence-type pill used to be the first child of `actions`, so
            `.ph-act` -- a row whose whole job is "the things you can press
            here" -- opened with something that cannot be pressed, and the
            reader met "Per user" between the heading and "Reveal key" (design
            review M14). Every other record page in this console puts its state
            in a pill row directly under the head: /service-desk/tickets/:id,
            /portal/tickets/:id, /devices/:id. This is that row, with the same
            three utilities and the same negative top margin pulling it under
            the description. */}
        {license.data && (
          <div className="flex flex-wrap items-center gap-2" style={{ margin: "-8px 0 14px" }}>
            <StatusPill
              value={license.data.type}
              label={humaniseEnum(license.data.type, "LicenseType")}
              kind={licenseTypeKind(license.data.type)}
            />
          </div>
        )}

        {/* The subject is the product name and the entitlement count, because
            an estate carries several licenses for one product and the count is
            what tells them apart on this screen. Typing is NOT required: a
            license row is re-creatable from the purchase paperwork, so this is
            irreversible but not wide, and `requireTyping` everywhere trains
            people to copy words without reading them. */}
        <ConfirmDialog
          open={confirmingDelete}
          title={t("licenses.detail.delete")}
          subject={
            license.data
              ? t("licenses.detail.deleteSubject", {
                  name: product.data?.name ?? t("licenses.detail.thisLicense"),
                  count: license.data.entitlements,
                })
              : ""
          }
          body={t("licenses.detail.deleteBody")}
          confirmLabel={t("licenses.detail.delete")}
          destructive
          onCancel={() => setConfirmingDelete(false)}
          onConfirm={() => {
            setConfirmingDelete(false);
            remove.mutate(licenseId, {
              // NAVIGATION ONLY ON SUCCESS. Leaving on click would be the
              // audit's swallowed-503: the row appears gone because the page
              // changed, not because the server agreed. A failure keeps the
              // operator here, where the alert below is.
              onSuccess: () => navigate("/licenses"),
            });
          }}
        />

        {remove.isError && (
          <p role="alert" className="notice red">
            <Icon name="circle-x" size={16} />
            <span>{describeSubmitError(remove.error)}</span>
          </p>
        )}

        {/* `String(error)` printed "ApiError: " with nothing after it for a
            bodyless 503 -- and rendered no live region at all, so a screen
            reader was told nothing. `describeSubmitError` has a sentence for
            every status; `role="alert"` is what announces it. */}
        {reveal.isError && (
          <p role="alert" className="notice red">
            <Icon name="circle-x" size={16} />
            <span>{describeSubmitError(reveal.error)}</span>
          </p>
        )}

        {license.data && (
          <Panel title={t("licenses.detail.details")}>
            {/* v3's `kv`: caption on the left, value on the right. Money is a
                Decimal on the wire; rendered verbatim in the license's own
                recorded currency, never assumed to be USD. */}
            <KV
              items={[
                [t("licenses.detail.entitlements"), <span className="mono">{license.data.entitlements}</span>],
                [
                  t("licenses.detail.cost"),
                  license.data.cost ? (
                    <span className="mono">{`${license.data.cost} ${license.data.currency ?? ""}`.trim()}</span>
                  ) : (
                    <NoValue label={t("licenses.detail.noCost")} />
                  ),
                ],
                [t("licenses.detail.poRef"), license.data.po_ref ?? <NoValue label={t("licenses.detail.noPoRef")} />],
                // CALENDAR DATES, NOT INSTANTS. schema.py types these three as
                // `date`, so they arrive as "2025-01-01" -- a wall-clock day
                // with no time and no zone. Rendering that through the
                // operator's zone is the off-by-one-day the helper's own
                // docstring warns about: west of UTC "2025-01-01" becomes 31
                // Dec. Pinned to UTC so the day shown is the day recorded.
                [t("licenses.detail.purchased"), formatDate(license.data.purchased_at, "UTC")],
                [t("licenses.detail.maintenance"), formatDate(license.data.maintenance_expires_at, "UTC")],
                [t("licenses.detail.renews"), formatDate(license.data.renews_at, "UTC")],
                [t("licenses.detail.agreementRef"), license.data.agreement_ref ?? <NoValue label={t("licenses.detail.noAgreementRef")} />],
                ...(license.data.notes ? ([[t("licenses.detail.notes"), license.data.notes]] as const) : []),
              ]}
            />
          </Panel>
        )}

        <Panel title={t("licenses.detail.allocations")} bare>
          <QueryState
            loading={allocations.isLoading}
            error={allocations.error}
            empty={(allocations.data ?? []).length === 0}
            emptyLabel={t("licenses.detail.allocationsEmpty")}
          >
            <Table>
              <thead>
                <tr>
                  <Th>{t("licenses.detail.colTargetType")}</Th>
                  <Th>{t("licenses.detail.colTarget")}</Th>
                  <Th>{t("licenses.detail.colAllocated")}</Th>
                  <Th>{t("licenses.detail.colReleased")}</Th>
                </tr>
              </thead>
              <tbody>
                {(allocations.data ?? []).map((allocation) => (
                  <tr key={allocation.id}>
                    <Td
                      lead={
                        <Icon
                          name={allocation.target_type === "device" ? "device-laptop" : "user"}
                          size={18}
                        />
                      }
                    >
                      {allocation.target_type === "device" ? t("licenses.detail.device") : t("licenses.detail.person")}
                    </Td>
                    <Td>
                      {allocation.person_id ? (
                        <PersonLink personId={allocation.person_id}>
                          {allocation.target_type === "user" && userNames.has(allocation.target_id)
                            ? userNames.get(allocation.target_id)
                            : t("licenses.detail.viewPerson")}
                        </PersonLink>
                      ) : allocation.target_type === "user" && userNames.has(allocation.target_id) ? (
                        <Person
                          initials={initialsOf(userNames.get(allocation.target_id) ?? "")}
                          index={personIndex(allocation.target_id)}
                          name={userNames.get(allocation.target_id) ?? allocation.target_id}
                        />
                      ) : (
                        <span className="mono">{allocation.target_id}</span>
                      )}
                    </Td>
                    {/* Instants, so these DO take the operator's zone --
                        `allocated_at`/`released_at` are `datetime` in
                        schema.py. The ad-hoc `toLocaleString()` they replace
                        rendered in the browser's zone and the browser's
                        format, which is how one record read two ways on two
                        machines. `formatDateTime` returns NO_DATE for a
                        still-held allocation, so the `? :` goes too. */}
                    <Td>{formatDateTime(allocation.allocated_at, zone)}</Td>
                    <Td>{formatDateTime(allocation.released_at, zone)}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </QueryState>
        </Panel>

        <Dialog open={revealed != null} onClose={() => setRevealed(null)} title={t("licenses.detail.keyTitle")}>
          {/* `.notice gold`, the component enrollment and credentials use for the
              same sentence. */}
          <p className="notice gold">
            <Icon name="alert-triangle" size={16} />
            <span>{t("licenses.detail.shownOnce")}</span>
          </p>
          <div className="flex items-center gap-2" style={{ marginTop: 12 }}>
            <code className="code" style={{ flex: 1 }}>
              {revealed}
            </code>
            <Button
              variant="outline"
              onClick={() => {
                if (!revealed) return;
                const clipboard = navigator.clipboard;
                if (!clipboard) {
                  setCopy({ kind: "blocked" });
                  return;
                }
                clipboard.writeText(revealed).then(
                  () => setCopy({ kind: "copied" }),
                  (error: unknown) => setCopy({ kind: "failed", error }),
                );
              }}
            >
              {t("licenses.detail.copy")}
            </Button>
          </div>
          {copy ? (
            <p
              role={copy.kind === "copied" ? "status" : "alert"}
              className={copy.kind === "copied" ? "note" : "form-error"}
              style={{ marginTop: 8 }}
            >
              {copy.kind === "copied"
                ? t("licenses.detail.copied")
                : copy.kind === "blocked"
                  ? t("licenses.detail.copyBlocked")
                  : describeSubmitError(copy.error)}
            </p>
          ) : null}
          <div className="flex justify-end" style={{ marginTop: 12 }}>
            <Button
              onClick={() => {
                setCopy(null);
                setRevealed(null);
              }}
            >
              {t("licenses.detail.done")}
            </Button>
          </div>
        </Dialog>
      </QueryState>
    </div>
  );
}
