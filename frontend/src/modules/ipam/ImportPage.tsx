import { useRef, useState } from "react";
import { ClientField } from "../../components/ClientField";
import { attemptProblems, describeSubmitError, type Attempt } from "../../components/form";
import { Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { Trans } from "../../i18n/Trans";
import { useApi } from "../../lib/ApiContext";
import { useClientScope } from "../../lib/clientScope";
import { useNewRowClientField } from "../../lib/clientSelection";
import { useClientName } from "../settings/api";
import {
  useApplyIpamImportMutation,
  useConvertIpamDocRecordsMutation,
  useIpamPermissions,
  useIpamVrfs,
  usePreviewIpamImportMutation,
} from "./api";
import { IpamNav } from "./IpamNav";
import { DetailFacts } from "./DetailFacts";
import { ErrorLine } from "./ErrorLine";
import { clientRule, ProblemLine } from "./NewRowClient";
import { UnassignedRefusal } from "../../components/UnassignedRefusal";
import {
  describeImportDetail,
  importActionPresentation,
  formatCount,
  importApplyBlockReason,
  type PreviewedFile,
} from "./ipamState";
import { variantKind } from "./kinds";
import type { ImportRowOut, VrfOut } from "./types";
import { IMPORT_FIELDS, IPAM_MANAGE, MAX_IMPORT_BYTES, MAX_IMPORT_ROWS } from "./types";
import { Icon } from "../../shell/icons";
import { Btn, FormSection, PageHead, StatusPill, UnsavedGuard } from "../../shell/components";
import { Steps, type StepState } from "../../shell/viz";
import { useRecordTitle } from "../../shell/title";

// Import is TWO STEPS BOUND BY A DIGEST, and this page cannot be used any other
// way.
//
// ON THE KIT (redesign plan, Receta O; Task 10, 5.11): the wizard's steps at
// the top (`Steps`), one folding section per step -- the file and where it
// goes, its columns, the preview, the apply -- and the three exports as the
// head's secondary actions. A file chosen and not applied asks before the
// page is left. The design's
// source chips (CSV, Windows DHCP, phpIPAM) are not drawn: CSV is the one
// source the API accepts, and a chip row with one chip is a control that
// switches nothing.
//
// Preview writes nothing and returns a sha256 of the file it read. Apply must
// echo that digest. The guard only means something if the file being applied is
// the file whose verdict somebody actually read, so the previewed file's
// identity is stored alongside its digest, and choosing a different file clears
// the preview and disables Apply. A page that let you preview one CSV, swap in
// another and apply defeats the whole mechanism; the server would still refuse
// the stale digest, but the operator would have made a decision on evidence
// about a different file.
//
// APPLY IS ONE TRANSACTION: a failure on the last row leaves zero rows written.
// And every input row gets a verdict, including the unparseable ones: reported,
// never silently skipped.
export function ImportPage() {
  const t = useT();
  // A sub-page without a rail entry names itself in the tab, as its h1 does
  // (the coordinator's ruling 1b): "<this page> · <rail entry> · Novieri RMM".
  useRecordTitle(t("ipam.importExport.title"));
  const permissions = useIpamPermissions();
  const { selected } = useClientScope();
  const exports = useCsvExport();
  return (
    <div>
      <PageHead
        title={t("ipam.importExport.title")}
        description={t("ipam.importExport.description")}
        actions={
          <>
            <Btn label={t("ipam.importExport.subnetsCsv")} icon="download" onClick={() => void exports.download("subnets")} />
            <Btn label={t("ipam.importExport.addressesCsv")} icon="download" onClick={() => void exports.download("addresses")} />
            <Btn label={t("ipam.importExport.driftCsv")} icon="download" onClick={() => void exports.download("drift")} />
          </>
        }
      />
      <IpamNav />
      {/* What the head's three exports hold, once, under them. */}
      <p className="note" style={{ marginBottom: 16 }}>
        {t("ipam.importExport.exportSubtitle")}{" "}
        <Trans k="ipam.importExport.dormantNote" components={{ b: <b /> }} />
      </p>
      {exports.error !== null && (
        <p role="alert" className="form-error" style={{ marginBottom: 16 }}>
          {describeSubmitError(exports.error)}
        </p>
      )}
      {permissions.manage ? (
        <>
          {/* Keyed by the top bar's client: a switch of client drops the chosen
              file, its mapping and its target instead of carrying them to the
              new client (the coordinator's ruling 12). */}
          <CsvImportWizard key={selected ?? ""} />
          <DocRecordSection />
        </>
      ) : (
        <div className="notice" style={{ marginBottom: 16 }}>
          <Icon name="info" size={16} />
          <p>{t("ipam.importExport.needsManage")}</p>
        </div>
      )}
    </div>
  );
}

/**
 * The VRF an import goes into until the operator picks another: the `global`
 * VRF of the client chosen at the top when it is listed, else the first
 * listed -- the VRFs listed are exactly those of the clients the operator can
 * see, and each names its client in the picker. `null` with none listed: the
 * import then names a client (`ipam/api.ts`), the one chosen at the top or
 * the one the Client field names.
 */
function defaultImportVrf(vrfs: readonly VrfOut[], selected: string | null): string | null {
  const selectedGlobal = vrfs.find((vrf) => vrf.client_id === selected && vrf.name === "global");
  return selectedGlobal?.id ?? vrfs[0]?.id ?? null;
}

function CsvImportWizard() {
  const t = useT();
  const preview = usePreviewIpamImportMutation();
  const apply = useApplyIpamImportMutation();
  // WHERE THE SHEET GOES. The import's client is the chosen VRF's (operator
  // decision 2026-10-28: "use the row's client"), named by the client's name
  // for someone holding `clients.read`, its id otherwise.
  const vrfs = useIpamVrfs();
  const { selected } = useClientScope();
  // The digest AND the identity of the file it belongs to. Storing the digest
  // alone would let it outlive the file it describes.
  const [previewed, setPreviewed] = useState<PreviewedFile | null>(null);
  // With no VRF listed, the import names a client: the one chosen at the top,
  // or with every client the Client field's (design §3.1). Another client is
  // another import: the verdict was not about it.
  const newRow = useNewRowClientField(IPAM_MANAGE, undefined, {
    onPick: () => {
      setPreviewed(null);
      apply.reset();
      preview.reset();
    },
  });
  const client = newRow.clientId;
  const clientName = useClientName();
  const [vrfChoice, setVrfChoice] = useState<string | null>(null);
  const vrfId = vrfChoice ?? defaultImportVrf(vrfs.data ?? [], selected);
  const [file, setFile] = useState<File | null>(null);
  const [columns, setColumns] = useState<string[]>([]);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  // Populated on Preview, never while choosing (the form kernel's rule).
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  // Once the VRF read has settled, not while it loads: the list may yet name
  // a VRF, which decides.
  const namesClient = !vrfs.isPending && (vrfs.data?.length ?? 0) === 0;

  async function chooseFile(chosen: File | null): Promise<void> {
    setFile(chosen);
    // A new file invalidates the previous verdict AND its digest.
    setPreviewed(null);
    setMapping({});
    setColumns([]);
    apply.reset();
    preview.reset();
    if (chosen === null) return;
    const head = await chosen.slice(0, 8192).text();
    const firstLine = head.split(/\r?\n/)[0] ?? "";
    setColumns(firstLine.split(",").map((column) => column.trim().replace(/^﻿/, "")));
  }

  const blockReason =
    importApplyBlockReason(
      file === null
        ? null
        : { name: file.name, size: file.size, lastModified: file.lastModified },
      previewed,
    ) ??
    // The picker no longer shows the target the preview read -- a choice made
    // since, a list that first appeared after a preview into a client's
    // `global` VRF, or another client named since. Apply would still go where
    // the preview read; say so instead.
    (previewed !== null &&
    (previewed.vrfId !== vrfId || (vrfId === null && previewed.clientId !== client))
      ? t("ipam.importExport.targetMoved")
      : null);
  const mappingNamesAddress = Object.values(mapping).includes("address");

  // WHERE THE WIZARD STANDS, said three ways by the kit's steps (the bar, the
  // pill and the words), from the same state the buttons are enabled by.
  const step = (done: boolean, reached: boolean): StepState => (done ? "done" : reached ? "now" : "todo");
  const steps = [
    { label: t("ipam.importExport.stepFile"), state: step(file !== null, true) },
    { label: t("ipam.importExport.stepMap"), state: step(file !== null && mappingNamesAddress, file !== null) },
    { label: t("ipam.importExport.preview"), state: step(previewed !== null, file !== null && mappingNamesAddress) },
    { label: t("ipam.importExport.apply"), state: step(apply.data !== undefined, previewed !== null) },
  ];

  return (
    <>
      {/* A FILE CHOSEN AND NOT APPLIED ASKS BEFORE THE PAGE IS LEFT: the
          mapping and the verdict read would be lost with it. Applied, it
          leaves without asking. */}
      <UnsavedGuard dirty={file !== null && apply.data === undefined} />
      <div style={{ marginBottom: 16 }}>
        <Steps items={steps} />
      </div>

      <FormSection
        title={t("ipam.importExport.csvTitle")}
        icon="upload"
        description={t("ipam.importExport.csvSubtitle", {
          megabytes: (MAX_IMPORT_BYTES / (1024 * 1024)).toFixed(0),
          rows: formatCount(MAX_IMPORT_ROWS),
        })}
      >
      <p className="note" style={{ marginBottom: 14 }}>
        {t("ipam.importExport.csvNote")}
      </p>

      {(vrfs.data?.length ?? 0) > 0 && (
        <Field label={t("ipam.importExport.vrf")} help={t("ipam.importExport.vrfHelp")}>
          <select
            className={inputClass}
            value={vrfId ?? ""}
            onChange={(e) => {
              setVrfChoice(e.target.value);
              // Another VRF is another import: the verdict read so far was
              // about the old one, and its digest does not cover the target.
              setPreviewed(null);
              apply.reset();
              preview.reset();
            }}
          >
            {vrfs.data?.map((vrf) => (
              <option key={vrf.id} value={vrf.id}>
                {t("ipam.importExport.vrfOption", { vrf: vrf.name, client: clientName(vrf.client_id) })}
              </option>
            ))}
          </select>
        </Field>
      )}
      {namesClient && newRow.needsField ? <ClientField field={newRow} /> : null}
      {/* With Unassigned chosen at the top and no VRF to import into, nothing
          new can be filed: the panel says so instead of offering a preview
          that can only be refused. */}
      {namesClient && newRow.refused === "unassigned" ? <UnassignedRefusal /> : null}

      <Field label={t("ipam.importExport.csvFile")}>
        {/* THE NATIVE CONTROL STAYS -- it is what the browser's picker and the
            tests drive -- but it is not what the operator looks at. Wearing
            `inputClass` it rendered the user agent's own "Choose File / No
            file chosen" inside the kit's box: unstyled, title-cased, in a
            column of kit controls. v3 draws this field as a dashed drop
            target reading "Drop a CSV or click" (`P['ipam/import']`), which
            is the same idiom the portal's attachment field already uses.

            A real `<button>`, so it is a tab stop and takes the global focus
            ring; the dashed edge is all it borrows from `.inp`. AND IT REALLY
            TAKES A DROP: `onDrop` reads `dataTransfer.files` through the same
            `chooseFile` the picker calls, so a dropped file invalidates the
            previous digest exactly as a picked one does. */}
        <input
          ref={fileInput}
          type="file"
          accept=".csv,text/csv"
          className="hidden"
          aria-label={t("ipam.importExport.csvFile")}
          onChange={(e) => void chooseFile(e.target.files?.[0] ?? null)}
        />
        <button
          type="button"
          className="inp focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]"
          style={{
            borderStyle: "dashed",
            justifyContent: "center",
            color: "var(--muted)",
            font: "inherit",
            cursor: "pointer",
          }}
          onClick={() => fileInput.current?.click()}
          onDragOver={(event) => event.preventDefault()}
          onDrop={(event) => {
            event.preventDefault();
            const dropped = event.dataTransfer.files[0];
            if (dropped) void chooseFile(dropped);
          }}
        >
          <Icon name="upload" size={14} />
          <span>{file === null ? t("ipam.importExport.drop") : file.name}</span>
        </button>
      </Field>
      <p className="note" style={{ marginTop: -8 }}>
        {t("ipam.importExport.columns", { fields: IMPORT_FIELDS.join(", ") })}
      </p>
      </FormSection>

      <FormSection title={t("ipam.importExport.mapping")} icon="list">
      {columns.length === 0 ? (
        <p className="note">{t("ipam.importExport.mapAfterFile")}</p>
      ) : (
        <div>
          <div className="form-grid">
            {columns.map((column) => (
              <Field key={column} label={column || t("ipam.importExport.unnamed")}>
                <select
                  className={inputClass}
                  value={mapping[column] ?? ""}
                  onChange={(e) => {
                    const next = { ...mapping };
                    if (e.target.value === "") delete next[column];
                    else next[column] = e.target.value;
                    setMapping(next);
                    // Changing the mapping changes what preview computed, so
                    // the previous verdict no longer describes what apply
                    // would do.
                    setPreviewed(null);
                  }}
                >
                  <option value="">{t("ipam.importExport.ignoreColumn")}</option>
                  {IMPORT_FIELDS.map((field) => (
                    <option key={field} value={field}>
                      {field}
                    </option>
                  ))}
                </select>
              </Field>
            ))}
          </div>
          {!mappingNamesAddress && (
            <p className="form-error" style={{ marginTop: 8 }}>
              <Trans k="ipam.importExport.mustMapAddress" components={{ mono: <span className="mono" /> }} />
            </p>
          )}
        </div>
      )}
      </FormSection>

      <FormSection title={t("ipam.importExport.stepPreviewApply")} icon="eye">

      {namesClient && newRow.refused === "unassigned" ? null : (
        <div className="form-actions" style={{ marginTop: 0 }}>
          <Btn
            label={preview.isPending ? t("ipam.importExport.previewing") : t("ipam.importExport.preview")}
            icon="eye"
            variant="primary"
            disabled={preview.isPending || file === null || !mappingNamesAddress}
            onClick={() => {
              if (file === null) return;
              // The Client field, when the import names a client (no VRF listed).
              const next: Attempt = { rules: () => (namesClient ? clientRule(newRow) : []), values: { client } };
              setAttempt(next);
              if (attemptProblems(next).length > 0) {
                preview.reset();
                return;
              }
              // THE TARGET IS TAKEN ONCE, HERE. `vrfId` is recomputed on every
              // render from the choice or the default, and a default can move
              // when the VRF list is read again; what was previewed is what is
              // applied, and the picker is pinned to it so it says so.
              const target = vrfId;
              const targetClient = client;
              preview.mutate(
                { file, mapping, vrfId: target, clientId: targetClient },
                {
                  onSuccess: (result) => {
                    setVrfChoice(target);
                    setPreviewed({
                      name: file.name,
                      size: file.size,
                      lastModified: file.lastModified,
                      digest: result.digest,
                      vrfId: target,
                      clientId: targetClient,
                    });
                  },
                },
              );
            }}
          />
          <Btn
            label={apply.isPending ? t("ipam.importExport.applying") : t("ipam.importExport.apply")}
            icon="check"
            disabled={apply.isPending || blockReason !== null}
            onClick={() => {
              if (file === null || previewed === null) return;
              apply.mutate({
                file,
                mapping,
                digest: previewed.digest,
                vrfId: previewed.vrfId,
                clientId: previewed.clientId,
              });
            }}
          />
          {blockReason !== null && <span className="form-hint">{blockReason}</span>}
        </div>
      )}

      <ProblemLine problems={attemptProblems(attempt)} />
      {preview.isError && <ErrorLine error={preview.error} />}
      {apply.isError && <ErrorLine error={apply.error} />}

      {preview.data && (
        <div style={{ marginTop: 16 }}>
          <h3 className="panel-title">{t("ipam.importExport.previewTitle")}</h3>
          <div
            style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8, margin: "8px 0 10px" }}
          >
            <StatusPill value="create" kind="ok" label={t("ipam.importExport.pillCreate", { count: preview.data.creates })} />
            <StatusPill value="update" kind="info" label={t("ipam.importExport.pillUpdate", { count: preview.data.updates })} />
            <StatusPill value="conflict" kind="warn" label={t("ipam.importExport.pillConflict", { count: preview.data.conflicts })} />
            <StatusPill value="error" kind="crit" label={t("ipam.importExport.pillError", { count: preview.data.errors })} />
            <span className="note">
              <Trans
                k="ipam.importExport.digestLine"
                vars={{ count: preview.data.total_rows, rows: preview.data.total_rows, digest: preview.data.digest.slice(0, 12) }}
                components={{ mono: <span className="mono" /> }}
              />
            </span>
          </div>
          <RowTable rows={preview.data.rows} />
        </div>
      )}

      {apply.data && (
        <div style={{ marginTop: 16 }}>
          <h3 className="panel-title">{t("ipam.importExport.appliedTitle")}</h3>
          <p style={{ margin: "4px 0 10px" }}>
            {t("ipam.importExport.appliedLine", {
              applied: apply.data.applied,
              total: apply.data.total_rows,
              errors: apply.data.errors,
            })}
          </p>
          <RowTable rows={apply.data.rows} />
        </div>
      )}
      </FormSection>
    </>
  );
}

