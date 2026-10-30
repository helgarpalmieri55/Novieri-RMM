/**
 * Licenses — the SAM (software asset management) system of record. Migrated
 * onto the design per the UI port spec §6, following DevicesPage.tsx as the
 * template.
 *
 * WHAT THE DESIGN ASKS FOR THAT THE API DOES NOT SERVE. The prototype's
 * Licenses stats show five cards: License products, Annual spend, Overused
 * products, Unused seats, Renewals in 90d. Two of the five have no honest
 * number behind them here:
 *
 * - "Annual spend" -- `GET /licenses/costs` (useCosts) returns one row per
 *   product PER CURRENCY (CostRow.currency). Summing across currencies into
 *   one figure would silently misstate it (a license priced in COP added to
 *   one priced in USD is not a real number) -- the same rule the Budget
 *   module's UnconvertibleDisclosure exists to enforce. Omitted rather than
 *   summed wrong.
 * - "Unused seats" -- would need, for every license, entitlements minus
 *   *active* allocation count. No endpoint serves that join; computing it
 *   client-side means one `/licenses/{id}/allocations` request per license
 *   row, which does not belong behind a stat card. Omitted; needs a
 *   dedicated aggregate endpoint (mirrors the Devices Patch/Backup gap, UI
 *   port spec §7).
 *
 * "License products" (product catalog count), "Compliance breaches"
 * (over-deployed products from `licenseCompliance`) and "Renewals in 90
 * days" (from `licenseRenewals`) are all real counts from the data actually
 * fetched below.
 */

