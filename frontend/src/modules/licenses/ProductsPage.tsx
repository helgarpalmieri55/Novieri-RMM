import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { attemptProblems, Form, FormActions } from "../../components/form";
import { NoValue } from "../../components/emptystate";
import type { Attempt, FieldRule } from "../../components/form";
import { Button, Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { lazyLabels } from "../../i18n/lazy";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { useCompliance, useCreateProductMutation, useProducts, useUpdateProductMutation } from "./api";
import { LicensePageNav } from "./licenseNav";
import type { MatchRule, ProductOut } from "./types";
import {
  Btn,
  ListEmpty,
  ListToolbar,
  PageHead,
  Panel,
  QueryState,
  SearchField,
  ShowingCount,
  useEditParam,
  useNewParam,
  useUnsavedGuard,
  useUrlFilters,
} from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useEverywherePermissions } from "../../lib/mePermissions";

const MATCH_RULE_TYPES: MatchRule["type"][] = ["exact", "prefix", "regex"];

/**
 * A rule kind as a person reads it.
 *
 * `rule.type` is a wire enum and was printed as one, glued to the pattern with
 * a colon: `prefix:Adobe Acrobat`. The PATTERN is data and is shown verbatim --
 * it is what an operator types into the editor and what the backend matches on
 * -- but the kind is an identifier, and identifiers get humanised here the same
 * way every other enum on this route does.
 */
const MATCH_RULE_LABEL: Readonly<Record<MatchRule["type"], string>> = lazyLabels({
  exact: "licenses.products.ruleExact",
  prefix: "licenses.products.rulePrefix",
  regex: "licenses.products.ruleRegex",
});

const productRules = (): readonly FieldRule[] => [{ name: "name", label: translate("licenses.common.name") }];

function matchesProduct(product: ProductOut, needle: string): boolean {
  if (needle === "") return true;
  return `${product.name} ${product.publisher}`.toLowerCase().includes(needle);
}