function RowTable({ rows }: { rows: ImportRowOut[] }) {
  const t = useT();
  const shown = rows.slice(0, 200);
  return (
    <>
      <Table>
        <thead>
          <tr>
            <Th>{t("ipam.importExport.colLine")}</Th>
            <Th>{t("ipam.importExport.colVerdict")}</Th>
            <Th>{t("ipam.fields.address")}</Th>
            <Th>{t("ipam.importExport.colDetail")}</Th>
          </tr>
        </thead>
        <tbody>
          {shown.map((row) => {
            const presentation = importActionPresentation(row.action);
            return (
              <tr key={`${row.line}-${row.address}`}>
                <Td>
                  <span className="mono">{row.line}</span>
                </Td>
                <Td>
                  <span title={presentation.meaning}>
                    <StatusPill
                      value={row.action}
                      kind={variantKind(presentation.variant)}
                      label={presentation.label}
                    />
                  </span>
                </Td>
                <Td>
                  <span className="mono">{row.address}</span>
                </Td>
                {/* THE DETAIL CELL USED TO BE `row.error ||
                    JSON.stringify(row.detail)`: a clean row read
                    `{"asset_label":"Laptop 4"}` and a row carrying nothing read
                    `{}`. Same engine as the drift evidence and the address
                    history, so the module has one answer to "what does a
                    free-form payload look like" rather than three. */}
                <Td>
                  {row.error ? (
                    <span className="note">{row.error}</span>
                  ) : (
                    <DetailFacts
                      facts={describeImportDetail(row)}
                      empty={t("ipam.importExport.nothingFurther")}
                    />
                  )}
                </Td>
              </tr>
            );
          })}
        </tbody>
      </Table>
      {rows.length > shown.length && (
        <p className="note" style={{ marginTop: 8 }}>
          {t("ipam.importExport.showingFirst", { shown: shown.length, total: rows.length })}
        </p>
      )}
    </>
  );
}

