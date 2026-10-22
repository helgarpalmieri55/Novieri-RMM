import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ConfirmDialog } from "../../components/confirm";
import { Form, FormActions, attemptProblems } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { EntityPicker } from "../../components/picker";
import { Button, Dialog, Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate, type MessageKey } from "../../i18n/core";
import type { Device, RemoteMode } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { ADD_COMPUTER_CTA } from "../../lib/detectors";
import { agentTooOld, devicePickerEmptyHint, devicePickerOptions, unreachableReason } from "./pickerOptions";

/** Where an agent too old to share its screen is updated: Agent packages' fleet versions. */
// The label is an accessor over the catalogue, so it follows a language switch.
export const UPDATE_AGENT_CTA = {
  to: "/agent-packages#fleet-versions",
  get label(): string {
    return translate("remote.start.updateAgent");
  },
};

// SCREEN SHARE IS THE ONLY SESSION TYPE (spec-screen-share.md, operator
// decision 2026-09-23: guacd is retired). It shares the logged-on user's own
// console, so there is no protocol to choose and no credential to pick: the
// server refuses a credential on a screen session outright.

/**
 * A REASON IS THE AUDIT RECORD, so "x" is not one.
 *
 * Auditor A, [HIGH]: "The justification for remotely accessing an employee's
 * machine is the single most important field in the audit record — it is what
 * an internal investigation, a works council or a data-protection review will
 * ask for." The fix line says "Make Reason mandatory and validate it is
 * non-trivial", and both halves are here: `requiredProblems` rejects blank and
 * whitespace, and this rejects the single character typed to get past it.
 *
 * SIX, not twenty. A minimum long enough to exclude "x" and "asdf" and short
 * enough to admit a real ticket reference ("INC-42"). A longer bar does not
 * buy honesty — it buys padded reasons — and this cannot verify the text
 * either way; see the module's leftOpen note about linking a real ticket.
 */
const MIN_REASON = 6;

// A function, so the labels and the sentence are worded when the problems are
// shown, in the language then active (see `Attempt`).
const startRules = (): readonly FieldRule[] => [
  { name: "deviceId", label: translate("remote.start.fieldDevice") },
  {
    name: "reason",
    label: translate("remote.start.fieldReason"),
    validate: (value) => (value.length < MIN_REASON ? translate("remote.start.reasonShort") : null),
  },
];

