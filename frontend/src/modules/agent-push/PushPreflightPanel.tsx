import { useMemo } from "react";
import { NoValue } from "../../components/emptystate";
import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import { Trans } from "../../i18n/Trans";
import { Button, Td, Th } from "../../components/ui";
import { ApiError } from "../../lib/api";
import { Panel, QueryState, StatusPill, TableWrap, type Kind } from "../../shell/components";
import { Icon } from "../../shell/icons";
import {
  useConfirmHostKeyMutation,
  usePushHostKeys,
  usePushRequestMutation,
} from "./api";
import {
  hostKeysForClient,
  isWinrmRow,
  kdcNotFound,
  refusalReasonLabel,
  winrmInstallable,
  winrmTargetState,
  type WinrmTargetState,
} from "./pushState";
import {
  PUSH_BATCH_MAX,
  WINDOWS_DOMAIN_CREDENTIAL_TYPE,
  type PushRefusalDetail,
} from "./types";

// **THE PRE-FLIGHT PANEL — THE LAST SCREEN BEFORE A CREDENTIALED PUSH, AND THE
// ONE THAT MUST NOT LIE (spec §10.4).**
//
// This screen presents administrative credentials to machines this platform has
// never managed and cannot verify, so §10.4 fixes what it must show, in order,
// and every item below is a requirement rather than polish:
//
//   1. EVERY target address, listed — no "and 22 more". The operator reads the
//      whole list.
//   2. The credential, by name (never a secret), with its scope.
//   3. The site server that will perform the push.
//   4. The release version and SHA-256.
//   5. The enrolment token's expiry and single use.
//   6. The host-key fingerprints AWAITING CONFIRMATION (§7.3) — the crux.
//   7. One plain sentence, not a tooltip.
//   8. The account-lockout warning (§5.3).
//
// **THE HOST KEY IS THE CRUX FOR SSH, AND THIS SCREEN IS WHERE A HUMAN CONFIRMS
// IT.** On first contact the platform cannot authenticate the target (§1.1,
// §7.3). A probe fetches the fingerprint carrying no credential; the operator
// compares it out-of-band and confirms it here; only then is the install
// unlocked, and the confirmed key is signed into the push so the agent verifies
// against it.
//
// **A WINDOWS TARGET HAS NO HOST KEY, AND IS NEVER OFFERED ONE TO CONFIRM**
// (plan W3/W4, 2026-09-28). It is pushed over WinRM with Kerberos only, and
// Kerberos mutual authentication -- a ticket only the real computer account can
// open -- is what authenticates it. Its probe row carries a marker (`winrm`, or
// the install's `kerberos` sentinel) where a fingerprint would be; offering that
// for confirmation would ask a human to vouch for a string that is not a key.
// What a Windows row shows instead is what its credential-free probe found:
// whether WinRM answered on HTTPS (5986) and HTTP (5985), and whether a domain
// controller was found by DNS and reachable from the site server. An install is
// offered when WinRM answered on either; a domain controller not found via DNS
// is a WARNING (review I2) -- the credential may name its domain controller.
//
// **THIS PANEL GATES NOTHING.** The server's four bounds, the
// `credentials.checkout` conjunction and the host-key check are the only
// authority (invariant #6); disabling a button here saves an operator a refusal
// and prevents nothing.

// The words are catalogue keys, looked up when drawn (never held as text).
const WINRM_STATE: Record<WinrmTargetState, { label: MessageKey; kind: Kind }> = {
  not_probed: { label: "agentPush.preflight.stateNotProbedYet", kind: "warn" },
  no_winrm: { label: "agentPush.preflight.stateNoWinrm", kind: "crit" },
  kdc_not_found: { label: "agentPush.preflight.stateKdcNotFound", kind: "warn" },
  ready: { label: "agentPush.preflight.stateReady", kind: "ok" },
};