// The documentation absorption. NOT a data migration: a silent conversion would
// hit unparseable CIDRs and duplicate prefixes with no operator to ask, so it is
// operator-invoked and dry-runs by default.
function DocRecordSection() {
  const t = useT();
  const convert = useConvertIpamDocRecordsMutation();
  return (
    <FormSection
      title={t("ipam.importExport.convertTitle")}
      icon="file-text"
      description={t("ipam.importExport.convertSubtitle")}
    >
      <div className="form-actions" style={{ marginTop: 0 }}>
        <Btn
          label={t("ipam.importExport.dryRun")}
          icon="eye"
          disabled={convert.isPending}
          onClick={() => convert.mutate(true)}
        />
        <Btn
          label={t("ipam.importExport.convertReal")}
          icon="check"
          disabled={convert.isPending}
          onClick={() => convert.mutate(false)}
        />
      </div>
      {convert.isError && <ErrorLine error={convert.error} />}
      {convert.data && (
        <div style={{ marginTop: 14 }}>
          {convert.data.dry_run ? (
            <StatusPill value="dry-run" kind="info" label={t("ipam.importExport.dryRunPill")} />
          ) : (
            <StatusPill value="converted" kind="ok" label={t("ipam.importExport.converted")} />
          )}
          <p style={{ marginTop: 8 }}>
            {t("ipam.importExport.convertible", {
              count: convert.data.converted.length,
              skipped: convert.data.skipped.length,
            })}
          </p>
          <ul className="note" style={{ margin: "8px 0 0", paddingLeft: 18 }}>
            {convert.data.converted.map((row) => (
              <li key={row.record_id}>
                <span className="mono">{row.prefix}</span>: {row.name}
                {row.warnings.length > 0 && <span> ({row.warnings.join("; ")})</span>}
              </li>
            ))}
            {convert.data.skipped.map((row) => (
              <li key={row.record_id}>
                {t("ipam.importExport.skipped", { name: row.name, reason: row.reason })}
              </li>
            ))}
          </ul>
        </div>
      )}
    </FormSection>
  );
}

/** The three CSV exports, as the head's actions: each downloads, and a refusal is said once under the head. */
function useCsvExport(): { download: (kind: "subnets" | "addresses" | "drift") => Promise<void>; error: unknown } {
  const api = useApi();
  // The failure itself, not its sentence: the sentence is worded at render, so a
  // language switch re-words a refusal that is already on screen.
  const [error, setError] = useState<unknown>(null);

  async function download(kind: "subnets" | "addresses" | "drift"): Promise<void> {
    setError(null);
    try {
      const body =
        kind === "subnets"
          ? await api.exportIpamSubnetsCsv()
          : kind === "addresses"
            ? await api.exportIpamAddressesCsv()
            : await api.exportIpamDriftCsv();
      const url = URL.createObjectURL(new Blob([body], { type: "text/csv" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = `ipam-${kind}.csv`;
      link.click();
      URL.revokeObjectURL(url);
    } catch (err: unknown) {
      setError(err);
    }
  }

  return { download, error };
}
