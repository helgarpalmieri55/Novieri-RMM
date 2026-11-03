import { useQuery } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { ConfirmDialog } from "../../components/confirm";
import { Form, FormActions, attemptProblems, describeSubmitError } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Button, Dialog, Field, inputClass, Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDate } from "../../lib/datetime";
import { RowMenu } from "../../components/rowmenu";
import { Icon } from "../../shell/icons";
import {
  useCreatePackageMutation,
  useDeletePackageMutation,
  usePackages,
  useUploadPackageArtifactMutation,
} from "./api";
import { OsIcon } from "../../shell/viz";
import { DeployPageNav } from "./deployNav";
import { osFamilyLabel } from "./pickerOptions";
import type { Package } from "./types";
import { Btn, PageHead, Panel, QueryState } from "../../shell/components";
import { useEverywherePermissions } from "../../lib/mePermissions";

// The design's `P['deploy/packages']`: one bare panel of rows led by the
// package glyph, the sha256 under the name in mono, a platform pill and the
// upload date. The design's rows also print the artefact's size; `Package`
// carries no size, so it is omitted rather than guessed.

// Every field the server requires. `detection_rule` and `uninstall_rule`
// default to `{}` server-side and are not editable here, so they are not rules.
// A function, so the labels are worded when the problems are shown (see `Attempt`).
const packageRules = (): readonly FieldRule[] => [
  { name: "name", label: translate("deploy.packages.ruleName") },
  { name: "version", label: translate("deploy.packages.ruleVersion") },
  { name: "osFamily", label: translate("deploy.packages.ruleOs") },
  { name: "installerType", label: translate("deploy.packages.ruleInstaller") },
];

/** `sha256 9f3a…c21e`: the first and last four hex digits, as the design prints them. */
function shortSha(sha: string): string {
  return sha.length > 12 ? `${sha.slice(0, 4)}…${sha.slice(-4)}` : sha;
}