export function ProductsPage() {
  const t = useT();
  const api = useApi();
  // ON THE LIST KIT (Receta L, a list that only searches; Task 10, 5.2): the
  // search in `?q=`, New product from `?new=product` and an edit from
  // `?edit=<id>`, each a link. The catalog is the organisation's, shared by
  // every client: no Client chip.
  const filters = useUrlFilters(["q"] as const);
  const newProduct = useNewParam("product");
  const edit = useEditParam("product");

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("licenses.read");
  // Configuration every client shares (GLOBAL_WRITES): the code on EVERY client (phase 3 design §2).
  const canManage = useEverywherePermissions().has("licenses.manage");

  const products = useProducts();
  // v3's "Detected" column: what inventory found installed, per product. The
  // compliance report is the one read that carries that number, and it is
  // already cached from `/licenses`. A failed read draws nothing in the
  // column rather than a zero that reads as "nothing installed".
  //
  // The report is one row per (client, product) since MSP clients phase 2,
  // each counting that client's own computers, so a product's installs are
  // the SUM of its rows -- keyed alone, the last client's row would win.
  const compliance = useCompliance();
  const installedByProduct = useMemo(() => {
    const totals = new Map<string, number>();
    for (const row of compliance.data ?? []) {
      totals.set(row.product_id, (totals.get(row.product_id) ?? 0) + row.installed);
    }
    return totals;
  }, [compliance.data]);
  const items = products.data ?? [];
  const needle = filters.values.q.trim().toLowerCase();
  const shown = items.filter((product) => matchesProduct(product, needle));
  // The editor opens only for a product that is in the catalog, and only for
  // someone who may change it: a stale `?edit=` opens nothing.
  const editing = canManage ? items.find((product) => product.id === edit.id) : undefined;

  const primary = canManage ? (
    <Btn label={t("licenses.products.newProduct")} icon="plus" variant="primary" onClick={newProduct.start} />
  ) : undefined;

  return (
    <div>
      <PageHead title={t("licenses.products.title")} description={t("licenses.products.description")} actions={primary} />

      <LicensePageNav canRead={canRead} />

      <Panel bare>
        <ListToolbar
          search={
            <SearchField
              placeholder={t("licenses.products.searchPlaceholder")}
              aria-label={t("licenses.products.search")}
              value={filters.values.q}
              onChange={(event) => filters.set("q", event.target.value)}
            />
          }
          end={products.data ? <ShowingCount shown={shown.length} total={items.length} /> : null}
        />
        <QueryState loading={products.isLoading} error={products.error} empty={false} emptyLabel="">
          {shown.length === 0 ? (
            <ListEmpty
              filtered={filters.active}
              onClear={filters.clear}
              nothingYet={t("licenses.products.empty")}
              action={primary}
              icon="package"
            />
          ) : (
          <Table label={t("licenses.products.title")}>
            <thead>
              <tr>
                <Th>{t("licenses.products.colName")}</Th>
                <Th>{t("licenses.products.colPublisher")}</Th>
                <Th>{t("licenses.products.colRules")}</Th>
                <Th>{t("licenses.products.colDetected")}</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {shown.map((product) => {
                const installed = installedByProduct.get(product.id);
                const rules = product.match_rules.length;
                return (
                  <tr key={product.id}>
                    <Td lead={<Icon name="package" size={18} />}>
                      <div className="cell-title">{product.name}</div>
                      <div className="cell-sub">{t("licenses.products.rules", { count: rules })}</div>
                    </Td>
                    <Td>{product.publisher || <NoValue label={t("licenses.products.noPublisher")} />}</Td>
                    {/* One rule per line, not one comma-joined string: at
                        390px the joined form wrapped to four lines of mixed
                        prose and pattern with no way to tell where one rule
                        ended and the next began. */}
                    <Td>
                      {product.match_rules.length === 0 ? (
                        <NoValue label={t("licenses.products.noRules")} />
                      ) : (
                        product.match_rules.map((rule, index) => (
                          <div key={index}>
                            {`${MATCH_RULE_LABEL[rule.type]} `}
                            <span className="mono">{rule.pattern}</span>
                          </div>
                        ))
                      )}
                    </Td>
                    {/* NO RULE, NO COUNT (The Absent Zero Rule): a product
                        with no match rule cannot be detected, so the report's
                        0 is "nothing looked", said as such, as on the
                        compliance page. */}
                    <Td>
                      {installed === undefined ? (
                        t("licenses.common.na")
                      ) : rules === 0 ? (
                        <span className="note">{t("licenses.compliance.noUsage")}</span>
                      ) : (
                        <span className="mono">{t("licenses.products.installs", { count: installed })}</span>
                      )}
                    </Td>
                    <Td>
                      {canManage && (
                        <Btn label={t("licenses.common.edit")} onClick={() => edit.open(product.id)} />
                      )}
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
          )}
        </QueryState>
      </Panel>

      {/* Mounted per product (its `key`), so the editor's fields start from
          the product it opens; one mounted for "new" starts empty. */}
      {canManage && newProduct.open ? <ProductEditor key="new" product="new" onClose={newProduct.close} /> : null}
      {editing ? <ProductEditor key={editing.id} product={editing} onClose={edit.close} /> : null}
    </div>
  );
}

function ProductEditor({
  product,
  onClose,
}: {
  product: ProductOut | "new" | null;
  onClose: () => void;
}) {
  const t = useT();
  const create = useCreateProductMutation();
  const update = useUpdateProductMutation();

  const existing = product === "new" || product == null ? null : product;

  const [name, setName] = useState(existing?.name ?? "");
  const [publisher, setPublisher] = useState(existing?.publisher ?? "");
  const [matchRules, setMatchRules] = useState<MatchRule[]>(existing?.match_rules ?? []);
  // Populated ON SUBMIT, never while typing -- complaining about a field the
  // operator has not finished filling in is its own kind of unhelpful.
  // Held as the rules and the values they were checked against, worded at render.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  const mutation = existing ? update : create;

  // A CHANGE IN THE EDITOR ASKS BEFORE IT IS LEFT (coordinator's ruling on the
  // wave reviews): `?new=product` or `?edit=<id>` is what holds the form, so
  // Back, Cancel or Escape with a change asks first. The page mounts the
  // editor per opening (its `key`), so each one starts from what it opens.
  const dirty =
    name !== (existing?.name ?? "") ||
    publisher !== (existing?.publisher ?? "") ||
    JSON.stringify(matchRules) !== JSON.stringify(existing?.match_rules ?? []);
  const { guard, release } = useUnsavedGuard(dirty, { search: [product === "new" ? "new" : "edit"] });

  function reset() {
    setName("");
    setPublisher("");
    setMatchRules([]);
    setAttempt(null);
  }

  // Clearing the mutations on close keeps a failure from one attempt off the next.
  function close() {
    create.reset();
    update.reset();
    setAttempt(null);
    onClose();
  }

  function addRule() {
    setMatchRules([...matchRules, { type: "exact", pattern: "", publisher: null }]);
  }

  function updateRule(index: number, patch: Partial<MatchRule>) {
    setMatchRules(matchRules.map((rule, i) => (i === index ? { ...rule, ...patch } : rule)));
  }

  function removeRule(index: number) {
    setMatchRules(matchRules.filter((_, i) => i !== index));
  }

  function submit() {
    // An untouched product sends nothing: Save closes as a save would, and no
    // PATCH writes a change that is not one (the coordinator's ruling 13).
    if (existing && !dirty) {
      close();
      return;
    }
    const next: Attempt = { rules: productRules, values: { name } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;

    const body = { name: name.trim(), publisher, match_rules: matchRules };
    const onSuccess = () => {
      // Saved: closing leaves without asking (useUnsavedGuard's release).
      release();
      reset();
      close();
    };
    if (existing) {
      update.mutate({ id: existing.id, body }, { onSuccess });
    } else {
      create.mutate(body, { onSuccess });
    }
  }

  return (
    <>
    <Dialog
      open={product != null}
      onClose={close}
      title={product === "new" ? t("licenses.products.newTitle") : t("licenses.products.editTitle")}
    >
      <Form onSubmit={submit}>
        <div className="form-grid">
          <Field label={t("licenses.products.name")}>
            <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label={t("licenses.products.publisher")}>
            <input
              className={inputClass}
              value={publisher}
              onChange={(e) => setPublisher(e.target.value)}
            />
          </Field>

          {/* A group, not a `<label>`: three controls per row share one
              caption, and a label wrapping several inputs names none of them.
              `.field` draws the caption; each control carries its own name. */}
          <div className="field full" role="group" aria-label={t("licenses.products.matchRules")}>
            <span>{t("licenses.products.matchRules")}</span>
            {matchRules.map((rule, index) => (
              <div key={index} className="flex items-center gap-2" style={{ marginBottom: 8 }}>
                <select
                  className={inputClass}
                  aria-label={t("licenses.products.ruleType", { number: index + 1 })}
                  value={rule.type}
                  onChange={(e) => updateRule(index, { type: e.target.value as MatchRule["type"] })}
                >
                  {MATCH_RULE_TYPES.map((t) => (
                    <option key={t} value={t}>
                      {MATCH_RULE_LABEL[t]}
                    </option>
                  ))}
                </select>
                <input
                  className={inputClass}
                  placeholder={t("licenses.products.patternPlaceholder")}
                  aria-label={t("licenses.products.rulePattern", { number: index + 1 })}
                  value={rule.pattern}
                  onChange={(e) => updateRule(index, { pattern: e.target.value })}
                />
                <input
                  className={inputClass}
                  placeholder={t("licenses.products.publisherPlaceholder")}
                  aria-label={t("licenses.products.rulePublisher", { number: index + 1 })}
                  value={rule.publisher ?? ""}
                  onChange={(e) => updateRule(index, { publisher: e.target.value || null })}
                />
                {/* `type="button"` is load-bearing now that this is a real
                    `<form>`: ui.tsx's Button sets no type, and an untyped
                    button submits -- Remove would have saved the product. */}
                <Button type="button" variant="ghost" onClick={() => removeRule(index)}>
                  {t("licenses.products.remove")}
                </Button>
              </div>
            ))}
            <Button type="button" variant="outline" onClick={addRule}>
              {t("licenses.products.addRule")}
            </Button>
          </div>
        </div>

        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={close}>
            {t("licenses.common.cancel")}
          </Button>
          {/* Enabled with an empty Name ON PURPOSE (`disabled={... || !name}`
              was the silent no-op), and the failure now goes through
              `describeSubmitError` inside a `role="alert"`:
              `String(mutation.error)` rendered "ApiError: " and nothing after
              it for a bodyless 503, announced to nobody. */}
          <FormActions
            label={t("licenses.common.save")}
            pending={mutation.isPending}
            error={mutation.error}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
    {/* After the dialog, so the question paints on top of it: both are
        `.modal`s at one z-index (coordinator's ruling 3a). */}
    {guard}
    </>
  );
}