interface Props {
  /** The addresses a human selected from a list they were shown (§4.2, §10.4). */
  targets: string[];
  /**
   * Which of them the asset rows say are Windows, so a Windows target that has
   * not been probed yet waits for a probe rather than for a key. A target whose
   * probe row is a WinRM row is treated as Windows whether listed here or not.
   */
  windowsTargets?: readonly string[];
  /**
   * The chosen push credential — id, label, type and client only, never a
   * secret. Its client is the push's (MSP clients phase 2, Task 12).
   */
  credential: { id: string; name: string; type?: string; client_id?: string | null };
}

/** The structured 422 body (spec §9), or null for any other failure. */
function refusalOf(err: unknown): PushRefusalDetail | null {
  if (!(err instanceof ApiError) || err.status !== 422) return null;
  try {
    const parsed = JSON.parse(err.message) as { detail?: unknown };
    const detail = parsed.detail;
    if (detail && typeof detail === "object" && Array.isArray((detail as PushRefusalDetail).targets)) {
      return detail as PushRefusalDetail;
    }
  } catch {
    /* not JSON: described as a sentence below */
  }
  return null;
}

export function PushPreflightPanel({ targets, windowsTargets = [], credential }: Props) {
  const t = useT();
  const hostKeys = usePushHostKeys();
  const probe = usePushRequestMutation();
  const install = usePushRequestMutation();
  const confirm = useConfirmHostKeyMutation();

  const client = credential.client_id ?? null;
  const byAddress = useMemo(() => hostKeysForClient(hostKeys.data ?? [], client), [hostKeys.data, client]);

  const isWindows = (address: string) => {
    const row = byAddress.get(address);
    return windowsTargets.includes(address) || (row !== undefined && isWinrmRow(row));
  };
  const sshTargets = targets.filter((address) => !isWindows(address));
  const winTargets = targets.filter(isWindows);

  const everyTargetReady = targets.every((address) =>
    isWindows(address)
      ? winrmInstallable(winrmTargetState(byAddress.get(address)))
      : byAddress.get(address)?.installable === true,
  );
  const domainAccount = credential.type === WINDOWS_DOMAIN_CREDENTIAL_TYPE;

  return (
    <Panel title={t("agentPush.preflight.title")} subtitle={t("agentPush.preflight.cannotVerify")}>
      <div className="space-y-6">

      {/* 1 — every target address, listed */}
      <section>
        <h3 className="cell-title">
          {t("agentPush.preflight.targets", { count: targets.length })}
        </h3>
        <ul className="mt-2 flex flex-wrap gap-2">
          {targets.map((address) => {
            const key = byAddress.get(address);
            const state = isWindows(address)
              ? WINRM_STATE[winrmTargetState(key)]
              : !key
                ? { label: "agentPush.preflight.stateNoHostKey" as const, kind: "warn" as const }
                : !key.confirmed_fingerprint
                  ? { label: "agentPush.preflight.stateAwaiting" as const, kind: "warn" as const }
                  : key.installable
                    ? { label: "agentPush.preflight.stateConfirmed" as const, kind: "ok" as const }
                    : { label: "agentPush.preflight.stateKeyChanged" as const, kind: "crit" as const };
            return (
              <li key={address} className="flex items-center gap-2">
                <code className="mono">{address}</code>
                <StatusPill value={state.label} kind={state.kind} label={t(state.label)} />
              </li>
            );
          })}
        </ul>
        {/* INVARIANT 2a(f), AT THE CONTROL IT BOUNDS. */}
        <p className="note mt-2">{t("agentPush.preflight.everyAddress", { max: PUSH_BATCH_MAX })}</p>
      </section>

      {/* 2 — the credential, by name */}
      <section>
        <h3 className="cell-title">{t("agentPush.preflight.credential")}</h3>
        <p className="mt-1">
          {credential.name}{" "}
          {domainAccount ? (
            <span className="note">{t("agentPush.preflight.domainAccount")}</span>
          ) : (
            <span className="note">{t("agentPush.preflight.dedicated")}</span>
          )}
        </p>
      </section>

      {/* 6 — probe, then (SSH only) confirm each fingerprint */}
      <section>
        <div className="flex items-center justify-between">
          <h3 className="cell-title">{winTargets.length > 0 && sshTargets.length === 0 ? t("agentPush.preflight.readiness") : t("agentPush.preflight.hostKeysAndReadiness")}</h3>
          <Button
            onClick={() =>
              probe.mutate({ mode: "probe", credential_id: credential.id, targets })
            }
            disabled={probe.isPending}
          >
            {probe.isPending ? t("agentPush.preflight.probing") : t("agentPush.preflight.probe")}
          </Button>
        </div>
        <p className="note mt-1">
          <Trans k="agentPush.preflight.probeNote" components={{ strong: <strong /> }} />
        </p>

        <QueryState
          loading={hostKeys.isLoading}
          error={hostKeys.error}
          empty={false}
          emptyLabel=""
        >
          {sshTargets.length > 0 ? (
            <TableWrap>
              <table aria-label={t("agentPush.preflight.sshTargets")}>
                <thead>
                  <tr>
                    <Th>{t("agentPush.preflight.address")}</Th>
                    <Th>{t("agentPush.preflight.fingerprint")}</Th>
                    <Th>{t("agentPush.preflight.type")}</Th>
                    <Th>{t("agentPush.preflight.status")}</Th>
                    <Th label={t("agentPush.preflight.confirm")} />
                  </tr>
                </thead>
                <tbody>
                  {sshTargets.map((address) => {
                    const key = byAddress.get(address);
                    return (
                      <tr key={address}>
                        <Td>
                          <code>{address}</code>
                        </Td>
                        <Td>
                          {key ? (
                            <code className="mono note break-all">{key.observed_fingerprint}</code>
                          ) : (
                            <span className="note">{t("agentPush.preflight.notProbedLower")}</span>
                          )}
                        </Td>
                        <Td>{key?.observed_key_type || <NoValue label={t("agentPush.preflight.notProbed")} />}</Td>
                        <Td>
                          {!key ? (
                            <NoValue label={t("agentPush.preflight.notProbed")} />
                          ) : key.installable ? (
                            <StatusPill value="confirmed" kind="ok" label={t("agentPush.preflight.confirmed")} />
                          ) : key.confirmed_fingerprint ? (
                            <StatusPill value="key-changed" kind="crit" label={t("agentPush.preflight.keyChangedReconfirm")} />
                          ) : (
                            <StatusPill value="unconfirmed" kind="warn" label={t("agentPush.preflight.unconfirmed")} />
                          )}
                        </Td>
                        <Td>
                          {key && !key.installable ? (
                            <Button
                              variant="outline"
                              onClick={() =>
                                confirm.mutate({
                                  address,
                                  fingerprint: key.observed_fingerprint,
                                })
                              }
                              disabled={confirm.isPending}
                            >
                              {t("agentPush.preflight.confirmKey", { fingerprint: key.observed_fingerprint.slice(0, 18) })}
                            </Button>
                          ) : (
                            <NoValue label={t("agentPush.preflight.nothingToConfirm")} />
                          )}
                        </Td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </TableWrap>
          ) : null}
          {winTargets.length > 0 ? (
            <TableWrap>
              <table aria-label={t("agentPush.preflight.windowsTargets")}>
                <thead>
                  <tr>
                    <Th>{t("agentPush.preflight.address")}</Th>
                    <Th>{t("agentPush.preflight.winrm")}</Th>
                    <Th>{t("agentPush.preflight.domainController")}</Th>
                    <Th>{t("agentPush.preflight.status")}</Th>
                  </tr>
                </thead>
                <tbody>
                  {winTargets.map((address) => {
                    const key = byAddress.get(address);
                    const probed = key !== undefined && isWinrmRow(key);
                    const state = WINRM_STATE[winrmTargetState(key)];
                    return (
                      <tr key={address}>
                        <Td>
                          <code>{address}</code>
                        </Td>
                        <Td>
                          {probed ? (
                            <>
                              <span className="block">
                                {t("agentPush.preflight.https", {
                                  answer: key.winrm_https ? t("agentPush.preflight.answered") : t("agentPush.preflight.noAnswer"),
                                })}
                              </span>
                              <span className="block">
                                {t("agentPush.preflight.http", {
                                  answer: key.winrm_http ? t("agentPush.preflight.answered") : t("agentPush.preflight.noAnswer"),
                                })}
                              </span>
                            </>
                          ) : (
                            <NoValue label={t("agentPush.preflight.notProbed")} />
                          )}
                        </Td>
                        <Td>
                          {probed ? (
                            <>
                              {t("agentPush.preflight.dcLine", {
                                state: key.kdc_reachable ? t("agentPush.preflight.reachable") : kdcNotFound(),
                              })}
                            </>
                          ) : (
                            <NoValue label={t("agentPush.preflight.notProbed")} />
                          )}
                        </Td>
                        <Td>
                          <StatusPill value={state.label} kind={state.kind} label={t(state.label)} />
                        </Td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </TableWrap>
          ) : null}
        </QueryState>
        {probe.isError ? <Refusal error={probe.error} /> : null}
        {confirm.isError ? (
          <p role="alert" className="form-error mt-2">
            {t("agentPush.preflight.hostKeyNotConfirmed", { detail: describeSubmitError(confirm.error) })}
          </p>
        ) : null}
      </section>

      {/* 7 & 8 — the plain sentence and the lockout warning */}
      <section className="notice red" role="note">
        <Icon name="warning" size={17} />
        <div>
          <p>
            <strong>{t("agentPush.preflight.cannotVerify")}</strong>
          </p>
          <p className="mt-2">{t("agentPush.preflight.lockout")}</p>
        </div>
      </section>

      {/* The push itself — unlocked only when every target is ready */}
      <section className="flex flex-wrap items-center gap-3">
        <Button
          disabled={!everyTargetReady || install.isPending || install.isSuccess}
          onClick={() =>
            install.mutate({ mode: "install", credential_id: credential.id, targets })
          }
        >
          {install.isPending
            ? t("agentPush.preflight.dispatching")
            : t("agentPush.preflight.install", { count: targets.length })}
        </Button>
        {!everyTargetReady ? (
          <span className="note">{t("agentPush.preflight.everyTargetReady")}</span>
        ) : null}
        {install.isSuccess ? (
          <StatusPill
            value="dispatched"
            kind="ok"
            label={t("agentPush.preflight.dispatched", { batch: install.data.batch_id.slice(0, 8) })}
          />
        ) : null}
      </section>
      {install.isError ? <Refusal error={install.error} /> : null}
      </div>
    </Panel>
  );
}

/**
 * Spec §9: a refusal that does not say WHICH bound failed for WHICH address is
 * one nobody can act on. The server returns every failing reason per address;
 * this names each in plain language (`REFUSAL_REASON_LABELS`), falling back to
 * the server's own remedy for a reason this console was not taught. Any
 * other refusal (too many addresses, a Windows target with no DNS name, no
 * permission to use a stored secret) is a sentence.
 */
function Refusal({ error }: { error: unknown }) {
  const t = useT();
  const detail = refusalOf(error);
  return (
    <div className="notice gold mt-3" role="alert">
      <Icon name="warning" size={17} />
      <div>
        <p>
          <strong>{t("agentPush.preflight.refused")}</strong>
        </p>
        {detail ? (
          <ul className="mt-2 space-y-2">
            {detail.targets.map((entry) => (
              <li key={entry.address}>
                <code>{entry.address}</code>
                <ul className="ml-4 list-disc">
                  {entry.reasons.map((r) => (
                    <li key={r}>{refusalReasonLabel(r, detail.reason_help[r])}</li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-1">{describeSubmitError(error)}</p>
        )}
      </div>
    </div>
  );
}
