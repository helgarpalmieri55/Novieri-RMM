import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { ConfirmDialog } from "../../components/confirm";
import { NoValue } from "../../components/emptystate";
import { attemptProblems, describeSubmitError, Form, FormActions } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { EntityPicker } from "../../components/picker";
import { Button, Dialog, Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import {
  useIgnoreSoftwareMutation,
  useMapSoftwareMutation,
  useProducts,
  useReviewQueue,
} from "./api";
import { LicensePageNav } from "./licenseNav";
import { productPickerEmptyHint, productPickerOptions } from "./pickerOptions";
import type { ReviewQueueRow } from "./types";
import { PageHead, Panel, QueryState } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useEverywherePermissions } from "../../lib/mePermissions";

export function ReviewQueue() {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("licenses.read");
  const canManage = permissions.includes("licenses.manage");
  // Mapping writes the product catalogue, configuration every client shares
  // (GLOBAL_WRITES): the code on EVERY client (phase 3 design §2). Ignoring is not global.
  const canMap = useEverywherePermissions().has("licenses.manage");

  const queue = useReviewQueue();
  const ignore = useIgnoreSoftwareMutation();
  const [mapping, setMapping] = useState<ReviewQueueRow | null>(null);
  // Cycle 8. Ignoring writes a `software_ignore` row that no route in this
  // API removes (`licenses/api.py` serves POST .../review-queue/ignore and
  // nothing that undoes it), so a misclick hides a discovered title from the
  // compliance reconciliation permanently -- and the control sat one ghost
  // button away from "Map", with no confirmation at all.
  const [ignoring, setIgnoring] = useState<ReviewQueueRow | null>(null);
  const items = queue.data ?? [];

  return (
    <div>
      <PageHead
        title={t("licenses.review.title")}
        description={t("licenses.review.description")}
      />

      <LicensePageNav canRead={canRead} />

      {/* SWALLOWED ENTIRELY BEFORE THIS. `ignore.mutate(...)` had no `onError`
          and `ignore.isError` was rendered nowhere, so a refused Ignore --
          403 for a caller without `licenses.manage`, 503 from a proxy --
          looked exactly like a successful one: the click landed, the button
          re-enabled, and the row stayed put with no explanation for why. This
          is Auditor A's swallowed-503 class on this route. */}
      {ignore.isError && (
        <p role="alert" className="notice red">
          <Icon name="circle-x" size={16} />
          <span>{describeSubmitError(ignore.error)}</span>
        </p>
      )}

      <Panel bare>
        <QueryState
          loading={queue.isLoading}
          error={queue.error}
          empty={items.length === 0}
          emptyLabel={t("licenses.review.empty")}
          emptyIcon="license"
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("licenses.review.colName")}</Th>
                <Th>{t("licenses.review.colPublisher")}</Th>
                <Th>{t("licenses.review.colDevices")}</Th>
                {canManage && <Th />}
              </tr>
            </thead>
            <tbody>
              {items.map((row) => (
                <tr key={row.name}>
                  <Td lead={<Icon name="package" size={18} />}>
                    <div className="cell-title">{row.name}</div>
                  </Td>
                  <Td>{row.publisher || <NoValue label={t("licenses.review.noPublisher")} />}</Td>
                  <Td>
                    <span className="mono">{row.device_count}</span>
                  </Td>
                  {canManage && (
                    <Td>
                      {/* ONE TREATMENT PER CELL. `Ignore` was `ghost`, which
                          paints as bare accent text -- the shape a link has --
                          so this cell drew a bordered control and a bare one
                          beside each other and the reader had to guess whether
                          the second one navigated. The console's convention is
                          already written down at
                          `subscriptions/SubscriptionDetail.tsx`: a row control
                          is a control and carries an edge. Both mutate, both
                          are `<button>`, both are drawn the same. */}
                      <div className="flex gap-2">
                        {canMap ? (
                          <Button variant="outline" onClick={() => setMapping(row)}>
                            {t("licenses.review.map")}
                          </Button>
                        ) : null}
                        <Button
                          variant="outline"
                          disabled={ignore.isPending}
                          onClick={() => setIgnoring(row)}
                        >
                          {t("licenses.review.ignore")}
                        </Button>
                      </div>
                    </Td>
                  )}
                </tr>
              ))}
            </tbody>
          </Table>
        </QueryState>
      </Panel>

      <MapSoftwareDialog row={mapping} onClose={() => setMapping(null)} />

      {/* The subject is the discovered title AND its device count, because the
          count is what makes the difference between ignoring a stray utility
          and ignoring something installed on four hundred endpoints. */}
      <ConfirmDialog
        open={ignoring != null}
        title={t("licenses.review.ignoreTitle")}
        subject={
          ignoring ? t("licenses.review.ignoreSubject", { name: ignoring.name, count: ignoring.device_count }) : ""
        }
        body={t("licenses.review.ignoreBody")}
        confirmLabel={t("licenses.review.ignoreConfirm")}
        destructive
        onCancel={() => setIgnoring(null)}
        onConfirm={() => {
          if (ignoring) ignore.mutate({ name: ignoring.name });
          setIgnoring(null);
        }}
      />
    </div>
  );
}

