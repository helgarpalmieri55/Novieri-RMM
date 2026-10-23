import { useState } from "react";
import { Dialog, Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import { Trans } from "../../i18n/Trans";
import { DetailFacts } from "./DetailFacts";
import { ErrorLine } from "./ErrorLine";
import {
  useCreateIpamAddressMutation,
  useIpamAddressHistory,
  useReleaseIpamAddressMutation,
  useUpdateIpamAddressMutation,
} from "./api";
import {
  allocationMethodWord,
  describeHistoryDetail,
  formatAge,
  lastSeenMeaning,
  statePresentation,
  statusPresentation,
} from "./ipamState";
import { variantKind } from "./kinds";
import type { GridEntryOut } from "./types";
import { ALLOCATION_METHODS } from "./types";
import { Icon } from "../../shell/icons";
import { KV } from "../../shell/viz";
import { Btn, StatusPill } from "../../shell/components";

const ACTOR_WORD: Readonly<Record<string, MessageKey>> = {
  user: "ipam.address.actor.user",
  agent: "ipam.address.actor.agent",
  system: "ipam.address.actor.system",
};

const STATE_WORD: Readonly<Record<string, MessageKey>> = {
  free: "ipam.address.stateWord.free",
  reserved: "ipam.address.stateWord.reserved",
  assigned: "ipam.address.stateWord.assigned",
  deprecated: "ipam.address.stateWord.deprecated",
};

// One address, and the four transitions an operator with `ipam.assign` can make
// on it.
//
// THERE IS NO "SET STATE" CONTROL, and there cannot be. `state` is documented
// intent and the API exposes no field that writes it directly: reserve and
// assign are separate transitions with separate predicates and separate audit
// events, and `POST /ipam/addresses` picks between them with a `reserve` flag.
// A dropdown here that let someone pick `assigned` would be a bypass of both
// predicates and of the audit trail, so the buttons ARE the transitions.
//
// The dialog shows `status` and `state` side by side, labelled differently,
// because they answer different questions: "what is happening here" versus
// "what did a person write down". A dialog that showed one under the other's
// name is how an operator concludes the plan changed when only reality did.
export function AddressDialog({
  entry,
  subnetPrefix,
  canAssign,
  onClose,
}: {
  entry: GridEntryOut;
  subnetPrefix: string;
  canAssign: boolean;
  onClose: () => void;
}) {
  const t = useT();
  const create = useCreateIpamAddressMutation();
  const update = useUpdateIpamAddressMutation();
  const release = useReleaseIpamAddressMutation();
  const history = useIpamAddressHistory(entry.address_id ?? undefined);

  const [hostname, setHostname] = useState(entry.hostname);
  const [assetLabel, setAssetLabel] = useState(entry.asset_label);
  const [deviceId, setDeviceId] = useState(entry.device_id ?? "");
  const [description, setDescription] = useState("");
  const [mac, setMac] = useState(entry.mac ?? "");
  const [method, setMethod] = useState("static");

  const status = statusPresentation(entry.status);
  const state = statePresentation(entry.state);
  const inDhcpPool = entry.range_kinds.includes("dhcp_pool");
  const error = create.error ?? update.error ?? release.error;
  /** A documented state as the history prints it; one this build does not know stays as the server wrote it. */
  const stateWord = (value: string | null): string =>
    value === null ? t("ipam.address.stateNone") : Object.hasOwn(STATE_WORD, value) ? t(STATE_WORD[value] as MessageKey) : value;
  const actorWord = (value: string): string =>
    Object.hasOwn(ACTOR_WORD, value) ? t(ACTOR_WORD[value] as MessageKey) : value;

  return (
    <Dialog open onClose={onClose} title={entry.address}>
      <p className="note" style={{ marginBottom: 12 }}>
        <Trans k="ipam.address.inSubnet" vars={{ prefix: subnetPrefix }} components={{ mono: <span className="mono" /> }} />
      </p>
      <KV
        items={[
          [
            t("ipam.fields.statusDerived"),
            <>
              <StatusPill value={entry.status} kind={variantKind(status.variant)} label={status.label} />
              <div className="note" style={{ marginTop: 4 }}>
                {status.meaning}
              </div>
            </>,
          ],
          [
            t("ipam.fields.stateDocumented"),
            <>
              <StatusPill value={entry.state ?? "none"} kind={variantKind(state.variant)} label={state.label} />
              <div className="note" style={{ marginTop: 4 }}>
                {state.meaning}
              </div>
            </>,
          ],
          [
            t("ipam.fields.lastSeen"),
            <>
              <span className="mono" title={lastSeenMeaning()}>
                {formatAge(entry.last_seen_at)}
              </span>
              <div className="note" style={{ marginTop: 4 }}>
                {lastSeenMeaning()}
              </div>
            </>,
          ],
        ]}
      />

      {inDhcpPool && (
        <div className="notice gold" style={{ marginTop: 14 }}>
          <Icon name="alert-triangle" size={16} />
          <p>{t("ipam.address.dhcpNotice")}</p>
        </div>
      )}

      {canAssign && (
        <div style={{ marginTop: 16 }}>
          <div className="form-grid">
            <Field label={t("ipam.fields.hostname")}>
              <input
                className={inputClass}
                value={hostname}
                onChange={(e) => setHostname(e.target.value)}
              />
            </Field>
            <Field label={t("ipam.fields.assetLabel")}>
              <input
                className={inputClass}
                value={assetLabel}
                onChange={(e) => setAssetLabel(e.target.value)}
              />
            </Field>
            <Field label={t("ipam.address.deviceId")} full>
              <input
                className={inputClass}
                placeholder={t("ipam.address.deviceIdPlaceholder")}
                value={deviceId}
                onChange={(e) => setDeviceId(e.target.value)}
              />
            </Field>
            <Field label={t("ipam.fields.mac")}>
              <input className={inputClass} value={mac} onChange={(e) => setMac(e.target.value)} />
            </Field>
            <Field label={t("ipam.fields.description")}>
              <input
                className={inputClass}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </Field>
            <Field label={t("ipam.address.allocationMethod")}>
              <select
                className={inputClass}
                value={method}
                onChange={(e) => setMethod(e.target.value)}
              >
                {ALLOCATION_METHODS.map((value) => (
                  <option key={value} value={value}>
                    {allocationMethodWord(value)}
                  </option>
                ))}
              </select>
            </Field>
          </div>

          <div className="form-actions">
            <Btn
              label={t("ipam.address.assign")}
              icon="check"
              variant="primary"
              disabled={create.isPending}
              onClick={() =>
                create.mutate(
                  {
                    address: entry.address,
                    device_id: deviceId || null,
                    asset_label: assetLabel,
                    hostname,
                    description,
                    mac: mac || null,
                    allocation_method: method,
                  },
                  { onSuccess: onClose },
                )
              }
            />
            <Btn
              label={t("ipam.address.reserve")}
              icon="lock"
              disabled={create.isPending}
              onClick={() =>
                create.mutate(
                  { address: entry.address, reserve: true, hostname, description },
                  { onSuccess: onClose },
                )
              }
            />
            {entry.address_id !== null && (
              <>
                <Btn
                  label={t("ipam.address.saveEdits")}
                  icon="edit"
                  disabled={update.isPending}
                  onClick={() =>
                    update.mutate(
                      {
                        id: entry.address_id as string,
                        body: {
                          hostname,
                          asset_label: assetLabel,
                          description,
                          allocation_method: method,
                        },
                      },
                      { onSuccess: onClose },
                    )
                  }
                />
                <Btn
                  label={t("ipam.address.release")}
                  icon="unlink"
                  variant="ghost"
                  disabled={release.isPending}
                  onClick={() =>
                    release.mutate(entry.address_id as string, { onSuccess: onClose })
                  }
                />
              </>
            )}
          </div>
        </div>
      )}

      {!canAssign && (
        <p className="note" style={{ marginTop: 14 }}>
          {t("ipam.address.noPermission")}
        </p>
      )}

      {error && <ErrorLine error={error} />}

      {entry.address_id !== null && (
        <div style={{ marginTop: 18 }}>
          <h3 className="panel-title">{t("ipam.address.history")}</h3>
          <p className="note" style={{ margin: "4px 0 10px" }}>
            {t("ipam.address.historyNote")}
          </p>
          {history.error && <ErrorLine error={history.error} />}
          {/*
            Each entry is its state change AND §11.3's payload for that change:
            the claimant and the allocation method on an assignment, the previous
            claimant on a release. A list that stopped at "assigned (free →
            assigned) by user" says an address changed hands and refuses to say
            to whom, which is the question somebody opened this panel to ask.

            `formatAge(item.ts)`, never a timestamp: this is still the module
            that is deliberately not monitoring, and history is exactly where a
            clock time would slip in.
          */}
          <ul className="mini-list" style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {history.data?.map((item, index) => {
              const facts = describeHistoryDetail(item);
              return (
                <li key={`${item.ts}-${item.action}-${index}`} className="mini-row" style={{ alignItems: "flex-start" }}>
                  <span className="mini-icon">
                    <Icon name="history" size={16} />
                  </span>
                  <div className="mini-copy">
                    <div className="mini-title" style={{ whiteSpace: "normal" }}>
                      <span className="mono">{formatAge(item.ts)}</span> · {item.action}{" "}
                      {item.from_state !== null || item.to_state !== null ? (
                        <span className="note">
                          ({stateWord(item.from_state)} → {stateWord(item.to_state)})
                        </span>
                      ) : null}{" "}
                      <span className="note">{t("ipam.address.by", { actor: actorWord(item.actor_type) })}</span>
                    </div>
                    <div style={{ marginTop: 4 }}>
                      <DetailFacts facts={facts} />
                    </div>
                  </div>
                </li>
              );
            })}
            {history.data && history.data.length === 0 && (
              <li className="note">{t("ipam.address.historyNone")}</li>
            )}
          </ul>
        </div>
      )}
    </Dialog>
  );
}