export function StartSessionDialog({
  open,
  onClose,
  initialDeviceId,
  initialReason,
}: {
  open: boolean;
  onClose: () => void;
  /** Opened from a device or a ticket: the device is already chosen. */
  initialDeviceId?: string;
  /** Opened from a ticket: its reference, which the technician may edit. */
  initialReason?: string;
}) {
  const t = useT();
  const api = useApi();
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const [deviceId, setDeviceId] = useState("");
  const [mode, setMode] = useState<RemoteMode>("attended");
  const [reason, setReason] = useState("");
  // Filled ON SUBMIT only, never while typing. Held as the rules and the values
  // they were checked against, and worded at render, so a language switch
  // re-words what is on screen.
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  // Non-null once the required checks pass AND something about this session
  // needs saying out loud first. Holds nothing but the flag: every fact the
  // confirmation renders is derived below, so it cannot go stale against a
  // field the operator changed after opening it.
  const [confirming, setConfirming] = useState(false);

  useEffect(() => {
    if (!open) return;
    if (initialDeviceId) setDeviceId(initialDeviceId);
    if (initialReason) setReason(initialReason);
  }, [open, initialDeviceId, initialReason]);

  const devices = useQuery({
    queryKey: ["devices"],
    queryFn: () => api.listDevices(),
    enabled: open,
  });

  const deviceOptions = devicePickerOptions(devices.data?.items ?? []);

  const chosenDevice: Device | undefined = devices.data?.items.find(
    (device) => device.id === deviceId,
  );
  const deviceName = chosenDevice?.hostname ?? chosenDevice?.fqdn ?? deviceId;
  const unreachable = unreachableReason(chosenDevice);
  const tooOld = agentTooOld(chosenDevice);
  const unattended = mode === "unattended";

  const start = useMutation({
    mutationFn: () =>
      api.startRemoteSession({
        device_id: deviceId,
        protocol: "screen",
        mode,
        // Trimmed on the way out, so the reason written to the audit log is
        // the one the required-field check above passed.
        reason: reason.trim(),
      }),
    onSuccess: (data) => {
      void queryClient.invalidateQueries({ queryKey: ["remote-sessions"] });
      reset();
      onClose();
      navigate(`/remote/${data.session_id}`);
    },
  });

  function reset() {
    setDeviceId("");
    setMode("attended");
    setReason("");
    setAttempt(null);
    setConfirming(false);
  }

  function submit() {
    const next: Attempt = { rules: startRules, values: { deviceId, reason } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    // AUDIT [HIGH], /remote: "'Unattended' is a peer option selected the same
    // way, with no warning, no confirmation step and no additional
    // authorisation", and "a remote session can be started against a device
    // that is offline". Neither is a validation error -- the operator may
    // genuinely mean both -- so neither blocks the button. They stop here for
    // one deliberate step that names what is about to happen.
    if (unattended || unreachable) {
      setConfirming(true);
      return;
    }
    start.mutate();
  }

  // The dialog is never unmounted -- SessionsPage keeps it rendered and
  // toggles `open` -- so a refusal from the last attempt would otherwise still
  // be announced the next time the technician opens it.
  function dismiss() {
    reset();
    start.reset();
    onClose();
  }

  const confirmTitleKey: MessageKey = unattended
    ? "remote.start.confirmTitleUnattended"
    : tooOld && chosenDevice?.agent_status === "online"
      ? "remote.start.confirmTitleTooOld"
      : "remote.start.confirmTitleOffline";

  const confirmBody = [
    unattended ? t("remote.start.confirmUnattended") : null,
    unreachable ? t("remote.start.unreachableLine", { device: deviceName, reason: unreachable }) : null,
    t("remote.start.confirmReason"),
  ]
    .filter((line): line is string => line != null)
    .join(" ");

  return (
    <>
      <Dialog open={open} onClose={dismiss} title={t("remote.start.dialogTitle")}>
        <Form onSubmit={submit}>
          <div className="form-grid">
            {/* AUDIT [HIGH]: the device `<select>` listed the fleet's only
                device -- offline since 8/18 -- as a plain selectable option
                beside a stat card reading "Online devices 0". The picker's
                sublabel carries the agent state, which is what stops a
                technician opening a live session against a machine that cannot
                answer it. */}
            <EntityPicker
              label={t("remote.start.fieldDevice")}
              options={deviceOptions}
              loading={devices.isLoading}
              emptyHint={devicePickerEmptyHint()}
              placeholder={t("remote.start.devicePlaceholder")}
              value={deviceId}
              onChange={setDeviceId}
            />
            {devices.isSuccess && devices.data.items.length === 0 ? (
              <p className="form-hint">
                <Link to={ADD_COMPUTER_CTA.to}>{ADD_COMPUTER_CTA.label}</Link>
              </p>
            ) : null}
            {unreachable ? (
              <p className="form-hint" role="status">
                {t("remote.start.unreachableLine", { device: deviceName, reason: unreachable })}
                {/* THE REMEDY, NOT ONLY THE REASON (cross-module-guidance §2
                    Remote #2): an old agent is fixed on Agent packages. */}
                {tooOld && chosenDevice?.agent_status === "online" ? (
                  <>
                    {" "}
                    <Link to={UPDATE_AGENT_CTA.to}>{UPDATE_AGENT_CTA.label}</Link>
                  </>
                ) : null}
              </p>
            ) : null}
            <Field label={t("remote.start.mode")} full>
              <select
                className={inputClass}
                value={mode}
                onChange={(event) => setMode(event.target.value as RemoteMode)}
              >
                <option value="attended">{t("remote.start.modeAttended")}</option>
                <option value="unattended">{t("remote.start.modeUnattended")}</option>
              </select>
            </Field>
            {unattended ? (
              // Said HERE as well as in the confirmation, because a warning that
              // only appears after the operator has committed is a warning that
              // arrives too late to change the choice cheaply.
              <p className="form-hint" role="status">
                {t("remote.start.unattendedNote")}
              </p>
            ) : null}
            <Field label={t("remote.start.fieldReason")} full>
              <input
                className={inputClass}
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                placeholder={t("remote.start.reasonPlaceholder")}
              />
            </Field>
            {/* MOVED HERE FROM THE PAGE DESCRIPTION, 2026-09-09. The /remote
                subtitle used to carry "every session is consent-gated and
                attended by default" among three other clauses of platform
                architecture. Consent and attendance are now read off the Mode
                select two fields up, which shows them; recording and the audit
                write are not shown anywhere, and this is the moment they
                matter — before the session starts, next to the reason that
                gets written. */}
            <p className="form-hint">
              {t("remote.start.recordedNote")}
            </p>
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2">
            {/* `type="button"` is load-bearing now these controls sit in a real
                `<form>`: `ui.tsx`'s Button sets no type, and an untyped button
                inside a form submits it -- Cancel would start the session. */}
            <Button type="button" variant="ghost" onClick={dismiss}>
              {t("common.actions.cancel")}
            </Button>
            {/* Enabled with no device and no reason ON PURPOSE. Audit [MEDIUM],
                /remote → Start remote session: "Start session with no device
                selected is a silent no-op" -- the button was disabled and named
                neither field, so the click looked like a broken console. It now
                answers with which of the two is missing. `String(start.error)`
                goes with it: an ApiError carrying an empty body (the bodyless
                503) stringified to nothing at all. */}
            <FormActions
              label={t("remote.start.submit")}
              pending={start.isPending}
              error={start.error}
              problems={attemptProblems(attempt)}
            />
          </div>
        </Form>
      </Dialog>

      {/* OUTSIDE the Dialog, not nested inside it: two stacked `.modal`
          elements in one subtree fight over the overlay, and a confirmation
          rendered underneath the thing it is confirming is not one. */}
      <ConfirmDialog
        open={open && confirming}
        title={t(confirmTitleKey)}
        subject={deviceName}
        body={confirmBody}
        confirmLabel={unattended ? t("remote.start.confirmUnattendedLabel") : t("remote.start.confirmAnywayLabel")}
        // Destructive styling for the consent escalation and nothing else: an
        // offline device wastes a click, unattended access reaches somebody's
        // machine without asking them. No `requireTyping` -- it is reserved
        // for the unrecoverable and wide, and typing a hostname here would
        // teach exactly the copy-without-reading habit it is meant to prevent.
        destructive={unattended}
        onCancel={() => setConfirming(false)}
        onConfirm={() => {
          setConfirming(false);
          start.mutate();
        }}
      />
    </>
  );
}
