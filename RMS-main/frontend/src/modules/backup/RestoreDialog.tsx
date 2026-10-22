/**
 * Restoring files out of a backup point.
 *
 * CYCLE 8 IS THE REASON THIS FILE CHANGED. A restore to the original location
 * overwrites what is on the endpoint now with what was there when the point
 * was taken — the current file is gone, and no undo exists anywhere in this
 * product for it. That is precisely the action the confirmation rule was
 * written for, and it named the two things a confirmation has to carry: WHAT
 * happens, and to WHICH thing. The subject here is the device and the moment
 * the point was taken, because "restored the wrong point" and "restored onto
 * the wrong machine" are the two mistakes this catches, and neither is
 * visible in a point's UUID.
 *
 * `requireTyping` IS DELIBERATELY OFF. `components/confirm.tsx` reserves it
 * for the unrecoverable AND wide — erasing a device, deleting an org. A
 * restore is scoped to the files ticked in this dialog; asking for a typed
 * device name on every one of them is friction that gets copied without
 * reading, which is worse than the dialog it would decorate.
 *
 * CYCLE 3 IS THE OTHER HALF. Restore was `disabled={… || !canSubmitRestore}`
 * — with nothing ticked, or Alternate selected and no path typed, the button
 * sat dead and named neither cause. That is the silent no-op the form kernel
 * exists to retire, and the two conditions are now problems the click
 * announces.
 *
 * THE CONFIRMATION STEP IS SCOPED TO THE DANGEROUS OPTION. Original overwrites
 * files that are on the device right now, with nothing else in this product
 * able to bring them back — that is what earns the extra step. Alternate
 * writes into a path the operator just typed themselves; the target is the
 * confirmation, and stacking a second one on top asks them to re-read what
 * they just wrote rather than catching a different mistake.
 */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Button, Dialog, Field, inputClass } from "../../components/ui";
import { ConfirmDialog } from "../../components/confirm";
import { Form, FormActions, attemptProblems, describeSubmitError } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { Btn } from "../../shell/components";
import { formatDateTime } from "../../lib/datetime";
import { formatBytes } from "../../lib/format";
import { useBackupManifest, useRestoreBackupMutation, useVerifyBackupMutation } from "./api";
import type { BackupPoint } from "./types";

/**
 * What stops a restore, as rules over a value snapshot: the number of ticked
 * rows ("0" when none) and whether an alternate location lacks its path
 * ("missing"). A rule reads nothing but the snapshot and the catalogue, so the
 * refusal can be worded again in another language.
 */
const restoreRules = (): readonly FieldRule[] => [
  {
    name: "selection",
    label: "",
    validate: (v) => (v === "0" ? translate("backup.restore.selectOne") : null),
  },
  {
    name: "targetPath",
    label: "",
    validate: (v) => (v === "missing" ? translate("backup.restore.pathRequired") : null),
  },
];

