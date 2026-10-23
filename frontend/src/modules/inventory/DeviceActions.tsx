/**
 * The device page's incident actions: what a technician does to one machine.
 *
 * Every action calls a route that already exists and is checked server-side;
 * hiding one the operator lacks the permission for is a courtesy, never a
 * control (invariant 6). Actions that need the agent are not offered on an
 * asset without one.
 */

import { useQuery } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { ConfirmDialog } from "../../components/confirm";
import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { useApi } from "../../lib/ApiContext";
import type { Device } from "../../lib/api";
import { Btn } from "../../shell/components";
import { useRunBackupMutation } from "../backup/api";
import { runQueuedMessage } from "../backup/runMessage";
import { RunScriptOnDeviceDialog } from "../deploy/RunScriptOnDeviceDialog";
import { DispatchJobDialog } from "../jobs/DispatchJobDialog";
import { useDeviceRestartMutation, usePatchApplyMutation, usePatchZone } from "../patch/api";
import { applyOutcomeMessage } from "../patch/patchCopy";
import { restartConfirmBody, restartWhen } from "../patch/restartCopy";
import type { DeviceStatus } from "../patch/types";
import { StartSessionDialog } from "../remote/StartSessionDialog";
import { NewTicketDialog } from "../service-desk/NewTicketDialog";

/**
 * What the last action did, for the page to say out loud.
 *
 * `text` and a link's `label` are either finished text or a function that
 * words it. This file passes functions, which the page calls at render, so an
 * outcome already on screen follows a language switch; holding the finished
 * sentence in state would leave it in the language it was made in.
 */
export interface ActionOutcome {
  readonly tone: "ok" | "error";
  readonly text: string | (() => string);
  /** Where to follow it up, when there is somewhere. */
  readonly link?: { readonly to: string; readonly label: string | (() => string) };
}

/** An outcome's words in the current language. */
export function outcomeText(value: string | (() => string)): string {
  return typeof value === "function" ? value() : value;
}

