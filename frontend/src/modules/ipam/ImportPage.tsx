import { useRef, useState } from "react";
import { describeSubmitError } from "../../components/form";
import { Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { Trans } from "../../i18n/Trans";
import { useApi } from "../../lib/ApiContext";
import { useClientName } from "../settings/api";
import {
  useApplyIpamImportMutation,
  useConvertIpamDocRecordsMutation,
  useIpamClient,
  useIpamPermissions,
  useIpamVrfs,
  usePreviewIpamImportMutation,
} from "./api";
import { IpamNav } from "./IpamNav";
import { DetailFacts } from "./DetailFacts";
import { ErrorLine } from "./ErrorLine";
import {
  describeImportDetail,
  importActionPresentation,
  formatCount,
  importApplyBlockReason,
  type PreviewedFile,
} from "./ipamState";
import { variantKind } from "./kinds";
import type { ImportRowOut, VrfOut } from "./types";
import { IMPORT_FIELDS, MAX_IMPORT_BYTES, MAX_IMPORT_ROWS } from "./types";
import { Icon } from "../../shell/icons";
import { Btn, PageHead, Panel, StatusPill } from "../../shell/components";

// Import is TWO STEPS BOUND BY A DIGEST, and this page cannot be used any other
// way.
//
// ON THE v3 KIT (`P['ipam/import']` in docs/design/itms-prototype-v3.html):
// one panel with the file field, its help line and the primary preview
// action; the verdict table under it once a preview exists. The design's
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
  const permissions = useIpamPermissions();
  return (
    <div>
      <PageHead
        title={t("ipam.importExport.title")}
        description={t("ipam.importExport.description")}
      />
      <IpamNav />
      {permissions.manage ? (
        <>
          <CsvImportPanel />
          <DocRecordPanel />
        </>
      ) : (
        <div className="notice" style={{ marginBottom: 16 }}>
          <Icon name="info" size={16} />
          <p>{t("ipam.importExport.needsManage")}</p>
        </div>
      )}
      <ExportPanel />
    </div>
  );
}

/**
 * The VRF an import goes into until the operator picks another: the internal
 * client's `global` one when it is listed, else the first listed -- the VRFs
 * listed are exactly those of the clients the operator can see, so a
 * technician limited to one client starts on theirs. `null` with none listed:
 * the import then names the internal client (`ipam/api.ts`, PHASE 3).
 */
function defaultImportVrf(vrfs: readonly VrfOut[], client: string | null): string | null {
  const internalGlobal = vrfs.find((vrf) => vrf.client_id === client && vrf.name === "global");
  return internalGlobal?.id ?? vrfs[0]?.id ?? null;
}

function CsvImportPanel() {
  const t = useT();
  const preview = usePreviewIpamImportMutation();
  const apply = useApplyIpamImportMutation();
  // WHERE THE SHEET GOES. The import's client is the chosen VRF's (operator
  // decision 2026-10-28: "use the row's client"), named by the client's name
  // for someone holding `clients.read`, its id otherwise.
  const vrfs = useIpamVrfs();
  const client = useIpamClient();
  const clientName = useClientName();
  const [vrfChoice, setVrfChoice] = useState<string | null>(null);
  const vrfId = vrfChoice ?? defaultImportVrf(vrfs.data ?? [], client);
  const [file, setFile] = useState<File | null>(null);
  const [columns, setColumns] = useState<string[]>([]);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  // The digest AND the identity of the file it belongs to. Storing the digest
  // alone would let it outlive the file it describes.
  const [previewed, setPreviewed] = useState<PreviewedFile | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

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
    // The picker no longer shows the VRF the preview read -- a choice made
    // since, or a list that first appeared after a preview into the internal
    // client. Apply would still go where the preview read; say so instead.
    (previewed !== null && previewed.vrfId !== vrfId ? t("ipam.importExport.targetMoved") : null);
  const mappingNamesAddress = Object.values(mapping).includes("address");

  return (
    <Panel
      title={t("ipam.importExport.csvTitle")}
      subtitle={t("ipam.importExport.csvSubtitle", {
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
      <p className="note" style={{ marginTop: -8, marginBottom: 14 }}>
        {t("ipam.importExport.columns", { fields: IMPORT_FIELDS.join(", ") })}
      </p>

      {columns.length > 0 && (
        <div style={{ marginBottom: 14 }}>
          <h3 className="panel-title" style={{ marginBottom: 8 }}>
            {t("ipam.importExport.mapping")}
          </h3>
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

      <div className="form-actions" style={{ marginTop: 0 }}>
        <Btn
          label={preview.isPending ? t("ipam.importExport.previewing") : t("ipam.importExport.preview")}
          icon="eye"
          variant="primary"
          disabled={preview.isPending || file === null || !mappingNamesAddress}
          onClick={() => {
            if (file === null) return;
            // THE TARGET IS TAKEN ONCE, HERE. `vrfId` is recomputed on every
            // render from the choice or the default, and a default can move
            // when the VRF list is read again; what was previewed is what is
            // applied, and the picker is pinned to it so it says so.
            const target = vrfId;
            preview.mutate(
              { file, mapping, vrfId: target },
              {
                onSuccess: (result) => {
                  setVrfChoice(target);
                  setPreviewed({
                    name: file.name,
                    size: file.size,
                    lastModified: file.lastModified,
                    digest: result.digest,
                    vrfId: target,
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
            apply.mutate({ file, mapping, digest: previewed.digest, vrfId: previewed.vrfId });
          }}
        />
        {blockReason !== null && <span className="form-hint">{blockReason}</span>}
      </div>

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
    </Panel>
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
function DocRecordPanel() {
  const t = useT();
  const convert = useConvertIpamDocRecordsMutation();
  return (
    <Panel
      title={t("ipam.importExport.convertTitle")}
      subtitle={t("ipam.importExport.convertSubtitle")}
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
    </Panel>
  );
}

function ExportPanel() {
  const t = useT();
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

  return (
    <Panel
      title={t("ipam.importExport.exportTitle")}
      subtitle={t("ipam.importExport.exportSubtitle")}
    >
      <p className="note" style={{ marginBottom: 12 }}>
        <Trans k="ipam.importExport.dormantNote" components={{ b: <b /> }} />
      </p>
      <div className="form-actions" style={{ marginTop: 0 }}>
        <Btn label={t("ipam.importExport.subnetsCsv")} icon="download" onClick={() => void download("subnets")} />
        <Btn label={t("ipam.importExport.addressesCsv")} icon="download" onClick={() => void download("addresses")} />
        <Btn label={t("ipam.importExport.driftCsv")} icon="download" onClick={() => void download("drift")} />
      </div>
      {error !== null && (
        <p role="alert" className="form-error" style={{ marginTop: 10 }}>
          {describeSubmitError(error)}
        </p>
      )}
    </Panel>
  );
}