export function RestoreDialog({
  point,
  deviceName,
  onClose,
}: {
  point: BackupPoint | null;
  /** The device as the operator knows it — never its UUID. */
  deviceName: string;
  onClose: () => void;
}) {
  const t = useT();
  const api = useApi();
  const pointId = point?.id ?? null;
  const manifest = useBackupManifest(pointId ?? undefined);
  const restore = useRestoreBackupMutation();
  const verify = useVerifyBackupMutation();
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  // Restore target: mirrors docs/modules/backup/spec.md §restore_jobs.target
  // (`original|alternate + path`). "location" is the target discriminator —
  // distinct from the restore job's `mode` (restore|verify). Original is the
  // safe, common-case default, so no path is required unless the operator
  // opts into an alternate location.
  const [location, setLocation] = useState<"original" | "alternate">("original");
  const [targetPath, setTargetPath] = useState("");
  // Populated ON SUBMIT, never while ticking boxes. Held as data (the rules and
  // the snapshot they were checked against) so a language switch re-words it.
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [confirming, setConfirming] = useState(false);
  const trimmedTargetPath = targetPath.trim();

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const canRestore = (me.data?.permissions ?? []).includes("backup.restore");
  const zone = me.data?.timezone;

  function togglePath(path: string) {
    setSelected((prev) => ({ ...prev, [path]: !prev[path] }));
  }

  function selectedPaths(): string[] {
    return Object.entries(selected)
      .filter(([, checked]) => checked)
      .map(([path]) => path);
  }

  function reset() {
    setSelected({});
    setLocation("original");
    setTargetPath("");
    setAttempt(null);
  }

  /**
   * What stops this restore, in the user's vocabulary.
   *
   * Neither condition is a missing text field: a selection is a set of ticked
   * rows and the path is required only in one of the two target modes. So the
   * snapshot carries the facts the rules judge ("0" ticked rows, a path that
   * is "missing"), and the rules answer through the same `problems` channel
   * and the same single `role="alert"`, because a second mechanism for "you
   * cannot submit yet" is how a product ends up with four error styles.
   */
  function restoreAttempt(): Attempt {
    return {
      rules: restoreRules,
      values: {
        selection: String(selectedPaths().length),
        targetPath: location === "alternate" && !trimmedTargetPath ? "missing" : "ok",
      },
    };
  }

  function requestRestore() {
    if (!pointId) return;
    // Clear the OTHER action's outcome first. A failed Verify left its
    // sentence on screen while a Restore was in flight — the operator reads a
    // refusal that belongs to a request they already abandoned.
    verify.reset();
    restore.reset();
    const next = restoreAttempt();
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    // Original overwrites files that are on the device right now, and
    // nothing else in this product can bring them back — that is what the
    // extra step is for. Alternate writes into a path the operator just
    // typed, so the submit itself is the confirmation.
    if (location === "original") {
      setConfirming(true);
    } else {
      doRestore();
    }
  }

  function doRestore() {
    if (!pointId) return;
    const target: Record<string, unknown> =
      location === "original"
        ? { location: "original" }
        : { location: "alternate", path: trimmedTargetPath };
    restore.mutate(
      {
        pointId,
        body: { selection: selectedPaths(), target },
      },
      {
        onSuccess: () => {
          reset();
          onClose();
        },
      },
    );
  }

  function handleVerify() {
    if (!pointId) return;
    restore.reset();
    setAttempt(null);
    verify.mutate(pointId, {
      onSuccess: () => {
        reset();
        onClose();
      },
    });
  }

  const chosenCount = selectedPaths().length;
  const takenAt = formatDateTime(point?.taken_at, zone);

  // CONSEQUENCES, NOT REASSURANCE (confirm.tsx's rule). This dialog only
  // opens for the original-location restore -- see `requestRestore` -- so it
  // states that consequence and no other.
  const confirmBody = [
    t("backup.restore.confirmLead", { count: chosenCount, takenAt }),
    t("backup.restore.confirmOverwrite"),
    t("backup.restore.confirmSigned"),
  ].join(" ");

  return (
    <Dialog open={point != null} onClose={onClose} title={t("backup.restore.title")}>
      <p className="form-hint">{t("backup.restore.taken", { device: deviceName, takenAt })}</p>

      {/* The manifest as v3's mini list: a caption per stream, a row per
          entry with its path in mono and its size on the right. The frame is
          the kit's control radius and hairline; the sizes are `formatBytes`,
          because `11534336` beside `4096` cannot be compared by eye. */}
      <div className="mini-list" style={{ maxHeight: 256, overflowY: "auto", border: "1px solid var(--line)", borderRadius: "var(--r-ctl)", marginBottom: 12 }}>
        {manifest.data?.streams.map((stream) => (
          <div key={stream.type}>
            <p className="note" style={{ padding: "6px 12px", background: "var(--surface-2)", borderBottom: "1px solid var(--line)", fontWeight: 500 }}>
              {stream.type}
            </p>
            {stream.entries.map((entry) => (
              <label key={entry.path} className="mini-row" style={{ padding: "6px 12px", minHeight: 36 }}>
                <input
                  type="checkbox"
                  checked={selected[entry.path] ?? false}
                  onChange={() => togglePath(entry.path)}
                />
                <span className="mini-copy mono">{entry.path}</span>
                <span className="mini-sub mono">{formatBytes(entry.size) || entry.size}</span>
              </label>
            ))}
          </div>
        ))}
        {manifest.data && manifest.data.streams.length === 0 && (
          <p className="note" style={{ padding: 12 }}>{t("backup.restore.noEntries")}</p>
        )}
      </div>

      {/* A real <form>, so Enter in the alternate-path field submits rather
          than doing nothing: `ui.tsx`'s Button sets no type, so this dialog
          had no submit control at all. */}
      <Form onSubmit={requestRestore}>
        <div className="form-grid">
          <fieldset className="field full">
            <legend className="note" style={{ marginBottom: 6, fontWeight: 500 }}>{t("backup.restore.target")}</legend>
            <div className="flex flex-col gap-1.5">
              <label className="flex items-center gap-2">
                <input
                  type="radio"
                  name="restore-target-location"
                  value="original"
                  checked={location === "original"}
                  onChange={() => setLocation("original")}
                />
                {/* SAID PLAINLY ON THE OPTION, not only in the confirmation
                    two clicks later: this choice overwrites what is on the
                    device right now, and nothing else in this product can
                    bring the current files back. */}
                <span>
                  {t("backup.restore.original")}{" "}
                  <span className="note">{t("backup.restore.originalWarning")}</span>
                </span>
              </label>
              <label className="flex items-center gap-2">
                <input
                  type="radio"
                  name="restore-target-location"
                  value="alternate"
                  checked={location === "alternate"}
                  onChange={() => setLocation("alternate")}
                />
                {t("backup.restore.alternate")}
              </label>
            </div>
          </fieldset>

          {location === "alternate" && (
            <Field label={t("backup.restore.alternatePath")} full>
              <input
                className={inputClass}
                value={targetPath}
                onChange={(e) => setTargetPath(e.target.value)}
              />
            </Field>
          )}
        </div>

        {/* Verify's own failure, in its own announced element. It is not a
            `FormActions` because Verify is not this form's submit — and a
            single shared element was how a stale refusal from one action came
            to describe the other. */}
        {verify.error ? (
          <p role="alert" className="form-error">
            {describeSubmitError(verify.error)}
          </p>
        ) : null}

        <div className="flex flex-wrap items-center justify-end gap-2">
          {/* `type="button"` on both non-submit controls: `ui.tsx`'s Button
              sets no type, and an untyped button inside a form submits it —
              Cancel and Verify would each start a restore. */}
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("common.actions.cancel")}
          </Button>
          {canRestore && (
            <Btn
              label={verify.isPending ? t("backup.restore.verifying") : t("backup.restore.verify")}
              icon="check"
              disabled={verify.isPending || pointId == null}
              onClick={handleVerify}
            />
          )}
          {canRestore && (
            <FormActions
              label={t("backup.points.restore")}
              pendingLabel={t("backup.restore.restoring")}
              pending={restore.isPending}
              error={restore.error}
              problems={attemptProblems(attempt)}
            />
          )}
        </div>
      </Form>

      <ConfirmDialog
        open={confirming}
        title={t("backup.restore.confirmTitle")}
        subject={deviceName}
        body={confirmBody}
        confirmLabel={t("backup.restore.confirmLabel")}
        destructive
        onCancel={() => setConfirming(false)}
        onConfirm={() => {
          setConfirming(false);
          doRestore();
        }}
      />
    </Dialog>
  );
}
