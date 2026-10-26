/**
 * Managed deployment — GPO and Intune.
 * `docs/modules/agent-distribution/spec-managed-distribution.md`.
 *
 * THIS SCREEN IS TWO ACTS AND IT KEEPS THEM APART ON PURPOSE.
 *
 * The package is inert: an artefact, a digest, the staging steps and a
 * token-free install invocation. It carries no credential, and the route that
 * builds it mints none — because a GPO distribution point is read by every
 * machine account in the domain by construction, so a credential staged on one
 * is a permanent fleet-wide enrolment secret. That is the property the whole
 * managed path rests on, so the console must not blur it by handing both halves
 * over in one click.
 *
 * The credential is the second act, behind its own button, its own permission
 * (`enrollment.manage`), and a confirmation that states the two bounds the
 * operator is choosing. THE BOUNDS ARE THE BLAST RADIUS. A fleet cannot race
 * for a single-use token, so this credential is multi-use by necessity; what
 * makes it defensible rather than a standing secret is that `max_uses` and the
 * lifetime are required, capped, and sized by the person doing the rollout.
 * `POST /enrollment/tokens` still defaults `max_uses` to null — unlimited — so
 * this screen exists partly to stop a fleet token being minted there.
 *
 * WHAT IT REFUSES TO PRETEND. `verified_at_install` comes from the server and
 * is FALSE on both pushed channels: GPO and Intune copy a file from a share and
 * run it, verifying nothing. The reassurance the one-machine command earns —
 * the endpoint checks the digest before installing — is not available here, and
 * the panel says so instead of showing the same green line everywhere.
 */

import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { ConfirmDialog } from "../../components/confirm";
import { describeSubmitError } from "../../components/form";
import { Badge, Button, Dialog, Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import { Trans } from "../../i18n/Trans";
import type {
  AgentDeploymentChannel,
  AgentDeploymentToken,
  AgentRelease,
} from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDateTime } from "../../lib/datetime";
import { requireKnown } from "../../lib/internalClient";
import { platformLabel } from "./platform";

// Catalogue keys, worded when drawn.
const CHANNELS: { value: AgentDeploymentChannel; label: MessageKey; blurb: MessageKey }[] = [
  { value: "gpo", label: "agentReleases.managed.gpo", blurb: "agentReleases.managed.gpoBlurb" },
  { value: "intune", label: "agentReleases.managed.intune", blurb: "agentReleases.managed.intuneBlurb" },
  { value: "manual", label: "agentReleases.managed.manual", blurb: "agentReleases.managed.manualBlurb" },
];

/** Sized to the rollout, not to the ceiling — see the confirmation copy. */
const DEFAULT_USES = 50;
const DEFAULT_HOURS = 72;
/** `managed.MAX_DEPLOYMENT_USES` / `MAX_DEPLOYMENT_HOURS`. Mirrored so the
 *  refusal is visible before the request, never so it replaces it. */
const MAX_USES = 1000;
const MAX_HOURS = 336;

async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

function Copyable({ label, command }: { label: string; command: string }) {
  const t = useT();
  const [copied, setCopied] = useState(false);
  return (
    <div className="mt-3">
      <div className="note">{label}</div>
      <pre className="code mt-1">{command}</pre>
      <div className="mt-1 flex items-center gap-2">
        <Button
          variant="ghost"
          onClick={() => {
            void copyToClipboard(command).then(setCopied);
          }}
        >
          {copied ? t("agentReleases.managed.copied") : t("agentReleases.managed.copy")}
        </Button>
      </div>
    </div>
  );
}

