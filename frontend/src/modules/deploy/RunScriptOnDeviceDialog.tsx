/**
 * Run a script from the library on one device -- the device page's "Run a
 * script". The target is fixed, so the only choice is which script; the
 * server signs the job and checks `deploy.script.run` (`POST
 * /deploy/scripts/{id}/run`). Same SYSTEM confirmation as the Scripts page:
 * a dispatched job cannot be taken back.
 */

import { useState } from "react";
import { ConfirmDialog } from "../../components/confirm";
import { Form, FormActions, attemptProblems } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { EntityPicker, type PickerOption } from "../../components/picker";
import { Button, Dialog } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { useRunScriptMutation, useScripts } from "./api";
import { interpreterLabel, osFamilyLabel } from "./pickerOptions";
import type { ScriptListItem } from "./types";
import { intlLocale } from "../../lib/datetime";

// A function, so the label is worded when the problems are shown (see `Attempt`).
const rules = (): readonly FieldRule[] => [{ name: "scriptId", label: translate("deploy.runOnDevice.rule") }];

/** Scripts for this device's platform first; the rest still offered, labelled. */
export function scriptOptions(
  scripts: readonly ScriptListItem[],
  osFamily: string | null,
): PickerOption[] {
  const family = osFamily?.toLowerCase() ?? null;
  const fits = (script: ScriptListItem) => family == null || script.os_family.toLowerCase() === family;
  return [...scripts]
    .sort((a, b) => Number(fits(b)) - Number(fits(a)) || a.name.localeCompare(b.name, intlLocale()))
    .map((script) => ({
      id: script.id,
      label: script.name,
      sublabel: [
        interpreterLabel(script.interpreter),
        fits(script)
          ? osFamilyLabel(script.os_family)
          : translate("deploy.picker.scriptElsewhere", { os: osFamilyLabel(script.os_family) }),
      ].join(" · "),
    }));
}

export function RunScriptOnDeviceDialog({
  open,
  onClose,
  deviceId,
  hostname,
  osFamily,
  onDispatched,
}: {
  open: boolean;
  onClose: () => void;
  deviceId: string;
  hostname: string;
  osFamily: string | null;
  onDispatched: (jobIds: readonly string[]) => void;
}) {
  const t = useT();
  const scripts = useScripts();
  const run = useRunScriptMutation();
  const [scriptId, setScriptId] = useState("");
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [confirming, setConfirming] = useState(false);
  // Not memoised: the options carry worded sublabels, which must follow a language switch.
  const options = scriptOptions(scripts.data ?? [], osFamily);
  const scriptName = options.find((option) => option.id === scriptId)?.label ?? "";

  function dismiss() {
    setAttempt(null);
    setConfirming(false);
    setScriptId("");
    run.reset();
    onClose();
  }

  function submit() {
    const next: Attempt = { rules, values: { scriptId } };
    setAttempt(next);
    if (attemptProblems(next).length === 0) setConfirming(true);
  }

  function dispatch() {
    setConfirming(false);
    run.mutate(
      { id: scriptId, body: { target_type: "device", target_id: deviceId } },
      {
        onSuccess: (data) => {
          onDispatched(data.job_ids);
          dismiss();
        },
      },
    );
  }

  return (
    <>
      <Dialog open={open} onClose={dismiss} dismissible={!confirming} title={t("deploy.runOnDevice.title", { hostname })}>
        <Form onSubmit={submit}>
          <EntityPicker
            label={t("deploy.runOnDevice.rule")}
            options={options}
            loading={scripts.isLoading}
            emptyHint={t("deploy.runOnDevice.emptyHint")}
            placeholder={t("deploy.runOnDevice.placeholder")}
            value={scriptId}
            onChange={setScriptId}
          />
          <div className="flex flex-wrap items-center justify-end gap-2">
            <Button type="button" variant="ghost" onClick={dismiss}>
              {t("common.actions.cancel")}
            </Button>
            <FormActions
              label={t("deploy.runOnDevice.submit")}
              pendingLabel={t("deploy.runOnDevice.pending")}
              pending={run.isPending}
              error={run.error ?? scripts.error}
              problems={attemptProblems(attempt)}
            />
          </div>
        </Form>
      </Dialog>
      <ConfirmDialog
        open={confirming}
        title={t("deploy.runOnDevice.confirmTitle")}
        subject={t("deploy.runOnDevice.confirmSubject", { script: scriptName, hostname })}
        body={t("deploy.runOnDevice.confirmBody")}
        confirmLabel={t("deploy.runOnDevice.confirmLabel")}
        destructive
        onCancel={() => setConfirming(false)}
        onConfirm={dispatch}
      />
    </>
  );
}
