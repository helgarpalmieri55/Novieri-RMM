import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { attemptProblems, Form, FormActions } from "../../components/form";
import { NoValue } from "../../components/emptystate";
import type { Attempt, FieldRule } from "../../components/form";
import { Button, Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDateTime } from "../../lib/datetime";
import { useCreateVendorMutation, useLicenses, useVendors } from "./api";
import { LicensePageNav } from "./licenseNav";
import { PageHead, Panel, QueryState } from "../../shell/components";
import { Icon } from "../../shell/icons";

export function VendorsPage() {
  const t = useT();
  const api = useApi();
  const [creating, setCreating] = useState(false);

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("licenses.read");
  const canManage = permissions.includes("licenses.manage");

  // The operator's own zone (cycle 5); `/me` is already fetched here for the
  // permission check, so reading `timezone` off it costs no extra request.
  const zone = me.data?.timezone;

  const vendors = useVendors();
  // v3's row carries "4 products" under the vendor's name. The nearest fact
  // this API serves is how many licenses name the vendor; the list is already
  // fetched by the page next door, so it is one cached read here. A failed
  // read shows no count rather than a count of nothing.
  const licenses = useLicenses();
  const licenseCountByVendor = useMemo(() => {
    const counts = new Map<string, number>();
    for (const license of licenses.data ?? []) {
      if (license.vendor_id) counts.set(license.vendor_id, (counts.get(license.vendor_id) ?? 0) + 1);
    }
    return counts;
  }, [licenses.data]);
  const items = vendors.data ?? [];

  return (
    <div>
      <PageHead
        title={t("licenses.vendors.title")}
        description={t("licenses.vendors.description")}
        actions={
          canManage ? (
            <Button onClick={() => setCreating(true)}>
              <Icon name="plus" size={14} />
              {t("licenses.vendors.newVendor")}
            </Button>
          ) : undefined
        }
      />

      <LicensePageNav canRead={canRead} />

      <Panel bare>
        <QueryState
          loading={vendors.isLoading}
          error={vendors.error}
          empty={items.length === 0}
          emptyLabel={t("licenses.vendors.empty")}
          emptyIcon="building"
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("licenses.vendors.colName")}</Th>
                <Th>{t("licenses.vendors.colWebsite")}</Th>
                <Th>{t("licenses.vendors.colCreated")}</Th>
              </tr>
            </thead>
            <tbody>
              {items.map((vendor) => {
                const count = licenses.data ? (licenseCountByVendor.get(vendor.id) ?? 0) : null;
                return (
                  <tr key={vendor.id}>
                    <Td lead={<Icon name="building" size={18} />}>
                      <div className="cell-title">{vendor.name}</div>
                      {count !== null ? (
                        <div className="cell-sub">{t("licenses.vendors.licenses", { count })}</div>
                      ) : null}
                    </Td>
                    <Td>{vendor.website ?? <NoValue label={t("licenses.vendors.noWebsite")} />}</Td>
                    {/* `created_at` is a `datetime` in schema.py -- a real
                        instant, so it takes the operator's zone. The
                        `toLocaleString()` this replaces used the browser's
                        zone AND the browser's format, which is how the same
                        record read two ways on two machines. */}
                    <Td>{formatDateTime(vendor.created_at, zone)}</Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        </QueryState>
      </Panel>

      <NewVendorDialog open={creating} onClose={() => setCreating(false)} />
    </div>
  );
}

const vendorRules = (): readonly FieldRule[] => [{ name: "name", label: translate("licenses.common.name") }];

function NewVendorDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useT();
  const [name, setName] = useState("");
  const [website, setWebsite] = useState("");
  // Populated ON SUBMIT, never while typing; worded at render.
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const create = useCreateVendorMutation();

  function reset() {
    setName("");
    setWebsite("");
    setAttempt(null);
  }

  // VendorsPage keeps this dialog mounted and toggles `open`, so without
  // clearing the mutation a failure from one attempt would still be on screen
  // the next time the form opens, describing a request nobody made.
  function close() {
    create.reset();
    setAttempt(null);
    onClose();
  }

  function submit() {
    const next: Attempt = { rules: vendorRules, values: { name } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;

    create.mutate(
      { name: name.trim(), website: website.trim() || null },
      {
        onSuccess: () => {
          reset();
          close();
        },
      },
    );
  }

  return (
    <Dialog open={open} onClose={close} title={t("licenses.vendors.newTitle")}>
      <Form onSubmit={submit}>
        <div className="form-grid">
          <Field label={t("licenses.vendors.name")}>
            <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label={t("licenses.vendors.website")}>
            <input
              className={inputClass}
              value={website}
              onChange={(e) => setWebsite(e.target.value)}
              placeholder={t("licenses.vendors.websitePlaceholder")}
            />
          </Field>
        </div>
        <div className="flex justify-end gap-2">
          {/* `type="button"` is load-bearing inside a real `<form>`: ui.tsx's
              Button sets no type, and an untyped button submits -- Cancel
              would create the vendor. */}
          <Button type="button" variant="ghost" onClick={close}>
            {t("licenses.common.cancel")}
          </Button>
          {/* Enabled with an empty Name ON PURPOSE, and the failure now goes
              through `describeSubmitError` inside a `role="alert"`:
              `String(create.error)` rendered "ApiError: " and nothing else for
              a bodyless 503, announced to nobody. */}
          <FormActions
            label={t("licenses.common.save")}
            pending={create.isPending}
            error={create.error}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
}
