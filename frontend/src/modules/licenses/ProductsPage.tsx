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
import { PageHead, Panel, QueryState } from "../../shell/components";
import { Icon } from "../../shell/icons";

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

export function ProductsPage() {
  const t = useT();
  const api = useApi();
  const [editing, setEditing] = useState<ProductOut | "new" | null>(null);

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("licenses.read");
  const canManage = permissions.includes("licenses.manage");

  const products = useProducts();
  // v3's "Detected" column: what inventory found installed, per product. The
  // compliance report is the one read that carries that number, and it is
  // already cached from `/licenses`. A failed read draws nothing in the
  // column rather than a zero that reads as "nothing installed".
  const compliance = useCompliance();
  const installedByProduct = useMemo(
    () => new Map((compliance.data ?? []).map((row) => [row.product_id, row.installed])),
    [compliance.data],
  );
  const items = products.data ?? [];

  return (
    <div>
      <PageHead
        title={t("licenses.products.title")}
        description={t("licenses.products.description")}
        actions={
          canManage ? (
            <Button onClick={() => setEditing("new")}>
              <Icon name="plus" size={14} />
              {t("licenses.products.newProduct")}
            </Button>
          ) : undefined
        }
      />

      <LicensePageNav canRead={canRead} />

      <Panel bare>
        <QueryState
          loading={products.isLoading}
          error={products.error}
          empty={items.length === 0}
          emptyLabel={t("licenses.products.empty")}
          emptyIcon="license"
        >
          <Table>
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
              {items.map((product) => {
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
                    <Td>
                      {installed === undefined ? (
                        t("licenses.common.na")
                      ) : (
                        <span className="mono">{t("licenses.products.installs", { count: installed })}</span>
                      )}
                    </Td>
                    <Td>
                      {canManage && (
                        <Button variant="outline" onClick={() => setEditing(product)}>
                          {t("licenses.common.edit")}
                        </Button>
                      )}
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        </QueryState>
      </Panel>

      <ProductEditor product={editing} onClose={() => setEditing(null)} />
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

  function reset() {
    setName("");
    setPublisher("");
    setMatchRules([]);
    setAttempt(null);
  }

  // ProductsPage keeps this editor mounted and toggles `product`, so without
  // clearing the mutation here a failure from one attempt sits in the form
  // waiting to be read as the next one's.
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
    const next: Attempt = { rules: productRules, values: { name } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;

    const body = { name: name.trim(), publisher, match_rules: matchRules };
    const onSuccess = () => {
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
  );
}
