/**
 * Off-site copy settings (docs/modules/backup/spec-server-backup.md §6).
 *
 * Three decisions, in the order the server enforces them: a recovery key the
 * operator holds (without it the off-site copy is unreadable if this server is
 * lost), the storage the copy goes to (tested by the server, and refused
 * without Object Lock), and which repositories are copied there.
 *
 * Nothing secret is ever shown: the access key and secret go in once and are
 * sealed server-side, and the recovery private key is made and kept offline.
 * The server gates every control here (`backup.offsite.manage`,
 * `backup.recovery.manage`); hiding a button is courtesy, not security.
 *
 * ON THE FORM KIT (redesign plan, Task 9, 4.4; Receta FO). Adding a storage
 * is a dialog opened from `?new=target` (decision D8), the page head's one
 * primary. Editing one is no longer a thirteen-field dialog: `?edit=<id>`
 * opens that storage's settings as a section of the page, below the list,
 * with the console's one save bar ("Unsaved changes · Discard · Save") and
 * the question before leaving. One storage is edited at a time, so there is
 * one bar and one guard holding changes. The bodies sent are the ones the
 * dialog sent (`targetSettings`), and the recovery key's wizard is unchanged.
 */

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { Button, Dialog, Field, inputClass, Table, Td, Th } from "../../components/ui";
import { Form, FormActions, attemptProblems } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { useT } from "../../i18n/context";
import { translate, type MessageKey } from "../../i18n/core";
import { lazyLabels } from "../../i18n/lazy";
import { Trans } from "../../i18n/Trans";
import { formatDateTime } from "../../lib/datetime";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import {
  Btn,
  FormSection,
  ListEmpty,
  PageHead,
  Panel,
  QueryState,
  SaveBar,
  StatusPill,
  useDraft,
  useEditParam,
  useNewParam,
  useUnsavedGuard,
} from "../../shell/components";
import {
  useBackupRepositories,
  useCreateOffsiteTargetMutation,
  useNewRecoveryKeyChallengeMutation,
  useOffsiteTargets,
  useProveRecoveryKeyMutation,
  useRecoveryKeys,
  useRegisterRecoveryKeyMutation,
  useSetRepositoryOffsiteMutation,
  useTestOffsiteTargetMutation,
  useUpdateOffsiteTargetMutation,
} from "./api";
import { OFFSITE_TARGET_STATE_LABEL, offsiteTargetStateKind } from "./backupState";
import { BackupPageNav } from "./backupNav";
import type { OffsiteTarget, RecoveryKey, RetentionGfs } from "./types";

/** What each server-side test step checked, in the operator's words. */
export const TEST_STEP_LABEL: Readonly<Record<string, string>> = lazyLabels({
  connect: "backup.offsite.testStep.connect",
  object_lock: "backup.offsite.testStep.object_lock",
  write: "backup.offsite.testStep.write",
  read: "backup.offsite.testStep.read",
  lock: "backup.offsite.testStep.lock",
  delete_refused: "backup.offsite.testStep.delete_refused",
});