export function DeviceActions({
  device,
  deviceId,
  hostname,
  permissions,
  myUserId,
  onOutcome,
}: {
  device: Device | undefined;
  deviceId: string;
  hostname: string;
  permissions: readonly string[];
  myUserId?: string;
  onOutcome: (outcome: ActionOutcome) => void;
}) {
  const t = useT();
  const [remoteOpen, setRemoteOpen] = useState(false);
  const [scriptOpen, setScriptOpen] = useState(false);
  const [confirmUpdates, setConfirmUpdates] = useState(false);
  const [confirmRestart, setConfirmRestart] = useState(false);
  const [ticketOpen, setTicketOpen] = useState(false);
  const [dispatchTarget, setDispatchTarget] = useState<Device | null>(null);
  const apply = usePatchApplyMutation();
  const backup = useRunBackupMutation();
  const restart = useDeviceRestartMutation();
  const zone = usePatchZone();
  const api = useApi();

  const can = (code: string) => permissions.includes(code);
  const managed = device?.agent_status != null;
  // Same key as the Overview's, so this is the query it already made.
  const patchStatus = useQuery<DeviceStatus>({
    queryKey: ["patch-device", deviceId],
    queryFn: () => api.getDevicePatchStatus(deviceId),
    enabled: managed && can("patch.read") && can("patch.apply"),
  });
  const patchTab = `/devices/${deviceId}?tab=patch`;
  const failed = (error: unknown) => onOutcome({ tone: "error", text: () => describeSubmitError(error) });

  function installUpdates() {
    setConfirmUpdates(false);
    apply.mutate(
      { device_ids: [deviceId] },
      {
        onSuccess: (data) =>
          onOutcome({
            tone: "ok",
            text: () => applyOutcomeMessage(data.job_ids, 1),
            link: { to: patchTab, label: () => translate("inventory.actions.seeUpdates") },
          }),
        onError: failed,
      },
    );
  }

  // The restart is booked for the maintenance window, never sent now: the
  // server's sweep dispatches it inside the window with the user warning.
  function restartInWindow() {
    setConfirmRestart(false);
    restart.mutate(deviceId, {
      onSuccess: (data) =>
        onOutcome({
          tone: "ok",
          text: () =>
            data.already_requested
              ? translate("inventory.actions.restartAlready", { hostname })
              : translate("inventory.actions.restartBooked", { hostname, when: restartWhen(data.window, zone) }),
          link: { to: patchTab, label: () => translate("inventory.actions.seeUpdates") },
        }),
      onError: failed,
    });
  }

  function backUpNow() {
    backup.mutate(deviceId, {
      onSuccess: (data) =>
        onOutcome({
          tone: "ok",
          text: () => runQueuedMessage(data),
          link: { to: `/devices/${deviceId}?tab=backup`, label: () => translate("inventory.actions.seeBackups") },
        }),
      onError: failed,
    });
  }

  const actions: ReactNode[] = [];
  if (managed && can("remote.control")) {
    actions.push(
      <Btn key="remote" label={t("inventory.actions.remoteIn")} icon="user-screen" variant="primary" onClick={() => setRemoteOpen(true)} />,
    );
  }
  if (managed && can("deploy.script.run")) {
    actions.push(<Btn key="script" label={t("inventory.actions.runScript")} icon="terminal" onClick={() => setScriptOpen(true)} />);
  }
  if (managed && can("patch.apply")) {
    actions.push(
      <Btn
        key="updates"
        label={apply.isPending ? t("inventory.actions.installing") : t("inventory.actions.installUpdates")}
        icon="download"
        disabled={apply.isPending}
        onClick={() => setConfirmUpdates(true)}
      />,
    );
  }
  if (managed && can("patch.apply")) {
    actions.push(
      <Btn
        key="restart"
        label={restart.isPending ? t("inventory.actions.bookingRestart") : t("inventory.actions.restart")}
        icon="refresh"
        disabled={restart.isPending}
        onClick={() => setConfirmRestart(true)}
      />,
    );
  }
  if (managed && can("backup.run")) {
    actions.push(
      <Btn
        key="backup"
        label={backup.isPending ? t("inventory.actions.startingBackup") : t("inventory.actions.backUpNow")}
        icon="backup"
        disabled={backup.isPending}
        onClick={backUpNow}
      />,
    );
  }
  if (can("ticket.write")) {
    actions.push(<Btn key="ticket" label={t("inventory.actions.openTicket")} icon="ticket" onClick={() => setTicketOpen(true)} />);
  }
  if (managed && can("jobs.dispatch")) {
    // The real row, so the dialog can warn about an offline agent.
    actions.push(
      <Btn key="job" label={t("inventory.actions.runJob")} icon="play" onClick={() => setDispatchTarget(device ?? null)} />,
    );
  }

  return (
    <>
      {actions}
      <StartSessionDialog open={remoteOpen} onClose={() => setRemoteOpen(false)} initialDeviceId={deviceId} />
      <RunScriptOnDeviceDialog
        open={scriptOpen}
        onClose={() => setScriptOpen(false)}
        deviceId={deviceId}
        hostname={hostname}
        osFamily={device?.os_family ?? null}
        onDispatched={(jobIds) =>
          onOutcome({
            tone: "ok",
            text: () =>
              jobIds.length === 0
                ? translate("inventory.actions.scriptNone")
                : translate("inventory.actions.scriptSent"),
            link: { to: `/devices/${deviceId}?tab=jobs`, label: () => translate("inventory.actions.seeJobs") },
          })
        }
      />
      <ConfirmDialog
        open={confirmUpdates}
        title={t("inventory.actions.updatesTitle")}
        subject={hostname}
        body={t("inventory.actions.updatesBody")}
        confirmLabel={t("inventory.actions.installUpdates")}
        onCancel={() => setConfirmUpdates(false)}
        onConfirm={installUpdates}
      />
      <ConfirmDialog
        open={confirmRestart}
        title={t("inventory.actions.restartTitle")}
        subject={hostname}
        body={restartConfirmBody(patchStatus.data?.restart_window, zone)}
        confirmLabel={t("inventory.actions.restartLabel")}
        onCancel={() => setConfirmRestart(false)}
        onConfirm={restartInWindow}
      />
      <NewTicketDialog
        open={ticketOpen}
        onClose={() => setTicketOpen(false)}
        myUserId={myUserId}
        initialDeviceId={deviceId}
        onCreated={(ticket) =>
          onOutcome({
            tone: "ok",
            text: () => translate("inventory.actions.ticketOpened", { number: ticket.number, hostname }),
            link: { to: `/service-desk/tickets/${ticket.id}`, label: () => translate("inventory.actions.openIt") },
          })
        }
      />
      <DispatchJobDialog device={dispatchTarget} onClose={() => setDispatchTarget(null)} />
    </>
  );
}