const mapRules = (): readonly FieldRule[] => [{ name: "product_id", label: translate("licenses.common.product") }];

function MapSoftwareDialog({
  row,
  onClose,
}: {
  row: ReviewQueueRow | null;
  onClose: () => void;
}) {
  const t = useT();
  const products = useProducts();
  const map = useMapSoftwareMutation();
  // Not memoised: the sublabels are worded, and a held list would keep the
  // language it was built in.
  const productOptions = productPickerOptions(products.data ?? []);
  const catalogEmpty = !products.isLoading && productOptions.length === 0;
  const [productId, setProductId] = useState("");
  // Populated ON SUBMIT, never while typing; worded at render.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  // ReviewQueue keeps this dialog mounted and toggles `row`, so without
  // clearing the mutation a failure from mapping one title would still be on
  // screen while the operator maps the next, attached to the wrong name.
  function close() {
    map.reset();
    setAttempt(null);
    onClose();
  }

  function submit() {
    if (!row) return;
    const next: Attempt = { rules: mapRules, values: { product_id: productId } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;

    map.mutate(
      { name: row.name, product_id: productId },
      {
        onSuccess: () => {
          setProductId("");
          close();
        },
      },
    );
  }

  return (
    <Dialog open={row != null} onClose={close} title={t("licenses.review.mapTitle", { name: row?.name ?? "" })}>
      <Form onSubmit={submit}>
        {/* Same empty-lookup dead end as the New license dialog, on the screen
            an operator reaches FIRST: a fresh install discovers software before
            anybody has entered a product, so this select was empty exactly when
            it was most needed. The publisher sublabel matters more here than
            anywhere -- the queue row carries its own publisher, and mapping
            "Office Suite" onto the wrong publisher's product silently
            misreports compliance for both. */}
        <EntityPicker
          label={t("licenses.common.product")}
          options={productOptions}
          loading={products.isLoading}
          emptyHint={productPickerEmptyHint()}
          placeholder={t("licenses.common.searchProducts")}
          value={productId}
          onChange={setProductId}
        />
        {catalogEmpty ? (
          <p className="note">
            <Link className="panel-link" to="/licenses/products">
              {t("licenses.common.addProduct")}
            </Link>
          </p>
        ) : null}
        <div className="flex justify-end gap-2">
          {/* `type="button"` is load-bearing inside a real `<form>`: ui.tsx's
              Button sets no type, and an untyped button submits -- Cancel
              would map the software. */}
          <Button type="button" variant="ghost" onClick={close}>
            {t("licenses.common.cancel")}
          </Button>
          {/* Enabled with no product chosen ON PURPOSE (`disabled={... ||
              !productId}` was the silent no-op), and the failure now goes
              through `describeSubmitError` inside a `role="alert"` --
              `String(map.error)` rendered "ApiError: " and nothing after it
              for a bodyless 503. */}
          <FormActions
            label={t("licenses.review.map")}
            pendingLabel={t("licenses.review.mapping")}
            pending={map.isPending}
            error={map.error}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
}