export function OffsitePage() {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("backup.read");
  const canManageStorage = permissions.includes("backup.offsite.manage");
  const canManageKey = permissions.includes("backup.recovery.manage");
  const zone = me.data?.timezone;
  // Add off-site storage opens from `?new=target`; the storage being edited
  // is `?edit=<id>`, so a reload or a link opens the same form. Closing
  // either undoes its opening (Back from the entry `start` added).
  const adding = useNewParam("target");
  const editing = useEditParam("target");
  const targets = useOffsiteTargets();
  const edited = canManageStorage ? (targets.data ?? []).find((target) => target.id === editing.id) : undefined;
  // ONE GUARD FOR THE PAGE (general ruling 3; formKit: one dirty guard per
  // page), fed by the storage edited in `?edit=` and the one being added in
  // `?new=target`. Each form's key is guarded only while that form holds a
  // change, so opening Add over an edit with changes is not "leaving" it.
  const [editorDirty, setEditorDirty] = useState(false);
  const [addDirty, setAddDirty] = useState(false);
  const { guard, release } = useUnsavedGuard(editorDirty || addDirty, {
    search: [...(editorDirty ? ["edit"] : []), ...(addDirty ? ["new"] : [])],
  });

  return (
    <div>
      <PageHead
        title={t("backup.offsite.title")}
        description={t("backup.offsite.description")}
        actions={
          canManageStorage ? (
            <Btn label={t("backup.offsite.storageAdd")} icon="plus" variant="primary" onClick={adding.start} />
          ) : undefined
        }
      />
      <BackupPageNav canRead={canRead} />
      <RecoveryKeyPanel canManage={canManageKey} zone={zone} />
      <StoragePanel
        canManage={canManageStorage}
        zone={zone}
        onEdit={(target) => editing.start(target.id)}
        add={
          canManageStorage ? <Btn label={t("backup.offsite.storageAdd")} icon="plus" small onClick={adding.start} /> : null
        }
      />
      {/* KEYED by the storage, so another storage's form starts from its own
          values rather than the last one's draft. */}
      {guard}
      {edited ? (
        <TargetEditor
          key={edited.id}
          target={edited}
          onClose={editing.close}
          onDirty={setEditorDirty}
        />
      ) : null}
      <RepositoriesPanel canManage={canManageStorage} />
      {/* KEYED so a refusal cannot outlive the dialog that produced it. Open
          only for someone who may manage storage, whatever the URL asks. */}
      <TargetDialog
        key={adding.open && canManageStorage ? "open" : "closed"}
        open={adding.open && canManageStorage}
        onClose={adding.close}
        onDirty={setAddDirty}
        onSaved={() => {
          // Saved: the close that follows is not a change being dropped.
          release();
          adding.close();
        }}
      />
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Recovery key
// --------------------------------------------------------------------------- //

function RecoveryKeyPanel({ canManage, zone }: { canManage: boolean; zone?: string | null }) {
  const t = useT();
  const keys = useRecoveryKeys();
  const [wizard, setWizard] = useState<{ open: boolean; pending?: RecoveryKey }>({ open: false });
  const live = (keys.data ?? []).filter((k) => k.retired_at == null);
  const proven = live.filter((k) => k.proven_at != null);
  const unconfirmed = live.filter((k) => k.proven_at == null);

  return (
    <Panel
      title={t("backup.offsite.keyTitle")}
      subtitle={t("backup.offsite.keySubtitle")}
      actions={
        canManage && proven.length === 0 && unconfirmed.length === 0 ? (
          // ONE PRIMARY PER PAGE (Receta FO; the head's Add off-site storage):
          // the key's first step is this panel's own action, drawn plain.
          <Btn label={t("backup.offsite.keySetUp")} icon="plus" onClick={() => setWizard({ open: true })} />
        ) : undefined
      }
    >
      <QueryState
        loading={keys.isLoading}
        error={keys.error}
        empty={live.length === 0}
        emptyLabel={t("backup.offsite.keyEmpty")}
        emptyIcon="key"
      >
        <ul className="stack">
          {live.map((key) => (
            <li key={key.id}>
              {key.proven_at != null ? (
                <>
                  <StatusPill value="ok" kind="ok" label={t("backup.offsite.confirmed")} />{" "}
                  {t("backup.offsite.confirmedOn", { date: formatDateTime(key.proven_at, zone) })}{" "}
                  <span className="mono">{t("backup.offsite.fingerprint", { fingerprint: key.fingerprint })}</span>
                </>
              ) : (
                <>
                  <StatusPill value="pending" kind="warn" label={t("backup.offsite.notConfirmedYet")} />{" "}
                  <span className="mono">{t("backup.offsite.fingerprint", { fingerprint: key.fingerprint })}</span>{" "}
                  {canManage ? (
                    <Btn
                      label={t("backup.offsite.confirmKey")}
                      small
                      onClick={() => setWizard({ open: true, pending: key })}
                    />
                  ) : null}
                </>
              )}
            </li>
          ))}
        </ul>
      </QueryState>
      <RecoveryKeyWizard
        key={wizard.open ? `open-${wizard.pending?.id ?? "new"}` : "closed"}
        open={wizard.open}
        pending={wizard.pending}
        onClose={() => setWizard({ open: false })}
      />
    </Panel>
  );
}

/**
 * The wizard's first step, as rules over a value snapshot: the public key the
 * person pasted and whether they ticked "stored" ("yes" / "no"). The checks
 * read nothing else, so the refusal can be worded again on a language switch.
 */
const keyRules = (): readonly FieldRule[] => [
  {
    name: "publicKey",
    label: translate("backup.offsite.publicKey"),
    validate: (v) =>
      v.trim().startsWith("itms-recovery-public:") ? null : translate("backup.offsite.publicKeyInvalid"),
  },
  {
    name: "stored",
    label: "",
    validate: (v) => (v === "no" ? translate("backup.offsite.storedFirst") : null),
  },
];

const answerRules = (): readonly FieldRule[] => [
  { name: "answer", label: translate("backup.offsite.answer") },
];

export function RecoveryKeyWizard({
  open,
  pending,
  onClose,
}: {
  open: boolean;
  pending?: RecoveryKey;
  onClose: () => void;
}) {
  const t = useT();
  const register = useRegisterRecoveryKeyMutation();
  const challenge = useNewRecoveryKeyChallengeMutation();
  const prove = useProveRecoveryKeyMutation();
  const [publicKey, setPublicKey] = useState("");
  const [stored, setStored] = useState(false);
  const [answer, setAnswer] = useState("");
  // Held as data (rules and values), never as finished sentences, so a
  // language switch re-words the refusal. The snapshot never holds a secret:
  // the public key is public, and the answer is the proof the person typed.
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [check, setCheck] = useState<{ keyId: string; text: string } | null>(null);
  const [done, setDone] = useState(false);

  function submitKey() {
    register.reset();
    const next: Attempt = { rules: keyRules, values: { publicKey, stored: stored ? "yes" : "no" } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    register.mutate(publicKey.trim(), {
      onSuccess: (out) => setCheck({ keyId: out.key.id, text: out.challenge }),
    });
  }

  function startCheck() {
    if (pending == null) return;
    challenge.mutate(pending.id, {
      onSuccess: (out) => setCheck({ keyId: out.key.id, text: out.challenge }),
    });
  }

  function submitAnswer() {
    if (check == null) return;
    prove.reset();
    const next: Attempt = { rules: answerRules, values: { answer } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    prove.mutate({ keyId: check.keyId, answer: answer.trim() }, { onSuccess: () => setDone(true) });
  }

  if (done) {
    return (
      <Dialog open={open} onClose={onClose} title={t("backup.offsite.doneTitle")}>
        <p>{t("backup.offsite.doneBody")}</p>
        <div className="flex justify-end">
          <Button type="button" onClick={onClose}>
            {t("backup.offsite.done")}
          </Button>
        </div>
      </Dialog>
    );
  }

  if (check != null) {
    return (
      <Dialog open={open} onClose={onClose} title={t("backup.offsite.proveTitle")}>
        <Form onSubmit={submitAnswer}>
          <p>{t("backup.offsite.proveBody")}</p>
          {/* i18n-exempt: a shell command the person runs verbatim; code, not language */}
          <pre className="mono wrap">{`python3 itms_recover.py prove --key recovery.key --challenge '${check.text}'`}</pre>
          <div className="form-grid">
            <Field label={t("backup.offsite.answer")}>
              <input
                className={inputClass}
                value={answer}
                onChange={(e) => setAnswer(e.target.value)}
                autoComplete="off"
              />
            </Field>
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2">
            <Button type="button" variant="ghost" onClick={onClose}>
              {t("common.actions.cancel")}
            </Button>
            <FormActions
              label={t("backup.offsite.confirm")}
              pendingLabel={t("backup.offsite.checking")}
              pending={prove.isPending}
              error={prove.error}
              problems={attemptProblems(attempt)}
            />
          </div>
        </Form>
      </Dialog>
    );
  }

  if (pending != null) {
    return (
      <Dialog open={open} onClose={onClose} title={t("backup.offsite.confirmTitle")}>
        <Form onSubmit={startCheck}>
          <p>{t("backup.offsite.confirmBody")}</p>
          <div className="flex flex-wrap items-center justify-end gap-2">
            <Button type="button" variant="ghost" onClick={onClose}>
              {t("common.actions.cancel")}
            </Button>
            <FormActions
              label={t("backup.offsite.newCheck")}
              pending={challenge.isPending}
              error={challenge.error}
            />
          </div>
        </Form>
      </Dialog>
    );
  }

  return (
    <Dialog open={open} onClose={onClose} title={t("backup.offsite.setupTitle")}>
      <Form onSubmit={submitKey}>
        <p>{t("backup.offsite.setupLead")}</p>
        {/* i18n-exempt: a shell command the person runs verbatim; code, not language */}
        <pre className="mono wrap">python3 itms_recover.py keygen --out recovery.key</pre>
        <p>
          <Trans k="backup.offsite.setupBody" components={{ mono: <span className="mono" /> }} />
        </p>
        <div className="form-grid">
          <Field label={t("backup.offsite.publicKey")} help={t("backup.offsite.publicKeyHelp")}>
            <textarea
              className={inputClass}
              rows={2}
              value={publicKey}
              onChange={(e) => setPublicKey(e.target.value)}
            />
          </Field>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={stored} onChange={(e) => setStored(e.target.checked)} />
            {t("backup.offsite.storedCheckbox")}
          </label>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("common.actions.cancel")}
          </Button>
          <FormActions
            label={t("backup.offsite.next")}
            pendingLabel={t("backup.offsite.saving")}
            pending={register.isPending}
            error={register.error}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
}

// --------------------------------------------------------------------------- //
// Storage
// --------------------------------------------------------------------------- //

function StoragePanel({
  canManage,
  zone,
  onEdit,
  add,
}: {
  canManage: boolean;
  zone?: string | null;
  onEdit: (target: OffsiteTarget) => void;
  /** The empty list's way forward: Add off-site storage, for someone who may. */
  add: ReactNode;
}) {
  const t = useT();
  const targets = useOffsiteTargets();
  const test = useTestOffsiteTargetMutation();
  const items = targets.data ?? [];

  return (
    <Panel bare title={t("backup.offsite.storageTitle")} subtitle={t("backup.offsite.storageSubtitle")}>
      <QueryState loading={targets.isLoading} error={targets.error} empty={false} emptyLabel="">
        {items.length === 0 ? (
          // The kit's empty state (fix round 1, M4): nothing exists yet, with
          // the way to add one. Nothing filters this list.
          <ListEmpty
            filtered={false}
            onClear={() => {}}
            nothingYet={t("backup.offsite.storageEmpty")}
            icon="database"
            action={add}
          />
        ) : (
        <Table label={t("backup.offsite.storageTitle")}>
          <thead>
            <tr>
              <Th>{t("backup.offsite.colName")}</Th>
              <Th>{t("backup.offsite.colStatus")}</Th>
              <Th>{t("backup.offsite.colLockedFor")}</Th>
              <Th>{t("backup.offsite.colLastTest")}</Th>
              <Th>{canManage ? t("backup.offsite.colActions") : ""}</Th>
            </tr>
          </thead>
          <tbody>
            {items.map((target) => (
              <tr key={target.id}>
                <Td>
                  <div className="cell-title">{target.name}</div>
                  <div className="cell-sub mono">
                    {target.endpoint
                      ? t("backup.offsite.bucketAt", { bucket: target.bucket, endpoint: target.endpoint })
                      : target.bucket}
                  </div>
                </Td>
                <Td>
                  <StatusPill
                    value={target.state}
                    kind={offsiteTargetStateKind(target.state)}
                    label={OFFSITE_TARGET_STATE_LABEL[target.state] ?? target.state}
                  />
                  {target.last_error ? (
                    <div className="cell-sub" role="note">
                      {target.last_error}
                    </div>
                  ) : null}
                </Td>
                <Td>{t("backup.offsite.days", { count: target.immutable_days })}</Td>
                <Td>
                  {target.tested_at ? formatDateTime(target.tested_at, zone) : t("backup.offsite.never")}
                  {target.test_steps.length > 0 ? (
                    <ul className="cell-sub">
                      {target.test_steps.map((s) => (
                        <li key={s.step}>
                          {s.ok ? "✓" : "✗"} {TEST_STEP_LABEL[s.step] ?? s.step}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </Td>
                <Td>
                  {canManage ? (
                    <span className="flex gap-2">
                      <Btn
                        label={target.state === "testing" ? t("backup.offsite.testing") : t("backup.offsite.test")}
                        small
                        disabled={target.state === "testing" || test.isPending}
                        onClick={() => test.mutate(target.id)}
                      />
                      <Btn
                        label={t("backup.offsite.edit")}
                        small
                        onClick={() => onEdit(target)}
                      />
                    </span>
                  ) : null}
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
        )}
      </QueryState>
    </Panel>
  );
}

/**
 * What an off-site storage may not be saved without, as rules over the form's
 * value snapshot. `creating` is fixed when the attempt is made: a new storage
 * needs its keys, an edited one keeps them unless both are given.
 */
const targetRules = (creating: boolean): readonly FieldRule[] => {
  const rules: FieldRule[] = [
    { name: "name", label: translate("backup.offsite.targetName") },
    { name: "bucket", label: translate("backup.offsite.targetBucket") },
    {
      name: "immutable_days",
      label: translate("backup.offsite.lockRule"),
      validate: (v) =>
        /^\d+$/.test(v) && Number(v) >= 1 && Number(v) <= 90 ? null : translate("backup.offsite.lockRange"),
    },
  ];
  if (creating) {
    rules.push({ name: "access_key_id", label: translate("backup.offsite.accessKey") });
    rules.push({ name: "secret_access_key", label: translate("backup.offsite.secretKey") });
  } else {
    // "mismatch" when only one of the two keys was given (see `submit`).
    rules.push({
      name: "keyPair",
      label: "",
      validate: (v) => (v === "mismatch" ? translate("backup.offsite.keysBoth") : null),
    });
  }
  return rules;
};

/** A storage's settings as the form holds them: everything but its two keys. */
type TargetSettingsValues = {
  name: string;
  endpoint: string;
  region: string;
  bucket: string;
  prefix: string;
  immutable_days: string;
  max_mbps: string;
  daily: string;
  weekly: string;
  monthly: string;
  yearly: string;
};

/**
 * The storage's two keys, held APART from its settings: they go in once and
 * the server never returns them, so they are never part of what the editor
 * compares with the server (`useDraft` keeps that as text) and never saved.
 */
type TargetKeys = { access_key_id: string; secret_access_key: string };

type TargetValues = TargetSettingsValues & TargetKeys;

const NO_KEYS: TargetKeys = { access_key_id: "", secret_access_key: "" };

const GFS_FIELDS: ReadonlyArray<{ key: keyof RetentionGfs & keyof TargetValues; label: MessageKey }> = [
  { key: "daily", label: "backup.offsite.gfsDaily" },
  { key: "weekly", label: "backup.offsite.gfsWeekly" },
  { key: "monthly", label: "backup.offsite.gfsMonthly" },
  { key: "yearly", label: "backup.offsite.gfsYearly" },
];

/** What the form shows for a storage: the server's values, or a new one's defaults. */
function targetValues(target?: OffsiteTarget): TargetSettingsValues {
  return {
    name: target?.name ?? "",
    endpoint: target?.endpoint ?? "",
    region: target?.region ?? "us-east-1",
    bucket: target?.bucket ?? "",
    prefix: target?.prefix ?? "itms",
    immutable_days: String(target?.immutable_days ?? 30),
    max_mbps: target?.max_mbps != null ? String(target.max_mbps) : "",
    daily: String(target?.retention_gfs.daily ?? 14),
    weekly: String(target?.retention_gfs.weekly ?? 8),
    monthly: String(target?.retention_gfs.monthly ?? 12),
    yearly: String(target?.retention_gfs.yearly ?? 0),
  };
}

/**
 * The attempt the rules judge. The rules only ask whether the secret key was
 * given, so the snapshot holds "set" or "", never a second copy of the secret
 * itself. Trimmed, as the required rule trims: a blank secret is not a given
 * one. Held as data (rules and the values they judged), never as finished
 * sentences, so a language switch re-words the refusal.
 */
function targetAttempt(values: TargetValues, creating: boolean): Attempt {
  const keyPair = !creating && (values.access_key_id === "") !== (values.secret_access_key === "");
  return {
    rules: () => targetRules(creating),
    values: {
      ...values,
      secret_access_key: values.secret_access_key.trim() ? "set" : "",
      keyPair: keyPair ? "mismatch" : "ok",
    },
  };
}

/** The settings half of the body, the same for a new storage and an edited one. */
function targetSettings(values: TargetValues, target?: OffsiteTarget) {
  const retention: RetentionGfs = {};
  for (const f of GFS_FIELDS) {
    const n = Number(values[f.key]);
    if (Number.isInteger(n) && n > 0) retention[f.key] = n;
  }
  return {
    name: values.name.trim(),
    endpoint: values.endpoint.trim() === "" ? null : values.endpoint.trim(),
    region: values.region.trim() || "us-east-1",
    bucket: values.bucket.trim(),
    prefix: values.prefix.trim(),
    addressing: target?.addressing ?? ("path" as const),
    lock_mode: target?.lock_mode ?? ("compliance" as const),
    immutable_days: Number(values.immutable_days),
    retention_gfs: retention,
    max_mbps: values.max_mbps.trim() === "" ? null : Number(values.max_mbps),
  };
}

/** The thirteen fields, for the Add dialog and for the editor on the page. */
function TargetFields({
  values,
  set,
  editing,
}: {
  values: TargetValues;
  set: (name: keyof TargetValues, value: string) => void;
  /** An existing storage: its keys may be left blank, to keep the current ones. */
  editing: boolean;
}) {
  const t = useT();
  const keyHelp = editing ? t("backup.offsite.keyHelp") : undefined;
  return (
    <div className="form-grid">
      <Field label={t("backup.offsite.targetName")}>
        <input className={inputClass} value={values.name} onChange={(e) => set("name", e.target.value)} />
      </Field>
      <Field label={t("backup.offsite.storageAddress")} help={t("backup.offsite.storageAddressHelp")}>
        <input
          className={inputClass}
          value={values.endpoint}
          // i18n-exempt: an example endpoint URL, an identifier written the same in every language
          placeholder="https://s3.eu-central-1.wasabisys.com"
          onChange={(e) => set("endpoint", e.target.value)}
        />
      </Field>
      <Field label={t("backup.offsite.region")}>
        <input className={inputClass} value={values.region} onChange={(e) => set("region", e.target.value)} />
      </Field>
      <Field label={t("backup.offsite.bucket")}>
        <input className={inputClass} value={values.bucket} onChange={(e) => set("bucket", e.target.value)} />
      </Field>
      <Field label={t("backup.offsite.folder")}>
        <input className={inputClass} value={values.prefix} onChange={(e) => set("prefix", e.target.value)} />
      </Field>
      <Field label={t("backup.offsite.accessKey")} help={keyHelp}>
        <input
          className={inputClass}
          value={values.access_key_id}
          autoComplete="off"
          onChange={(e) => set("access_key_id", e.target.value)}
        />
      </Field>
      <Field label={t("backup.offsite.secretKey")} help={keyHelp}>
        <input
          className={inputClass}
          type="password"
          value={values.secret_access_key}
          autoComplete="new-password"
          onChange={(e) => set("secret_access_key", e.target.value)}
        />
      </Field>
      <Field label={t("backup.offsite.lockDays")} help={t("backup.offsite.lockDaysHelp")}>
        <input
          className={inputClass}
          inputMode="numeric"
          value={values.immutable_days}
          onChange={(e) => set("immutable_days", e.target.value)}
        />
      </Field>
      {GFS_FIELDS.map((f) => (
        <Field key={f.key} label={t(f.label)}>
          <input
            className={inputClass}
            inputMode="numeric"
            value={values[f.key]}
            onChange={(e) => set(f.key, e.target.value)}
          />
        </Field>
      ))}
      <Field label={t("backup.offsite.speedLimit")} help={t("backup.offsite.speedLimitHelp")}>
        <input
          className={inputClass}
          inputMode="numeric"
          value={values.max_mbps}
          onChange={(e) => set("max_mbps", e.target.value)}
        />
      </Field>
    </div>
  );
}

/** Add off-site storage (`?new=target`). Editing one is `TargetEditor`, on the page. */
export function TargetDialog({
  open,
  onClose,
  onDirty,
  onSaved,
}: {
  open: boolean;
  onClose: () => void;
  /** Tells the page's one guard whether the form holds a change (general ruling 3). */
  onDirty?: (dirty: boolean) => void;
  /** After the 2xx: the page releases its guard, then closes. Without it, the dialog just closes. */
  onSaved?: () => void;
}) {
  const t = useT();
  const create = useCreateOffsiteTargetMutation();
  const [values, setValues] = useState<TargetValues>({ ...targetValues(), ...NO_KEYS });
  // The snapshot is checked and kept in memory only; the secret key is never
  // written to a message (`targetAttempt`).
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  const dirty = open && JSON.stringify(values) !== JSON.stringify({ ...targetValues(), ...NO_KEYS });
  useEffect(() => {
    onDirty?.(dirty);
    return () => onDirty?.(false);
  }, [dirty, onDirty]);

  function set(name: keyof TargetValues, value: string) {
    setValues((prev) => ({ ...prev, [name]: value }));
  }

  function submit() {
    create.reset();
    const next = targetAttempt(values, true);
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    create.mutate(
      {
        ...targetSettings(values),
        access_key_id: values.access_key_id,
        secret_access_key: values.secret_access_key,
      },
      { onSuccess: onSaved ?? onClose },
    );
  }

  return (
    <Dialog open={open} onClose={onClose} title={t("backup.offsite.storageAdd")}>
      <Form onSubmit={submit}>
        <p>{t("backup.offsite.targetIntro")}</p>
        <TargetFields values={values} set={set} editing={false} />
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("common.actions.cancel")}
          </Button>
          <FormActions
            label={t("backup.offsite.add")}
            pending={create.isPending}
            error={create.error}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
}

/**
 * ONE STORAGE'S SETTINGS, EDITED ON THE PAGE (`?edit=<id>`; Receta FO). The
 * draft is what the server has until somebody types (`useDraft`); the keys
 * stay apart from it (`TargetKeys`), and typing either is a change too. The
 * page's one save bar sends the same body the edit dialog sent; a refusal of
 * the rules is said in the bar (`problems`), before anything is sent.
 *
 * Closing the editor, or leaving the page, with a change asks first: the form
 * lives in `?edit=`, and it reports its changes to the page's one guard
 * (`onDirty`), which guards `edit` while they last. Opening Add off-site
 * storage over it (`?new=target`) is not leaving it.
 */
function TargetEditor({
  target,
  onClose,
  onDirty,
}: {
  target: OffsiteTarget;
  onClose: () => void;
  /** Tells the page's one guard whether this storage's form holds a change. */
  onDirty: (dirty: boolean) => void;
}) {
  const t = useT();
  const update = useUpdateOffsiteTargetMutation();
  const { draft, setDraft, dirty: settingsDirty, discard, markSent } = useDraft(targetValues(target));
  const [keys, setKeys] = useState<TargetKeys>(NO_KEYS);
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const keysTyped = keys.access_key_id !== "" || keys.secret_access_key !== "";
  const dirty = settingsDirty || keysTyped;
  const values: TargetValues = { ...(draft ?? targetValues(target)), ...keys };

  useEffect(() => {
    onDirty(dirty);
    return () => onDirty(false);
  }, [dirty, onDirty]);

  function set(name: keyof TargetValues, value: string) {
    if (name === "access_key_id" || name === "secret_access_key") {
      setKeys((prev) => ({ ...prev, [name]: value }));
      return;
    }
    if (draft) setDraft({ ...draft, [name]: value });
  }

  function save() {
    if (!draft) return;
    update.reset();
    const next = targetAttempt(values, false);
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    // What is sent becomes what is saved when the server answers (useDraft).
    markSent(draft);
    update.mutate(
      {
        id: target.id,
        body: {
          ...targetSettings(values, target),
          access_key_id: keys.access_key_id || null,
          secret_access_key: keys.secret_access_key || null,
        },
      },
      {
        onSuccess: () => setKeys(NO_KEYS),
        // The save failed: the change is still unsaved, and a refetch must not replace it.
        onError: () => markSent(null),
      },
    );
  }

  return (
    <>
      <FormSection title={t("backup.offsite.editStorageNamed", { name: target.name })} icon="edit">
        <TargetFields values={values} set={set} editing />
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Btn label={t("common.actions.close")} variant="ghost" onClick={onClose} />
        </div>
      </FormSection>
      <SaveBar
        dirty={dirty}
        saving={update.isPending}
        error={update.error}
        saved={update.isSuccess && !dirty}
        problems={dirty ? attemptProblems(attempt) : []}
        onDiscard={() => {
          discard();
          setKeys(NO_KEYS);
          setAttempt(null);
          update.reset();
        }}
        onSave={save}
      />
    </>
  );
}

// --------------------------------------------------------------------------- //
// Which repositories are copied
// --------------------------------------------------------------------------- //

function RepositoriesPanel({ canManage }: { canManage: boolean }) {
  const t = useT();
  const repositories = useBackupRepositories();
  const targets = useOffsiteTargets();
  const setTarget = useSetRepositoryOffsiteMutation();
  const [failed, setFailed] = useState<{ repositoryId: string; message: string } | null>(null);
  const items = repositories.data ?? [];
  const ready = (targets.data ?? []).filter((target) => target.state === "ok");
  const nameOf = new Map((targets.data ?? []).map((target) => [target.id, target.name]));

  return (
    <Panel bare title={t("backup.offsite.copyTitle")}>
      <QueryState
        loading={repositories.isLoading}
        error={repositories.error}
        empty={items.length === 0}
        emptyLabel={t("backup.offsite.copyEmpty")}
        emptyIcon="database"
      >
        <Table>
          <thead>
            <tr>
              <Th>{t("backup.offsite.colRepository")}</Th>
              <Th>{t("backup.offsite.colOffsite")}</Th>
            </tr>
          </thead>
          <tbody>
            {items.map((repo) => (
              <tr key={repo.id}>
                <Td>{repo.name}</Td>
                <Td>
                  {canManage ? (
                    <select
                      className="select-field"
                      aria-label={t("backup.offsite.copyAria", { name: repo.name })}
                      value={repo.offsite_target_id ?? ""}
                      disabled={setTarget.isPending}
                      onChange={(e) => {
                        setFailed(null);
                        setTarget.mutate(
                          { repositoryId: repo.id, targetId: e.target.value || null },
                          {
                            onError: (err) =>
                              setFailed({
                                repositoryId: repo.id,
                                message: err instanceof Error ? err.message : String(err),
                              }),
                          },
                        );
                      }}
                    >
                      <option value="">{t("backup.offsite.off")}</option>
                      {ready.map((target) => (
                        <option key={target.id} value={target.id}>
                          {target.name}
                        </option>
                      ))}
                      {repo.offsite_target_id && !ready.some((target) => target.id === repo.offsite_target_id) ? (
                        <option value={repo.offsite_target_id}>
                          {nameOf.get(repo.offsite_target_id) ?? t("backup.offsite.unavailable")}
                        </option>
                      ) : null}
                    </select>
                  ) : (
                    (repo.offsite_target_id && nameOf.get(repo.offsite_target_id)) || t("backup.offsite.off")
                  )}
                  {failed?.repositoryId === repo.id ? (
                    <div role="alert" className="cell-sub">
                      {failed.message}
                    </div>
                  ) : null}
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </QueryState>
    </Panel>
  );
}
