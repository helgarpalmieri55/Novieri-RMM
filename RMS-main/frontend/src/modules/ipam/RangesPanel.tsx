import { useState } from "react";
import { ConfirmDialog } from "../../components/confirm";
import { NoValue } from "../../components/emptystate";
import { RowMenu } from "../../components/rowmenu";
import { Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { useCreateIpamRangeMutation, useDeleteIpamRangeMutation, useIpamRanges } from "./api";
import { ErrorLine } from "./ErrorLine";
import { rangeKindPresentation, rangeSpan } from "./ipamState";
import { variantKind } from "./kinds";
import type { RangeKind } from "./types";
import { RANGE_KINDS } from "./types";
import { Icon } from "../../shell/icons";
import { Btn, Panel, StatusPill } from "../../shell/components";

// The three range kinds, and what each one actually does, which is different
// in each case and easy to assume wrong:
//
//   * `dhcp_pool` outranks every other row of the derived-status table: an
//     address inside one reads `dhcp` whatever the plan says, and a static
//     assignment inside it is REFUSED with a 409.
//   * `reserved` is held back from the allocator only.
//   * `excluded` is held back from the allocator AND subtracted from every
//     sweep target set, so addresses in it are never probed.
export function RangesPanel({ subnetId, canManage }: { subnetId: string; canManage: boolean }) {
  const t = useT();
  const ranges = useIpamRanges(subnetId);
  const create = useCreateIpamRangeMutation();
  const remove = useDeleteIpamRangeMutation();
  const [kind, setKind] = useState<RangeKind>("dhcp_pool");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [name, setName] = useState("");
  const [deleting, setDeleting] = useState<{ id: string; span: string } | null>(null);

  return (
    <Panel
      title={t("ipam.ranges.title")}
      subtitle={t("ipam.ranges.subtitle")}
      bare
    >
      {ranges.data && ranges.data.length === 0 ? (
        <div className="empty">
          <Icon name="list" size={26} />
          <b>{t("ipam.ranges.empty")}</b>
        </div>
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>{t("ipam.ranges.colRange")}</Th>
              <Th>{t("ipam.fields.kind")}</Th>
              {canManage && <Th>{t("ipam.fields.actions")}</Th>}
            </tr>
          </thead>
          <tbody>
            {ranges.data?.map((range) => {
              const presentation = rangeKindPresentation(range.kind);
              return (
                <tr key={range.id}>
                  <Td lead={<Icon name="list" size={18} />}>
                    <div className="cell-title mono">{rangeSpan(range)}</div>
                    <div className="cell-sub">{range.name || <NoValue label={t("ipam.ranges.noName")} />}</div>
                  </Td>
                  <Td>
                    <span title={presentation.meaning}>
                      <StatusPill
                        value={range.kind}
                        kind={variantKind(presentation.variant)}
                        label={presentation.label}
                      />
                    </span>
                  </Td>
                  {canManage && (
                    <Td>
                      <RowMenu
                        subject={rangeSpan(range)}
                        items={[
                          {
                            label: t("ipam.fields.delete"),
                            destructive: true,
                            onSelect: () => setDeleting({ id: range.id, span: rangeSpan(range) }),
                          },
                        ]}
                      />
                    </Td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </Table>
      )}

      <ConfirmDialog
        open={deleting !== null}
        title={t("ipam.ranges.deleteTitle")}
        body={t("ipam.ranges.deleteBody")}
        subject={deleting?.span ?? ""}
        confirmLabel={t("ipam.ranges.deleteConfirm")}
        destructive
        onConfirm={() => {
          if (deleting) remove.mutate({ subnetId, rangeId: deleting.id });
          setDeleting(null);
        }}
        onCancel={() => setDeleting(null)}
      />

      {(create.isError || remove.isError) && (
        <div className="panel-b">
          <ErrorLine error={create.error ?? remove.error} />
        </div>
      )}

      {canManage && (
        <div className="panel-b" style={{ borderTop: "1px solid var(--line)" }}>
          <div className="form-grid">
            <Field label={t("ipam.fields.kind")}>
              <select
                className={inputClass}
                value={kind}
                onChange={(e) => setKind(e.target.value as RangeKind)}
              >
                {RANGE_KINDS.map((value) => (
                  <option key={value} value={value}>
                    {rangeKindPresentation(value).label}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={t("ipam.fields.name")}>
              <input
                className={inputClass}
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </Field>
            <Field label={t("ipam.fields.start")}>
              <input
                className={inputClass}
                value={start}
                onChange={(e) => setStart(e.target.value)}
              />
            </Field>
            <Field label={t("ipam.fields.end")}>
              <input className={inputClass} value={end} onChange={(e) => setEnd(e.target.value)} />
            </Field>
          </div>
          <p className="note">{rangeKindPresentation(kind).meaning}</p>
          <div className="form-actions">
            <Btn
              label={t("ipam.ranges.add")}
              icon="plus"
              variant="primary"
              disabled={create.isPending || start === "" || end === ""}
              onClick={() =>
                create.mutate(
                  { subnetId, body: { kind, start_address: start, end_address: end, name } },
                  {
                    onSuccess: () => {
                      setStart("");
                      setEnd("");
                      setName("");
                    },
                  },
                )
              }
            />
          </div>
        </div>
      )}
    </Panel>
  );
}
