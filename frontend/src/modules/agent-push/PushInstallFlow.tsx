// "Push install the agent…": the flow a person starts on Devices after ticking
// machines one by one (spec-push-install §10.4; invariant 2a(f)).
//
//   1. THE MACHINES, each by name and address, with what a push needs of it --
//      an operating system and processor (push picks the package from them,
//      §7.2), and for Windows a DNS name (Kerberos issues a ticket for a name,
//      never an address; design 2026-09-28 §2.3). A machine missing one is
//      named with a link to set it, and the flow stops there: the server would
//      refuse the push, and saying so before a credential is chosen is kinder
//      than a 400 after.
//   2. THE CREDENTIAL, of the kind those machines take: a Windows domain
//      account for Windows, a push install account for Linux and macOS. One
//      push presents one credential, so a selection that mixes the two is
//      split rather than half-refused.
//   3. THE PRE-FLIGHT (`PushPreflightPanel`): probe, (SSH only) confirm each
//      host key, then push.
//
// NOTHING HERE IS AUTOMATED OR GATES ANYTHING. Every step is a click by a
// person; the server checks `agent.push.run`, `credentials.checkout`, the four
// bounds of 2a(d) and the batch ceiling on every request (invariant 6).

import { useState } from "react";
import { Link } from "react-router-dom";

import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { Button, Field, Td, Th } from "../../components/ui";
import type { Device } from "../../lib/api";
import { Panel, TableWrap } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { credentialTypeLabel } from "../credentials/credentialTypes";
import { osFamilyLabel } from "../devices/osLabel";
import { ARCH_OPTIONS } from "../devices/PushDetailsFields";
import { usePushCredentials } from "./api";
import { PushPreflightPanel } from "./PushPreflightPanel";
import { PUSH_BATCH_MAX, PUSH_CREDENTIAL_TYPE, WINDOWS_DOMAIN_CREDENTIAL_TYPE } from "./types";


/** What stops a push to this machine before any request is sent. Empty: nothing. */
export function pushProblems(device: Device): string[] {
  const problems: string[] = [];
  if (!device.primary_ip) problems.push(translate("agentPush.flow.problemNoIp"));
  if (!device.os_family) problems.push(translate("agentPush.flow.problemNoOs"));
  if (device.architecture !== "amd64" && device.architecture !== "arm64") {
    problems.push(translate("agentPush.flow.problemNoProcessor"));
  }
  if (device.os_family === "windows" && !device.fqdn) {
    problems.push(translate("agentPush.flow.problemNoDns"));
  }
  return problems;
}

function machineName(device: Device): string {
  return device.hostname || device.primary_ip || translate("agentPush.flow.unnamed");
}

export function PushInstallFlow({
  machines,
  onRemove,
  onClose,
}: {
  /** The machines a person ticked on Devices, in the order they were ticked. */
  machines: readonly Device[];
  onRemove: (id: string) => void;
  onClose: () => void;
}) {
  const t = useT();
  const credentials = usePushCredentials();
  const [credentialId, setCredentialId] = useState("");

  const windows = machines.filter((m) => m.os_family === "windows");
  const others = machines.filter((m) => m.os_family !== "windows");
  const mixed = windows.length > 0 && others.length > 0;
  const blocked = machines.some((m) => pushProblems(m).length > 0);
  const kind = windows.length > 0 ? WINDOWS_DOMAIN_CREDENTIAL_TYPE : PUSH_CREDENTIAL_TYPE;
  const offered = (credentials.data ?? []).filter((c) => c.type === kind);
  const chosen = offered.find((c) => c.id === credentialId) ?? null;
  const targets = machines.map((m) => m.primary_ip as string);

  return (
    <section aria-label={t("agentPush.flow.title")} className="space-y-4">
      <Panel
        title={t("agentPush.flow.title")}
        subtitle={t("agentPush.flow.subtitle", { ticked: machines.length, max: PUSH_BATCH_MAX })}
        actions={
          <Button variant="ghost" onClick={onClose}>
            {t("agentPush.shared.close")}
          </Button>
        }
      >
        <TableWrap>
          <table aria-label={t("agentPush.flow.tableLabel")}>
            <thead>
              <tr>
                <Th>{t("agentPush.flow.machine")}</Th>
                <Th>{t("agentPush.flow.address")}</Th>
                <Th>{t("agentPush.flow.os")}</Th>
                <Th>{t("agentPush.flow.processor")}</Th>
                <Th>{t("agentPush.flow.dns")}</Th>
                <Th>{t("agentPush.flow.ready")}</Th>
                <Th label={t("agentPush.flow.remove")} />
              </tr>
            </thead>
            <tbody>
              {machines.map((m) => {
                const problems = pushProblems(m);
                return (
                  <tr key={m.id}>
                    <Td>
                      <Link to={`/devices/${m.id}`}>{machineName(m)}</Link>
                    </Td>
                    <Td>
                      <code>{m.primary_ip ?? ""}</code>
                    </Td>
                    <Td>{m.os_family ? osFamilyLabel(m.os_family) : t("agentPush.flow.notRecorded")}</Td>
                    <Td>{ARCH_OPTIONS.find((a) => a.value === m.architecture)?.label ?? t("agentPush.flow.notRecorded")}</Td>
                    <Td>{m.fqdn ?? t("agentPush.flow.notRecorded")}</Td>
                    <Td>
                      {problems.length === 0 ? (
                        t("agentPush.flow.yes")
                      ) : (
                        <>
                          {problems.map((p) => (
                            <span key={p} className="block">
                              {p}
                            </span>
                          ))}
                          <Link to={`/devices/${m.id}`} aria-label={t("agentPush.flow.setDetailsFor", { name: machineName(m) })}>
                            {t("agentPush.flow.setDetails")}
                          </Link>
                        </>
                      )}
                    </Td>
                    <Td>
                      <Button
                        variant="ghost"
                        onClick={() => onRemove(m.id)}
                        aria-label={t("agentPush.flow.removeFrom", { name: machineName(m) })}
                      >
                        {t("agentPush.flow.remove")}
                      </Button>
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </TableWrap>

        {mixed ? (
          <div className="notice gold mt-3" role="note">
            <Icon name="warning" size={17} />
            <p>{t("agentPush.flow.mixed")}</p>
          </div>
        ) : blocked ? (
          <p className="note mt-3">{t("agentPush.flow.blocked")}</p>
        ) : (
          <div className="form-grid mt-3">
            <Field label={t("agentPush.shared.credential")}>
              <select
                className="select-field"
                value={credentialId}
                onChange={(event) => setCredentialId(event.target.value)}
              >
                <option value="">{t("agentPush.flow.chooseCredential", { kind: credentialTypeLabel(kind) })}</option>
                {offered.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
            </Field>
            {!credentials.isLoading && offered.length === 0 ? (
              <p className="note">
                {credentials.isError
                  ? t("agentPush.flow.cannotView")
                  : `${t("agentPush.flow.noneSaved", { kind: credentialTypeLabel(kind) })} `}
                {!credentials.isError ? (
                  <Link to={`/credentials?new=${kind}`}>{t("agentPush.flow.addOne")}</Link>
                ) : null}
              </p>
            ) : null}
          </div>
        )}
      </Panel>

      {!mixed && !blocked && chosen ? (
        <PushPreflightPanel
          key={`${chosen.id}:${targets.join(",")}`}
          targets={targets}
          windowsTargets={windows.map((m) => m.primary_ip as string)}
          credential={{ id: chosen.id, name: chosen.name, type: chosen.type, client_id: chosen.client_id }}
        />
      ) : null}
    </section>
  );
}