import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { attemptProblems, describeSubmitError, Form, FormActions } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { EntityPicker } from "../../components/picker";
import { Button, Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useLanguage, useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDate } from "../../lib/datetime";
import { internalClientId, requireKnown } from "../../lib/internalClient";
import { humaniseEnum } from "../../lib/labels";
import {
  useCompliance,
  useCreateLicenseMutation,
  useLicenses,
  useProducts,
  useRenewals,
  useVendors,
} from "./api";
import { LicenseReadiness } from "./LicenseReadiness";
import { LicensePageNav } from "./licenseNav";
import { licenseTypeKind } from "./licenseState";
import {
  productPickerEmptyHint,
  productPickerOptions,
  vendorPickerEmptyHint,
  vendorPickerOptions,
} from "./pickerOptions";
import type { LicenseOut, LicenseType } from "./types";
import { FilterBar, PageHead, Panel, QueryState, SearchField, StatCard, StatsGrid, StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";

const LICENSE_TYPES: LicenseType[] = [
  "perpetual",
  "volume",
  "oem",
  "cal",
  "per_device",
  "per_user",
];

const RENEWAL_WINDOW_DAYS = 90;

/**
 * What a license record may not be created without.
 *
 * Closes the audit's "Empty submit in the license modal is a silent no-op":
 * the control read `disabled={create.isPending || !productId}`, so a Save
 * with nothing chosen moved nothing and named nothing. `entitlements` was
 * never guarded at all -- `Number("")` is NaN, and NaN serialises to `null`,
 * so a cleared field reached the API as an absent entitlement count.
 */
// Built when asked, never held: the labels and sentences follow the language.
const licenseBaseRules = (): readonly FieldRule[] => [
  { name: "product_id", label: translate("licenses.new.ruleProduct") },
  { name: "entitlements", label: translate("licenses.new.ruleEntitlements"), validate: wholeSeatCount },
];

/**
 * Money, and the currency without which money is not a fact.
 *
 * Closes "Money and currency are unvalidated free-text fields". Cost is a
 * Decimal server-side and travels as a STRING (types.ts), so nothing between
 * the keyboard and the database parses it -- "1,200" and "approx 1.2k" are
 * accepted by the input, rejected by pydantic, and the 422 lands as the raw
 * error this dialog used to render with `String(create.error)`.
 *
 * Currency is required ONCE a cost is entered, for the reason this module's
 * docstring already gives: an amount whose currency is unknown cannot be
 * reconciled or summed, which is why `/licenses/costs` reports per currency
 * rather than as a total.
 */
const costRules = (): readonly FieldRule[] => [
  { name: "cost", label: translate("licenses.new.ruleCost"), validate: decimalAmount },
  { name: "currency", label: translate("licenses.new.ruleCurrency"), validate: currencyCode },
];

// One mechanism for "missing" and for "malformed" (form.tsx's requiredProblems
// runs both), so an optional field is expressed as a rule that only applies
// once the operator has typed something -- never as a second check bolted on
// beside the first.
function licenseRules(cost: string): readonly FieldRule[] {
  return cost.trim() ? [...licenseBaseRules(), ...costRules()] : licenseBaseRules();
}

function wholeSeatCount(value: string): string | null {
  if (!/^\d+$/.test(value) || Number(value) < 1) {
    return translate("licenses.new.entitlementsWhole");
  }
  return null;
}

function decimalAmount(value: string): string | null {
  // No symbol and no thousands separator: the string is forwarded verbatim as
  // the Decimal, so whatever is typed here is what the backend parses.
  if (!/^\d+(\.\d{1,2})?$/.test(value)) {
    return translate("licenses.new.costAmount");
  }
  return null;
}

function currencyCode(value: string): string | null {
  // ISO 4217 is three letters; the field is `maxLength={3}` but a paste of
  // "US" or "$" satisfies that and still means nothing.
  if (!/^[A-Za-z]{3}$/.test(value)) return translate("licenses.new.currencyCode");
  return null;
}

function matchesSearch(license: LicenseOut, productName: string, needle: string): boolean {
  if (needle === "") return true;
  // The type as it is worded on screen too, so "perpetua" finds a row.
  return [productName, license.type, humaniseEnum(license.type, "LicenseType"), license.po_ref ?? "", license.agreement_ref ?? ""]
    .join(" ")
    .toLowerCase()
    .includes(needle);
}

export function LicensesPage() {
  const t = useT();
  // The filter matches on worded labels, so it is rebuilt when the language changes.
  const { language } = useLanguage();
  const api = useApi();
  const [creating, setCreating] = useState(false);
  const [search, setSearch] = useState("");

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("licenses.read");
  const canManage = permissions.includes("licenses.manage");

  const licenses = useLicenses();
  const products = useProducts();
  const vendors = useVendors();
  const compliance = useCompliance();
  const renewals = useRenewals(RENEWAL_WINDOW_DAYS);
  const productNameById = useMemo(
    () => new Map((products.data ?? []).map((p) => [p.id, p.name])),
    [products.data],
  );
  const vendorNameById = useMemo(
    () => new Map((vendors.data ?? []).map((v) => [v.id, v.name])),
    [vendors.data],
  );

  const items = licenses.data ?? [];
  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return items.filter((license) =>
      matchesSearch(license, productNameById.get(license.product_id) ?? license.product_id, needle),
    );
  }, [items, productNameById, search, language]);

  // The Over-deployed, Compliant and "of 3 products reconciled" figures all
  // count PRODUCTS. The report is one row per (client, product) since MSP
  // clients phase 2, so each counts distinct products: a product is over when
  // any client's position on it is a breach, and compliant only when none is,
  // so over + compliant = reconciled.
  const { breachCount, compliantCount, reconciledCount } = useMemo(() => {
    const breached = new Set<string>();
    const reconciled = new Set<string>();
    for (const row of compliance.data ?? []) {
      reconciled.add(row.product_id);
      if (row.breach) breached.add(row.product_id);
    }
    return {
      breachCount: breached.size,
      compliantCount: reconciled.size - breached.size,
      reconciledCount: reconciled.size,
    };
  }, [compliance.data]);

  // EVERY TILE'S FOOT LINE IS READ OFF THE SAME QUERY ITS FIGURE IS. The row
  // read uneven because one tile carried its breakdown and three carried an
  // empty line; each of these is the second half of a fact this page has
  // already fetched, never a figure invented to fill the slot.
  //
  //  - renewals: which products are due in the window, from the renewal rows
  //    themselves (one row per licence, so the names are de-duplicated);
  //  - products: how many distinct publishers the catalog spans;
  //  - compliant: how many products were reconciled at all, which is what
  //    "3 compliant" is a share of.
  const renewingProducts = useMemo(() => {
    const names = new Set<string>();
    for (const row of renewals.data ?? []) {
      names.add(productNameById.get(row.product_id) ?? row.product_id);
    }
    return [...names];
  }, [renewals.data, productNameById]);
  const publisherCount = useMemo(
    () => new Set((products.data ?? []).map((p) => p.publisher).filter(Boolean)).size,
    [products.data],
  );

  // A STAT CARD MUST NOT REPORT A FAILED QUERY AS A NUMBER. `compliance.data
  // ?? []` makes a refused or unreachable `/licenses/compliance` render
  // "Compliance breaches: 0" -- a reassuring compliance figure that no data
  // supports, and indistinguishable from a genuinely clean estate. Auditor
  // A's swallowed-503 class, in the one place on this route where the UI
  // still looked successful after a failure. Each card falls back to NO_DATE
  // for its own query, and the reason is said once, announced.
  const statsError = products.error ?? compliance.error ?? renewals.error;

  return (
    <div>
      <PageHead
        title={t("licenses.list.title")}
        description={t("licenses.list.description")}
        actions={
          canManage ? (
            <Button onClick={() => setCreating(true)}>
              <Icon name="plus" size={14} />
              {t("licenses.list.newLicense")}
            </Button>
          ) : undefined
        }
      />

      <LicensePageNav canRead={canRead} />

      <LicenseReadiness />

      {/* EVERY TILE NAMES THE LIST BEHIND IT. The four numbers on this row are
          each computed from a query whose full result already has a page:
          the compliance dashboard (twice: what is over and what is within its
          entitlement), the renewal calendar, the product catalog. The order
          is v3's: the breach first, because it is the one number here that
          needs somebody today, and only a non-zero breach count wears the
          critical wash; the renewal window is the warning; the compliant
          count colours its figure ok; the catalog count is plain.

          The zero labels are safe against the failed-query case above them:
          a failed query renders "n/a", not "0", so "nothing over-deployed"
          can only appear when the compliance query actually returned and
          found nothing.

          The over-deployed tile carries v3's foot line: the names of the
          products that are over, which are already in the compliance result.
          The other three have no honest second figure on this page, so they
          carry none rather than a made-up one. */}
      <StatsGrid columns={4}>
        <StatCard
          label={t("licenses.list.tileOver")}
          value={compliance.error ? t("licenses.common.na") : String(breachCount)}
          icon="alert-triangle"
          to="/licenses/compliance"
          tone="red"
          urgent={!compliance.error && breachCount > 0}
          zeroLabel={t("licenses.list.tileOverZero")}
          foot={
            // One compliance row per (client, product): two clients over on
            // one product are one product over and one name.
            !compliance.error && breachCount > 0
              ? [
                  ...new Set(
                    (compliance.data ?? []).filter((row) => row.breach).map((row) => row.name),
                  ),
                ].join(", ")
              : undefined
          }
        />
        <StatCard
          label={t("licenses.list.tileRenewals", { count: RENEWAL_WINDOW_DAYS })}
          value={renewals.error ? t("licenses.common.na") : String((renewals.data ?? []).length)}
          icon="clock"
          to="/licenses/renewals"
          tone="gold"
          zeroLabel={t("licenses.list.tileRenewalsZero")}
          foot={renewingProducts.length > 0 ? renewingProducts.join(", ") : undefined}
        />
        <StatCard
          label={t("licenses.list.tileProducts")}
          value={products.error ? t("licenses.common.na") : String(products.data?.length ?? 0)}
          icon="license"
          to="/licenses/products"
          tone="blue"
          zeroLabel={t("licenses.list.tileProductsZero")}
          foot={
            publisherCount > 0 ? t("licenses.list.publishers", { count: publisherCount }) : undefined
          }
        />
        <StatCard
          label={t("licenses.list.tileCompliant")}
          value={compliance.error ? t("licenses.common.na") : String(compliantCount)}
          icon="circle-check"
          to="/licenses/compliance"
          tone="green"
          zeroLabel={t("licenses.list.tileCompliantZero")}
          foot={
            !compliance.error && compliantCount > 0
              ? t("licenses.list.reconciled", { count: reconciledCount })
              : undefined
          }
        />
      </StatsGrid>

      {statsError ? (
        <p role="alert" className="form-error">
          {describeSubmitError(statsError)}
        </p>
      ) : null}

      <Panel bare>
        <FilterBar>
          <SearchField
            placeholder={t("licenses.list.searchPlaceholder")}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            aria-label={t("licenses.list.searchAria")}
          />
          <span className="filter-spacer" />
          <span className="note">{t("licenses.list.range", { shown: filtered.length, total: items.length })}</span>
        </FilterBar>

        <QueryState
          loading={licenses.isLoading}
          error={licenses.error}
          empty={filtered.length === 0}
          emptyLabel={items.length === 0 ? t("licenses.list.emptyNone") : t("licenses.list.emptyFiltered")}
          emptyIcon="license"
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("licenses.list.colProduct")}</Th>
                <Th>{t("licenses.list.colType")}</Th>
                <Th>{t("licenses.list.colEntitlements")}</Th>
                <Th>{t("licenses.list.colKey")}</Th>
                <Th>{t("licenses.list.colRenews")}</Th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((license) => (
                <tr key={license.id}>
                  {/* v3's row: the glyph, the title, and the one line under it
                      that says who sold it. The vendor is optional on a
                      license, so the sub-line falls back to the PO reference
                      and then to nothing at all rather than a placeholder. */}
                  <Td lead={<Icon name="license" size={18} />}>
                    <div className="cell-title">
                      <Link to={`/licenses/${license.id}`}>
                        {productNameById.get(license.product_id) ?? license.product_id}
                      </Link>
                    </div>
                    {license.vendor_id || license.po_ref ? (
                      <div className="cell-sub">
                        {[
                          license.vendor_id ? vendorNameById.get(license.vendor_id) ?? null : null,
                          license.po_ref,
                        ]
                          .filter(Boolean)
                          .join(" · ")}
                      </div>
                    ) : null}
                  </Td>
                  <Td>
                    <StatusPill
                      value={license.type}
                      label={humaniseEnum(license.type, "LicenseType")}
                      kind={licenseTypeKind(license.type)}
                    />
                  </Td>
                  <Td>
                    <span className="mono">{license.entitlements}</span>
                  </Td>
                  <Td>
                    {license.has_key ? (
                      <StatusPill value="has_key" label={t("licenses.list.hasKey")} kind="info" />
                    ) : (
                      <StatusPill value="no_key" label={t("licenses.list.noKey")} kind="mut" />
                    )}
                  </Td>
                  {/* A CALENDAR DATE, NOT AN INSTANT. schema.py types
                      `renews_at` as `date`, so it arrives as "2026-08-01" --
                      a day with no time and no zone. Rendering it through
                      the operator's zone would move it to 31 Jul anywhere
                      west of UTC, an off-by-one-day that reads as real data.
                      Pinned to UTC, and NO_DATE rather than a bare `??` so
                      an absent renewal cannot become "Invalid Date". */}
                  <Td>{formatDate(license.renews_at, "UTC")}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </QueryState>
      </Panel>

      <NewLicenseDialog open={creating} onClose={() => setCreating(false)} />
    </div>
  );
}

function NewLicenseDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const products = useProducts();
  const vendors = useVendors();
  const create = useCreateLicenseMutation();

  // Not memoised: the sublabels are worded, and a held list would keep the
  // language it was built in.
  const productOptions = productPickerOptions(products.data ?? []);
  const vendorOptions = vendorPickerOptions(vendors.data ?? []);
  // A list that has not arrived yet is not an empty list, and offering the
  // "add one" link during the first fetch tells people to go fix a catalog
  // that may be full. `isLoading` gates both.
  const catalogEmpty = !products.isLoading && productOptions.length === 0;
  const vendorsEmpty = !vendors.isLoading && vendorOptions.length === 0;

  const [productId, setProductId] = useState("");
  const [vendorId, setVendorId] = useState("");
  const [type, setType] = useState<LicenseType>("perpetual");
  const [entitlements, setEntitlements] = useState("1");
  const [key, setKey] = useState("");
  // The key is masked by default and revealable on request -- Auditor B,
  // [LOW]: "License keys are long alphanumeric strings that are usually pasted
  // and then visually verified; masking without a reveal makes transcription
  // errors undetectable."
  const [keyVisible, setKeyVisible] = useState(false);
  const [cost, setCost] = useState("");
  const [currency, setCurrency] = useState("");
  // Populated ON SUBMIT, never while typing -- complaining about a field the
  // operator has not finished filling in is its own kind of unhelpful. Held as
  // the rules and the values they were checked against, worded at render. The
  // key is NOT in the snapshot: it is not a rule's business and never copied.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  function reset() {
    setProductId("");
    setVendorId("");
    setType("perpetual");
    setEntitlements("1");
    setKey("");
    setKeyVisible(false);
    setCost("");
    setCurrency("");
    setAttempt(null);
  }

  // This dialog is never unmounted -- LicensesPage keeps it rendered and
  // toggles `open` -- so without clearing the mutation here a failure from
  // one attempt sits in the form waiting to be read as the next one's.
  function close() {
    create.reset();
    setAttempt(null);
    onClose();
  }

  function submit() {
    const values = { product_id: productId, entitlements, cost, currency };
    const next: Attempt = { rules: () => licenseRules(values.cost), values };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;

    // A license is one client's (MSP clients phase 2), and the server holds
    // what may hold it to that client.
    // PHASE 3: the client/site selector. Until it exists that is the
    // organisation's internal client, whose id is the organisation's --
    // `/me`'s `org_id` (`lib/internalClient.ts`).
    const internalClient = internalClientId(me.data);
    create.mutate(
      () => ({
        client_id: requireKnown(internalClient),
        product_id: productId,
        vendor_id: vendorId || null,
        type,
        // `entitlements` is a count, so it is a legitimate Number() -- and by
        // this line the rule above has already established it is a whole
        // number, which is what stops `Number("")` reaching the API as NaN.
        entitlements: Number(entitlements),
        key: key || null,
        // Money stays a STRING all the way to the wire (types.ts: Decimal
        // server-side). Empty means "not recorded", which is not the same
        // fact as zero and must not be sent as one.
        cost: cost.trim() || null,
        currency: currency.trim().toUpperCase() || null,
      }),
      {
        onSuccess: () => {
          reset();
          close();
        },
      },
    );
  }

  return (
    <Dialog open={open} onClose={close} title={t("licenses.new.title")}>
      <Form onSubmit={submit}>
        <div className="form-grid">
          {/* THE BLOCKED FLOW. Auditor B, [HIGH]: the Product select held exactly
              one option -- "Select a product…" -- on a fresh install, so no
              license could ever be created, and "the modal neither says so nor
              links to" the Products tab that would seed it. `EntityPicker`
              renders `emptyHint` in place of the empty list; the link below puts
              the screen that fills it one click away. Hiding the form was never
              the fix: a form that cannot be completed and does not say why IS
              the finding. */}
          <EntityPicker
            label={t("licenses.new.product")}
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
          <EntityPicker
            label={t("licenses.new.vendor")}
            options={vendorOptions}
            loading={vendors.isLoading}
            emptyHint={vendorPickerEmptyHint()}
            placeholder={t("licenses.new.searchVendors")}
            value={vendorId}
            onChange={setVendorId}
          />
          {vendorsEmpty ? (
            <p className="note">
              <Link className="panel-link" to="/licenses/vendors">
                {t("licenses.new.addVendor")}
              </Link>
            </p>
          ) : null}
          <Field label={t("licenses.new.type")}>
            <select
              className={inputClass}
              value={type}
              onChange={(e) => setType(e.target.value as LicenseType)}
            >
              {LICENSE_TYPES.map((type) => (
                <option key={type} value={type}>
                  {humaniseEnum(type, "LicenseType")}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("licenses.new.entitlements")}>
            {/* `min`/`max` state the rule to the spinner; the refusal a typed or
                pasted `0` gets is `wholeSeatCount`'s, because <Form> is
                `noValidate` and a native bubble that vanishes on the next click
                is the silent no-op wearing a hat. Auditor B, [MEDIUM]: "`min=""`
                and `max=""` -- no bounds ... a negative or zero entitlement count
                would corrupt the compliance calculation that is this module's
                whole purpose." */}
            <input
              type="number"
              min={1}
              step={1}
              required
              className={inputClass}
              value={entitlements}
              onChange={(e) => setEntitlements(e.target.value)}
            />
          </Field>
          <Field label={t("licenses.new.cost")}>
            {/* Text with a decimal keypad, not type="number": the value is a
                Decimal string on the wire, and a number input would hand us a
                locale-formatted or exponent-notation value to re-serialise. */}
            <input
              className={inputClass}
              type="text"
              inputMode="decimal"
              placeholder={t("licenses.new.costPlaceholder")}
              value={cost}
              onChange={(e) => setCost(e.target.value)}
            />
          </Field>
          <Field label={t("licenses.new.currency")}>
            <input
              className={inputClass}
              value={currency}
              maxLength={3}
              placeholder={t("licenses.new.currencyPlaceholder")}
              onChange={(e) => setCurrency(e.target.value.toUpperCase())}
            />
          </Field>
          <Field label={t("licenses.new.key")} full>
            {/* MASKED BY DEFAULT, REVEALABLE ON REQUEST. The key is sealed
                server-side and read back only through the audited reveal
                endpoint, so the default stays masked -- but a key is pasted and
                then checked against the paperwork, and a mask with no reveal
                makes a transcription error undetectable until the vendor
                refuses the activation. The toggle says which state it is in, so
                nobody has to infer it from the dots. */}
            <div className="flex items-center gap-2">
              <input
                className={inputClass}
                type={keyVisible ? "text" : "password"}
                value={key}
                onChange={(e) => setKey(e.target.value)}
              />
              <Button type="button" variant="ghost" onClick={() => setKeyVisible((on) => !on)}>
                {keyVisible ? t("licenses.new.hideKey") : t("licenses.new.showKey")}
              </Button>
            </div>
          </Field>
        </div>
        <div className="flex justify-end gap-2">
          {/* `type="button"` is load-bearing inside a real `<form>`:
              ui.tsx's Button sets no type, and an untyped button submits --
              Cancel would create the license. */}
          <Button type="button" variant="ghost" onClick={close}>
            {t("licenses.common.cancel")}
          </Button>
          {/* Enabled with no product selected ON PURPOSE. `disabled={... ||
              !productId}` was the silent no-op the audit found: the click
              moved nothing and named no field. Now the click says which. */}
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