export function ManagedDeploymentDialog({
  release,
  onClose,
  timezone,
  siteId,
}: {
  release: AgentRelease | null;
  onClose: () => void;
  timezone?: string | null;
  /**
   * The site the machines land in (MSP clients spec §1.5): the deployment
   * token is that site's, like every enrollment token. `null` until the page
   * knows it; a mint then refuses with a sentence rather than sending a body
   * the server would answer with a 422.
   */
  siteId: string | null;
}) {
  const t = useT();
  const api = useApi();
  const [channel, setChannel] = useState<AgentDeploymentChannel>("gpo");
  const [maxUses, setMaxUses] = useState(String(DEFAULT_USES));
  const [hours, setHours] = useState(String(DEFAULT_HOURS));
  const [confirming, setConfirming] = useState(false);
  const [minted, setMinted] = useState<AgentDeploymentToken | null>(null);

  const pkg = useQuery({
    queryKey: ["agent-deployment-package", release?.id, channel],
    queryFn: () => api.getAgentDeploymentPackage(release?.id ?? "", channel),
    enabled: release != null,
  });

  const mint = useMutation({
    mutationFn: () =>
      api.mintAgentDeploymentToken(release?.id ?? "", {
        max_uses: Number(maxUses),
        expires_in_hours: Number(hours),
        channel,
        // PHASE 3: the client/site selector. The page passes the internal
        // client's "Principal" site (`lib/internalClient.ts`).
        site_id: requireKnown(siteId),
      }),
    onSuccess: setMinted,
  });

  const uses = Number(maxUses);
  const life = Number(hours);
  const boundsValid =
    Number.isInteger(uses) && uses >= 1 && uses <= MAX_USES &&
    Number.isInteger(life) && life >= 1 && life <= MAX_HOURS;

  return (
    <>
      <Dialog
        open={release != null}
        onClose={onClose}
        title={t("agentReleases.managed.title")}
        dismissible={!confirming}
      >
        {release != null && (
          <>
            <p>
              <Trans k="agentReleases.managed.intro" components={{ strong: <strong /> }} />
            </p>

            <div className="form-grid mt-3">
              <Field label={t("agentReleases.managed.channel")} full>
                <select
                  className={inputClass}
                  value={channel}
                  onChange={(event) =>
                    setChannel(event.target.value as AgentDeploymentChannel)
                  }
                >
                  {CHANNELS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {t(option.label)}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            <p className="note mt-1">
              {(() => {
                const blurb = CHANNELS.find((option) => option.value === channel)?.blurb;
                return blurb ? t(blurb) : null;
              })()}
            </p>

            {pkg.isLoading ? <p className="note mt-3">{t("agentReleases.managed.loading")}</p> : null}
            {pkg.isError ? (
              <p role="alert" className="form-error mt-3">
                {describeSubmitError(pkg.error)}
              </p>
            ) : null}

            {pkg.data ? (
              <>
                <h3 className="cell-title mt-4">{t("agentReleases.managed.staging")}</h3>
                <ol className="mt-2 list-decimal space-y-3 pl-5">
                  {pkg.data.staging.map((step) => (
                    <li key={step.title}>
                      <div className="cell-title">{step.title}</div>
                      <div className="cell-sub">{step.detail}</div>
                      {step.command ? (
                        <pre className="code mt-1">{step.command}</pre>
                      ) : null}
                    </li>
                  ))}
                </ol>

                <Copyable
                  label={t("agentReleases.managed.install")}
                  command={pkg.data.install_command}
                />
                <Copyable
                  label={t("agentReleases.managed.enrol")}
                  command={pkg.data.enrol_command}
                />

                {/* THE INTEGRITY ACCOUNTING, AND THE MISSING CHECK IS NAMED.
                    `spec.md` §4.4 rests on the operator comparing the digest
                    the server computed against the digest of what they built.
                    On a pushed channel the endpoint re-verifies nothing, so the
                    comparison moves to the administrator and stops there. */}
                <h3 className="cell-title mt-4">{t("agentReleases.managed.integrity")}</h3>
                <p className="note mono mt-1 break-all">{pkg.data.integrity.sha256}</p>
                <div className="mt-2">
                  <Badge
                    variant={pkg.data.integrity.verified_at_install ? "success" : "warning"}
                  >
                    {pkg.data.integrity.verified_at_install
                      ? t("agentReleases.managed.verifies")
                      : t("agentReleases.managed.verifiesNothing")}
                  </Badge>
                </div>
                <Copyable
                  label={t("agentReleases.managed.verifyFile")}
                  command={pkg.data.integrity.verify_command}
                />
                <ul className="note mt-2 list-disc space-y-1 pl-5">
                  {pkg.data.integrity.notes.map((note) => (
                    <li key={note}>{note}</li>
                  ))}
                </ul>

                {/* ACT TWO, VISUALLY SEPARATED, because it is a different kind
                    of thing: everything above is inert and this mints a live
                    credential. */}
                <hr className="mt-5" />
                <h3 className="cell-title mt-4">{t("agentReleases.managed.credentialTitle")}</h3>
                <p className="note mt-1">
                  <Trans k="agentReleases.managed.credentialNote" components={{ strong: <strong /> }} />
                </p>

                <div className="form-grid mt-3">
                  <Field label={t("agentReleases.managed.maxEnrolments", { max: MAX_USES })}>
                    <input
                      className={inputClass}
                      type="number"
                      min={1}
                      max={MAX_USES}
                      value={maxUses}
                      onChange={(event) => setMaxUses(event.target.value)}
                    />
                  </Field>
                  <Field label={t("agentReleases.managed.validHours", { max: MAX_HOURS })}>
                    <input
                      className={inputClass}
                      type="number"
                      min={1}
                      max={MAX_HOURS}
                      value={hours}
                      onChange={(event) => setHours(event.target.value)}
                    />
                  </Field>
                </div>

                {minted == null ? (
                  <div className="mt-3">
                    <Button
                      variant="outline"
                      disabled={!boundsValid || mint.isPending}
                      onClick={() => setConfirming(true)}
                    >
                      {t("agentReleases.managed.create")}
                    </Button>
                  </div>
                ) : (
                  <>
                    <p className="mt-3">
                      <Trans
                        k="agentReleases.managed.shownOnce"
                        vars={{ date: formatDateTime(minted.token_expires_at, timezone), uses: minted.max_uses }}
                        components={{ strong: <strong /> }}
                      />
                    </p>
                    <pre className="code mt-2">{minted.token}</pre>
                    <ul className="note mt-2 list-disc space-y-1 pl-5">
                      {minted.blast_radius.map((line) => (
                        <li key={line}>{line}</li>
                      ))}
                    </ul>
                  </>
                )}

                {mint.isError ? (
                  <p role="alert" className="form-error mt-3">
                    {describeSubmitError(mint.error)}
                  </p>
                ) : null}
              </>
            ) : null}
          </>
        )}
      </Dialog>

      {/* Not `destructive`: nothing is destroyed. But the body states the reach
          in machines and in hours rather than saying "are you sure", because
          those two numbers are the entire decision. */}
      <ConfirmDialog
        open={confirming}
        title={t("agentReleases.managed.confirmTitle")}
        subject={
          release ? `${release.version} · ${platformLabel(release.os, release.arch)} · ${channel}` : ""
        }
        body={t("agentReleases.managed.confirmBody", { uses, hours: life })}
        confirmLabel={t("agentReleases.managed.confirmLabel")}
        onCancel={() => setConfirming(false)}
        onConfirm={() => {
          setConfirming(false);
          mint.mutate();
        }}
      />
    </>
  );
}
