// "By subnet": the list a person confirms, built from a subnet (design
// 2026-09-30 §1.1, §1.7; CLAUDE.md 2a(f) as amended 2026-09-30).
//
//   1. THE SUBNET is one of this organization's active push authorizations,
//      chosen from a list. There is no text box: a subnet that is not
//      authorized cannot be chosen, and none can be typed.
//   2. THE CREDENTIAL is one push credential. One run presents one credential,
//      so machines of the other kind are listed as not ready, never
//      half-pushed.
//   3. THE LIST is every vouched-for machine the subnet holds, each with why it
//      is or is not ready. Every row is shown; there is no "and 40 more".
//   4. START sends back the list's digest, so what is confirmed is these
//      machines and not "the subnet". If the subnet changed meanwhile, the
//      server refuses, and the list is fetched and shown again.
//
// NOTHING HERE GATES ANYTHING (invariant 6): the server checks
// `agent.push.run`, `credentials.checkout`, every bound and the digest.

import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import { Trans } from "../../i18n/Trans";
import { Button, Field, Td, Th } from "../../components/ui";
import { Panel, StatusPill, TableWrap } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { credentialTypeLabel } from "../credentials/credentialTypes";
import { osFamilyLabel } from "../devices/osLabel";
import { useCreatePushRunMutation, usePushCredentials, usePushRunPrefixes, usePushRunPreview } from "./api";
import { formatCount, refusalReasonLabel } from "./pushState";
import { isListChanged, readinessChip } from "./runState";

export function PushBySubnetFlow({ onClose }: { onClose: () => void }) {
  const t = useT();
  const navigate = useNavigate();
  const prefixes = usePushRunPrefixes();
  const credentials = usePushCredentials();
  const [prefix, setPrefix] = useState("");
  const [credentialId, setCredentialId] = useState("");
  const [changed, setChanged] = useState(false);
  const preview = usePushRunPreview(prefix, credentialId);
  const start = useCreatePushRunMutation();
  const list = preview.data;

  function confirm() {
    if (!list) return;
    setChanged(false);
    start.mutate(
      { prefix: list.prefix, credential_id: list.credential_id, list_digest: list.list_digest },
      {
        onSuccess: (run) => navigate(`/devices/push-runs/${run.id}`),
        onError: (error) => {
          if (isListChanged(error)) {
            setChanged(true);
            void preview.refetch();
          }
        },
      },
    );
  }

  return (
    <Panel
      title={t("agentPush.subnet.title")}
      subtitle={t("agentPush.subnet.subtitle")}
      actions={
        <Button variant="ghost" onClick={onClose}>
          {t("agentPush.shared.close")}
        </Button>
      }
    >
      <div className="form-grid">
        <Field label={t("agentPush.subnet.subnet")}>
          <select
            className="select-field"
            value={prefix}
            onChange={(event) => {
              setPrefix(event.target.value);
              setChanged(false);
            }}
          >
            <option value="">{t("agentPush.subnet.chooseSubnet")}</option>
            {(prefixes.data ?? []).map((p) => (
              <option key={p.prefix} value={p.prefix}>
                {t("agentPush.subnet.addressesInOption", { prefix: p.prefix, formatted: formatCount(p.address_count) })}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t("agentPush.shared.credential")}>
          <select
            className="select-field"
            value={credentialId}
            onChange={(event) => {
              setCredentialId(event.target.value);
              setChanged(false);
            }}
          >
            <option value="">{t("agentPush.subnet.choosePushCredential")}</option>
            {(credentials.data ?? []).map((c) => (
              <option key={c.id} value={c.id}>
                {c.name} ({credentialTypeLabel(c.type)})
              </option>
            ))}
          </select>
        </Field>
      </div>

      {prefixes.isError ? (
        <p role="alert" className="form-error mt-3">
          {describeSubmitError(prefixes.error)}
        </p>
      ) : null}
      {prefixes.isSuccess && prefixes.data.length === 0 ? (
        <p className="note mt-3">{t("agentPush.subnet.noSubnet")}</p>
      ) : null}
      {preview.isLoading ? <p className="note mt-3">{t("agentPush.subnet.listing")}</p> : null}
      {preview.isError ? (
        <p role="alert" className="form-error mt-3">
          {describeSubmitError(preview.error)}
        </p>
      ) : null}
      {changed ? (
        <p role="alert" className="form-error mt-3">
          {t("agentPush.subnet.changed")}
        </p>
      ) : null}

      {list ? (
        <>
          <div className="notice gold mt-3" role="note">
            <Icon name="warning" size={17} />
            <p>{t("agentPush.subnet.warning", { concurrency: list.concurrency })}</p>
          </div>
          <TableWrap>
            <table aria-label={t("agentPush.subnet.tableLabel")}>
              <thead>
                <tr>
                  <Th>{t("agentPush.subnet.machine")}</Th>
                  <Th>{t("agentPush.subnet.address")}</Th>
                  <Th>{t("agentPush.subnet.os")}</Th>
                  <Th>{t("agentPush.subnet.ready")}</Th>
                </tr>
              </thead>
              <tbody>
                {list.rows.map((row) => (
                  <tr key={row.device_id}>
                    <Td>
                      <Link to={`/devices/${row.device_id}`}>{row.hostname || row.address}</Link>
                    </Td>
                    <Td>
                      <code>{row.address}</code>
                    </Td>
                    <Td>{row.os_family ? osFamilyLabel(row.os_family) : t("agentPush.subnet.notRecorded")}</Td>
                    <Td>
                      {row.ready ? (
                        <StatusPill value="ready" label={t("agentPush.subnet.readyPill")} kind="ok" />
                      ) : (
                        row.reasons.map((reason) => (
                          <span key={reason} title={refusalReasonLabel(reason, list.reason_help[reason])}>
                            <StatusPill value={reason} label={readinessChip(reason)} kind="warn" />
                          </span>
                        ))
                      )}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
          {list.unvouched_count > 0 ? (
            <p className="note mt-3">
              <Trans
                k="agentPush.subnet.unvouched"
                vars={{ count: list.unvouched_count }}
                components={{ link: <Link to="/discovered" /> }}
              />
            </p>
          ) : null}
          {list.awaiting_review_count > 0 ? (
            <p className="note mt-3">{t("agentPush.subnet.awaitingReview", { count: list.awaiting_review_count })}</p>
          ) : null}
          {start.isError && !isListChanged(start.error) ? (
            <p role="alert" className="form-error mt-3">
              {describeSubmitError(start.error)}
            </p>
          ) : null}
          <div className="mt-3">
            <Button onClick={confirm} disabled={list.ready_count === 0 || start.isPending}>
              {t("agentPush.subnet.start", { count: list.ready_count })}
            </Button>
          </div>
        </>
      ) : null}
    </Panel>
  );
}