export function PackagesPage() {
  const t = useT();
  const api = useApi();
  const [createOpen, setCreateOpen] = useState(false);
  // The package awaiting a confirmed delete, or null. The ROW, not the id, so
  // the dialog can name what it is about to remove.
  const [pendingDelete, setPendingDelete] = useState<Package | null>(null);

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("deploy.read");
  // Configuration every client shares (GLOBAL_WRITES): the code on EVERY client (phase 3 design §2).
  const canManage = useEverywherePermissions().has("deploy.package.manage");
  const zone = me.data?.timezone;

  const packages = usePackages();
  const upload = useUploadPackageArtifactMutation();
  const remove = useDeletePackageMutation();
  const fileInputs = useRef<Record<string, HTMLInputElement | null>>({});

  function handleFileChange(pkg: Package, file: File | undefined) {
    if (!file) return;
    upload.mutate({ packageId: pkg.id, file });
  }

  return (
    <div>
      <PageHead
        title={t("deploy.packages.title")}
        description={t("deploy.packages.description")}
        actions={
          canManage ? (
            <Btn label={t("deploy.packages.newPackage")} icon="plus" variant="primary" onClick={() => setCreateOpen(true)} />
          ) : undefined
        }
      />

      <DeployPageNav canRead={canRead} />

      {/* An artifact upload that fails leaves the row reading "no artifact"
          and nothing else: `String(upload.error)` was "ApiError: " for the
          bodyless 503 a reverse proxy returns, so the red line was empty.
          `describeSubmitError` gives it a sentence; `role="alert"` gets it
          announced. */}
      {upload.error ? (
        <p role="alert" className="form-error">
          {describeSubmitError(upload.error)}
        </p>
      ) : null}

      {/* THE SWALLOWED DESTRUCTIVE FAILURE, and it is the exact class Auditor
          A filed as a BLOCKER elsewhere: Delete had NO error branch at all.
          `remove.mutate(pkg.id)` fired, the mutation rejected, and the only
          consequence was that the row stayed put, which reads as a UI that
          has not refreshed yet, not as a refusal. An operator deleting a
          package they believe is gone, on a 403 they were never shown, is a
          worse outcome than a failed upload.

          It names the package because this alert sits above a whole table:
          "That package was not deleted" beside eleven rows is a puzzle. The
          name resolves from the live list, which after a FAILED delete still
          contains the row. */}
      {remove.error ? (
        <p role="alert" className="form-error">
          {t("deploy.packages.deleteFailed", {
            name: packages.data?.find((pkg) => pkg.id === remove.variables)?.name ?? t("deploy.packages.thatPackage"),
            detail: describeSubmitError(remove.error),
          })}
        </p>
      ) : null}

      <Panel bare>
        <QueryState
          loading={packages.isLoading}
          error={packages.error}
          empty={packages.data != null && packages.data.length === 0}
          emptyIcon="package"
          emptyLabel={t("deploy.packages.empty")}
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("deploy.packages.colName")}</Th>
                <Th>{t("deploy.packages.colVersion")}</Th>
                <Th>{t("deploy.packages.colPlatform")}</Th>
                <Th>{t("deploy.packages.colInstaller")}</Th>
                <Th>{t("deploy.packages.colUploaded")}</Th>
                <Th>{""}</Th>
              </tr>
            </thead>
            <tbody>
              {packages.data?.map((pkg) => (
                <tr key={pkg.id}>
                  <Td lead={<Icon name="package" size={18} />}>
                    <div className="cell-title">{pkg.name}</div>
                    {/* The hash the agent verifies, or the fact that there is
                        none yet: a catalog row with no bytes behind it
                        dispatches a job that cannot succeed. */}
                    <div className="cell-sub">
                      {pkg.artifact_sha256 ? (
                        <span className="mono">sha256 {shortSha(pkg.artifact_sha256)}</span>
                      ) : (
                        t("deploy.packages.noFile")
                      )}
                    </div>
                  </Td>
                  <Td>{pkg.version}</Td>
                  {/* PLATFORM IS NOT A SEVERITY. It was a `StatusPill` in
                      the info kind, so "Windows" arrived wearing an
                      info-circle in the accent wash -- the treatment this
                      console reserves for a state somebody may have to act
                      on. It is a property of the row, and the kit already
                      draws platforms one route away: the brand glyph, then
                      the platform's name. */}
                  <Td lead={<OsIcon os={pkg.os_family} />}>{osFamilyLabel(pkg.os_family)}</Td>
                  <Td>{pkg.installer_type}</Td>
                  <Td>
                    <span className="mono">{formatDate(pkg.created_at, zone)}</span>
                  </Td>
                  <Td>
                    {canManage && (
                      <div className="flex justify-end gap-2">
                        <input
                          ref={(el) => {
                            fileInputs.current[pkg.id] = el;
                          }}
                          type="file"
                          className="hidden"
                          aria-label={t("deploy.packages.fileLabel", { name: pkg.name })}
                          onChange={(event) => handleFileChange(pkg, event.target.files?.[0])}
                        />
                        <Btn
                          label={t("deploy.packages.upload")}
                          icon="upload"
                          small
                          disabled={upload.isPending}
                          onClick={() => fileInputs.current[pkg.id]?.click()}
                        />
                        {/* REVIEW WAVE, round 2 (harmony): `ghost` painted
                            this the accent blue with no edge -- a destructive
                            verb dressed as a link, and the same word on
                            /credentials wearing the critical edge. One
                            console, one treatment for delete. */}
                        <RowMenu
                          subject={`${pkg.name} ${pkg.version}`}
                          items={[
                            {
                              label: t("deploy.packages.delete"),
                              destructive: true,
                              disabled: remove.isPending,
                              onSelect: () => setPendingDelete(pkg),
                            },
                          ]}
                        />
                      </div>
                    )}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </QueryState>
      </Panel>

      {/* CYCLE 8. Delete fired on a single click, from what was then a
          `ghost` button sitting beside "Upload artifact": the two
          safe-looking controls in one row, one of which removes a catalog
          entry every existing deployment references by id. The button now
          wears the critical edge as well as asking. There is no undo and no restore route.
          The subject is name AND version because a catalog carries `1.0`,
          `1.1` and `2.0` of the same product as separate rows, and the row is
          the thing people get wrong. */}
      <ConfirmDialog
        open={pendingDelete != null}
        title={t("deploy.packages.confirmTitle")}
        subject={pendingDelete ? `${pendingDelete.name} ${pendingDelete.version}` : ""}
        body={t("deploy.packages.confirmBody")}
        confirmLabel={t("deploy.packages.confirmLabel")}
        destructive
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => {
          if (pendingDelete) remove.mutate(pendingDelete.id);
          setPendingDelete(null);
        }}
      />

      <CreatePackageDialog open={createOpen} onClose={() => setCreateOpen(false)} />
    </div>
  );
}

function CreatePackageDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useT();
  const create = useCreatePackageMutation();
  const [name, setName] = useState("");
  const [version, setVersion] = useState("");
  const [osFamily, setOsFamily] = useState("windows");
  const [installerType, setInstallerType] = useState("msi");
  const [installArgs, setInstallArgs] = useState("");
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  function submit() {
    const next: Attempt = { rules: packageRules, values: { name, version, osFamily, installerType } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    create.mutate(
      {
        // Trimmed on the way out: a name that passed the check with leading
        // whitespace would be stored with it, and then never match a search.
        name: name.trim(),
        version: version.trim(),
        os_family: osFamily.trim(),
        installer_type: installerType.trim(),
        install_args: installArgs.trim() || null,
      },
      {
        onSuccess: () => {
          setName("");
          setVersion("");
          setInstallArgs("");
          setAttempt(null);
          onClose();
        },
      },
    );
  }

  function dismiss() {
    setAttempt(null);
    create.reset();
    onClose();
  }

  return (
    <Dialog open={open} onClose={dismiss} title={t("deploy.packages.createTitle")}>
      <Form onSubmit={submit}>
        <div className="form-grid">
          <Field label={t("deploy.packages.fieldName")}>
            <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label={t("deploy.packages.fieldVersion")}>
            <input
              className={inputClass}
              value={version}
              onChange={(e) => setVersion(e.target.value)}
            />
          </Field>
          <Field label={t("deploy.packages.fieldOs")}>
            <input
              className={inputClass}
              value={osFamily}
              onChange={(e) => setOsFamily(e.target.value)}
            />
          </Field>
          <Field label={t("deploy.packages.fieldInstaller")}>
            <input
              className={inputClass}
              value={installerType}
              onChange={(e) => setInstallerType(e.target.value)}
            />
          </Field>
          <Field label={t("deploy.packages.fieldArgs")} full>
            <input
              className={inputClass}
              value={installArgs}
              onChange={(e) => setInstallArgs(e.target.value)}
            />
          </Field>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          {/* `type="button"`: `ui.tsx`'s Button sets none, and an untyped
              button inside a `<form>` submits it -- Cancel would create. */}
          <Button type="button" variant="ghost" onClick={dismiss}>
            {t("common.actions.cancel")}
          </Button>
          {/* WAS `disabled={create.isPending || !name || !version}`: the
              product-wide silent no-op the form kernel exists to end. The
              click moved nothing and named no field, and the two fields the
              server also requires (OS family, Installer type) were not even
              in the guard. `FormActions` renders the refusal and the server's
              own, and keeps the control enabled so there is something to
              click that answers. */}
          <FormActions
            label={t("deploy.packages.submit")}
            pending={create.isPending}
            error={create.error}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
}
