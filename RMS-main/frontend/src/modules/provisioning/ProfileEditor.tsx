import { useState } from "react";
import { Link } from "react-router-dom";
import { Form, FormActions, attemptProblems } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { EntityPicker } from "../../components/picker";
import { Button, Dialog, Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { Pill } from "../../shell/viz";
import { useCreateProvisionProfileMutation, useOsImages, useUpdateProvisionProfileMutation } from "./api";
import { imagePickerEmptyHint, imagePickerOptions } from "./pickerOptions";
import type { ProvisionProfile } from "./types";

/**
 * What a profile may not be saved without.
 *
 * Both are required server-side (`ProfileUpsertRequest.name`/`image_id`), and
 * both were enforced here by `disabled={… || !name || !imageId}` — the silent
 * no-op the form kernel exists to retire: the one expression that knew what
 * was missing was also the one that said nothing.
 */
const profileRules = (): readonly FieldRule[] => [
  { name: "name", label: translate("provisioning.editor.name") },
  { name: "imageId", label: translate("provisioning.editor.image") },
];

/** A framed group of fields, on the kit's tokens: no class in the kit draws a fieldset yet. */
const FIELDSET_STYLE = {
  border: "1px solid var(--line)",
  borderRadius: "var(--r-panel)",
  padding: 12,
  margin: 0,
} as const;

const LEGEND_STYLE = {
  display: "inline-flex",
  alignItems: "center",
  gap: 8,
  padding: "0 4px",
  fontWeight: 500,
} as const;

// Secret fields (domain-join password, local-admin password) are WRITE-ONLY:
// the API never returns them (see api.py._redact_post_config /
// ProfileOut.has_*_credential), so this editor never prefills a password
// input on edit — leaving one blank keeps the existing sealed credential
// untouched. Because PUT /provisioning/profiles/{id} is a full-body upsert,
// naively posting an empty post_config would clobber the sealed credential,
// so on submit this editor carries forward the loaded profile's post_config
// (mirrors BackupPolicyEditor's PolicyDialog carrying forward
// sources/retention_gfs) and only overwrites a password sub-field when the
// operator actually typed a new value.
export function ProfileEditor({
  profile,
  onClose,
}: {
  profile: ProvisionProfile | "new" | null;
  onClose: () => void;
}) {
  const t = useT();
  const images = useOsImages();
  const create = useCreateProvisionProfileMutation();
  const update = useUpdateProvisionProfileMutation();

  const existing = profile === "new" || profile == null ? null : profile;
  const existingPostConfig = (existing?.post_config ?? {}) as Record<string, unknown>;
  const existingDomainJoin = (existingPostConfig.domain_join ?? {}) as Record<string, unknown>;
  const existingLocalAdmin = (existingPostConfig.local_admin ?? {}) as Record<string, unknown>;

  const [name, setName] = useState(existing?.name ?? "");
  const [imageId, setImageId] = useState(existing?.image_id ?? "");
  const [enabled, setEnabled] = useState(existing?.enabled ?? true);
  const [domain, setDomain] = useState(String(existingDomainJoin.domain ?? ""));
  const [domainUsername, setDomainUsername] = useState(String(existingDomainJoin.username ?? ""));
  const [domainPassword, setDomainPassword] = useState("");
  const [localAdminUsername, setLocalAdminUsername] = useState(
    String(existingLocalAdmin.username ?? ""),
  );
  const [localAdminPassword, setLocalAdminPassword] = useState("");
  // Populated ON SUBMIT, never while typing. Held as data (rules and values) so
  // a language switch re-words the sentences. The values are names, never a password.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  const mutation = existing ? update : create;

  const imageOptions = imagePickerOptions(images.data ?? []);
  // Gated on a settled query so a slow images fetch never reads as an empty
  // one: "nothing exists" and "nothing has arrived yet" are different states.
  const noImages = images.isSuccess && (images.data?.length ?? 0) === 0;

  function submit() {
    // The previous outcome must not be readable as this one.
    mutation.reset();
    const next: Attempt = { rules: profileRules, values: { name, imageId } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;

    // Carry forward the existing post_config (already secret-redacted by the
    // backend) and layer the operator's non-secret edits + any newly typed
    // passwords on top, so an untouched password field never clobbers the
    // profile's existing sealed credential.
    const domainJoin: Record<string, unknown> = { ...existingDomainJoin };
    if (domain) domainJoin.domain = domain;
    else delete domainJoin.domain;
    if (domainUsername) domainJoin.username = domainUsername;
    else delete domainJoin.username;
    if (domainPassword) domainJoin.password = domainPassword;

    const localAdmin: Record<string, unknown> = { ...existingLocalAdmin };
    if (localAdminUsername) localAdmin.username = localAdminUsername;
    else delete localAdmin.username;
    if (localAdminPassword) localAdmin.password = localAdminPassword;

    const post_config: Record<string, unknown> = { ...existingPostConfig };
    if (Object.keys(domainJoin).length > 0) post_config.domain_join = domainJoin;
    else delete post_config.domain_join;
    if (Object.keys(localAdmin).length > 0) post_config.local_admin = localAdmin;
    else delete post_config.local_admin;

    const body = {
      name,
      image_id: imageId,
      post_config,
      target_scope: existing?.target_scope ?? {},
      enabled,
    };

    const onSuccess = () => {
      setDomainPassword("");
      setLocalAdminPassword("");
      onClose();
    };

    if (existing) {
      update.mutate({ id: existing.id, body }, { onSuccess });
    } else {
      create.mutate(body, { onSuccess });
    }
  }

  return (
    <Dialog
      open={profile != null}
      onClose={onClose}
      title={profile === "new" ? t("provisioning.editor.titleNew") : t("provisioning.editor.titleEdit")}
    >
      {/* A real <form>, so Enter in Name saves the profile. It did nothing
          before: there was no <form> on this dialog at all. */}
      <Form onSubmit={submit}>
        <div className="form-grid">
          <Field label={t("provisioning.editor.name")} full>
            <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
          </Field>

          {/* CYCLES 6 AND 7 — the second empty-dropdown dead end in this module.
              With no OS images registered this `<select>` offered only its own
              placeholder, so a profile could not be completed and the screen
              said nothing about why. `EntityPicker` renders `emptyHint` in
              place of the empty list, and the link names the surface that fills
              it. The sublabel is `os_family · kind · state`: binding a profile
              to a draft image is a build that fails at PXE time rather than at
              edit time. */}
          <EntityPicker
            label={t("provisioning.editor.image")}
            options={imageOptions}
            value={imageId}
            onChange={setImageId}
            loading={images.isLoading}
            emptyHint={imagePickerEmptyHint()}
            placeholder={t("provisioning.editor.searchImages")}
          />
          {noImages ? (
            <p className="form-hint">
              <Link to="/provisioning/images">{t("provisioning.editor.registerImage")}</Link>
            </p>
          ) : null}

          <label className="field full">
            <span className="flex items-center gap-2">
              <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
              {t("provisioning.editor.enabled")}
            </span>
          </label>

          {/* The two credential groups as framed fieldsets on the kit's
              panel radius and hairline. Whether a credential is SET is a
              pill; its value is never here (invariant 3). */}
          <fieldset className="field full" style={FIELDSET_STYLE}>
            <legend className="note" style={LEGEND_STYLE}>
              {t("provisioning.editor.domainJoin")}
              {existing?.has_domain_join_credential && <Pill kind="ok">{t("provisioning.editor.credentialSet")}</Pill>}
            </legend>
            <div className="form-grid">
              <Field label={t("provisioning.editor.domain")}>
                <input className={inputClass} value={domain} onChange={(e) => setDomain(e.target.value)} />
              </Field>
              <Field label={t("provisioning.editor.username")}>
                <input
                  className={inputClass}
                  value={domainUsername}
                  onChange={(e) => setDomainUsername(e.target.value)}
                />
              </Field>
              <Field label={t("provisioning.editor.passwordKeep")} full>
                <input
                  type="password"
                  className={inputClass}
                  value={domainPassword}
                  onChange={(e) => setDomainPassword(e.target.value)}
                  autoComplete="new-password"
                />
              </Field>
            </div>
          </fieldset>

          <fieldset className="field full" style={FIELDSET_STYLE}>
            <legend className="note" style={LEGEND_STYLE}>
              {t("provisioning.editor.localAdmin")}
              {existing?.has_local_admin_credential && <Pill kind="ok">{t("provisioning.editor.credentialSet")}</Pill>}
            </legend>
            <div className="form-grid">
              <Field label={t("provisioning.editor.username")}>
                <input
                  className={inputClass}
                  value={localAdminUsername}
                  onChange={(e) => setLocalAdminUsername(e.target.value)}
                />
              </Field>
              <Field label={t("provisioning.editor.passwordKeep")}>
                <input
                  type="password"
                  className={inputClass}
                  value={localAdminPassword}
                  onChange={(e) => setLocalAdminPassword(e.target.value)}
                  autoComplete="new-password"
                />
              </Field>
            </div>
          </fieldset>
        </div>

        <div className="flex flex-wrap items-center justify-end gap-2">
          {/* `type="button"` is load-bearing now that these controls live in a
              real `<form>`: `ui.tsx`'s Button sets no type, and an untyped
              button inside a form submits it — Cancel would save. */}
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("common.actions.cancel")}
          </Button>
          {/* This is the save that carries a domain-join or local-admin
              password, so a failure that renders as nothing is the worst case
              on this screen: the operator closes the dialog believing the
              credential was sealed. `String(mutation.error)` produced
              "ApiError: " for the bodyless 503 a proxy returns, which is
              exactly that; `FormActions` routes every failure through
              `describeSubmitError` into a role="alert". That function never
              reads a 422's `input` echo — the field value the server rejected
              — so a password that fails a length rule is not printed back
              onto the screen (invariant 3). Save is no longer disabled on an
              empty Name or Image: the click names them instead. */}
          <FormActions
            label={t("provisioning.editor.save")}
            pendingLabel={t("provisioning.editor.saving")}
            pending={mutation.isPending}
            error={mutation.error}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
}
