/**
 * The install link (install-link spec 2026-09-28 §1.1, §1.6).
 *
 * ONE LINK PER SITE (MSP clients spec §1.5, §3.2): `siteId` says whose, and a
 * machine that joins through it lands in that site and its client. The
 * networks it answers are the site's egress networks.
 *
 * ONE LINK FOR EVERY COMPUTER A TECHNICIAN SETS UP, off by default. What
 * bounds it is on this panel: the networks it answers, an hourly limit, an
 * optional automatic switch-off, and regeneration. Machines that join wait
 * for review (Devices → Waiting for review) and receive nothing but
 * inventory, health monitoring and certificate renewal until someone approves
 * them.
 *
 * THE LINK IS SHOWN ONCE. The server stores only its hash and returns the
 * full link in the one response that generated it; this panel keeps it in
 * component state until the operator says they have it, and never writes it
 * to the query cache.
 *
 * INVARIANT 6: rendering this panel is a courtesy to a holder of
 * `enrollment.link.manage`. Every route checks the permission itself.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { ConfirmDialog } from "../../components/confirm";
import { describeSubmitError } from "../../components/form";
import { Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { Trans } from "../../i18n/Trans";
import type { InstallLinkSaved, InstallLinkSettings, InstallLinkUpdate } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDateTime } from "../../lib/datetime";
import { Btn, Panel, QueryState, Toggle } from "../../shell/components";
import { MsiDownload } from "./MsiDownload";

export const INSTALL_LINK_KEY = ["install-link"] as const;
export const LINK_MANAGE = "enrollment.link.manage";

/** One network per line (or comma), blanks dropped. The server validates. */
export function parseNetworks(text: string): string[] {
  return text
    .split(/\r?\n|,/)
    .map((item) => item.trim())
    .filter((item) => item !== "");
}

/** ISO -> `<input type="datetime-local">`, in the browser's own time zone. */
export function toLocalInput(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** `<input type="datetime-local">` -> ISO, or null for "no switch-off". */
export function fromLocalInput(value: string): string | null {
  return value ? new Date(value).toISOString() : null;
}

interface Draft {
  enabled: boolean;
  networks: string;
  disableAt: string;
  hourlyLimit: string;
}

function draftFrom(settings: InstallLinkSettings): Draft {
  return {
    enabled: settings.enabled,
    networks: settings.networks.join("\n"),
    disableAt: toLocalInput(settings.disable_at),
    hourlyLimit: String(settings.hourly_limit),
  };
}

function bodyFrom(draft: Draft): InstallLinkUpdate {
  return {
    enabled: draft.enabled,
    networks: parseNetworks(draft.networks),
    disable_at: fromLocalInput(draft.disableAt),
    hourly_limit: Number(draft.hourlyLimit),
  };
}

/** The settings without the link, which is what the cache may hold. */
function settingsOf(saved: InstallLinkSaved): InstallLinkSettings {
  return {
    enabled: saved.enabled,
    has_code: saved.has_code,
    networks: saved.networks,
    disable_at: saved.disable_at,
    hourly_limit: saved.hourly_limit,
    enabled_at: saved.enabled_at,
    joined_since_enabled: saved.joined_since_enabled,
  };
}

function statusLine(settings: InstallLinkSettings, zone?: string | null): string {
  if (!settings.enabled) {
    return settings.has_code ? translate("agentReleases.link.offWithCode") : translate("agentReleases.link.offNoCode");
  }
  const joined = settings.joined_since_enabled;
  const parts = [
    translate("agentReleases.link.onSince", { date: formatDateTime(settings.enabled_at, zone) }),
    translate("agentReleases.link.joined", { count: joined }),
  ];
  if (settings.disable_at) {
    parts.push(translate("agentReleases.link.switchesOff", { date: formatDateTime(settings.disable_at, zone) }));
  }
  return `${parts.join(" · ")}.`;
}

export function InstallLinkPanel({ zone, siteId }: { zone?: string | null; siteId: string }) {
  const t = useT();
  const api = useApi();
  const client = useQueryClient();
  // The SITE is in the key: two sites' links are two answers.
  const linkKey = [...INSTALL_LINK_KEY, siteId] as const;
  const link = useQuery({ queryKey: linkKey, queryFn: () => api.getInstallLink(siteId) });
  const [draft, setDraft] = useState<Draft | null>(null);
  const [revealed, setRevealed] = useState<string | null>(null);
  const [copied, setCopied] = useState<"idle" | "done" | "failed">("idle");
  const [confirmRegenerate, setConfirmRegenerate] = useState(false);

  useEffect(() => {
    if (link.data && draft === null) setDraft(draftFrom(link.data));
  }, [link.data, draft]);

  const onSaved = (saved: InstallLinkSaved) => {
    client.setQueryData(linkKey, settingsOf(saved));
    setDraft(draftFrom(saved));
    if (saved.url) {
      setRevealed(saved.url);
      setCopied("idle");
    }
  };
  const save = useMutation({
    mutationFn: (body: InstallLinkUpdate) => api.saveInstallLink(siteId, body),
    onSuccess: onSaved,
  });
  const regenerate = useMutation({
    mutationFn: () => api.regenerateInstallLink(siteId),
    onSuccess: onSaved,
  });

  const noNetwork = draft !== null && draft.enabled && parseNetworks(draft.networks).length === 0;
  const failure = save.error ?? regenerate.error;

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied("done");
    } catch {
      setCopied("failed");
    }
  }

  return (
    <Panel
      title={t("agentReleases.link.title")}
      subtitle={t("agentReleases.link.subtitle")}
    >
      <QueryState
        loading={link.isLoading}
        error={link.error}
        empty={false}
        emptyLabel={t("agentReleases.link.noSettings")}
      >
        {draft && link.data ? (
          <form
            onSubmit={(event) => {
              event.preventDefault();
              save.mutate(bodyFrom(draft));
            }}
          >
            <p className="note">{statusLine(link.data, zone)}</p>
            {link.data.has_code && !revealed ? (
              <p className="note">{t("agentReleases.link.shownOnce")}</p>
            ) : null}
            <Toggle
              checked={draft.enabled}
              onChange={(enabled) => setDraft({ ...draft, enabled })}
              label={t("agentReleases.link.accept")}
            />
            <Field
              label={t("agentReleases.link.networks")}
              help={t("agentReleases.link.networksHelp")}
            >
              <textarea
                className={inputClass}
                rows={4}
                value={draft.networks}
                onChange={(event) => setDraft({ ...draft, networks: event.target.value })}
              />
            </Field>
            <Field
              label={t("agentReleases.link.switchOff")}
              help={t("agentReleases.link.switchOffHelp")}
            >
              <input
                type="datetime-local"
                className={inputClass}
                value={draft.disableAt}
                onChange={(event) => setDraft({ ...draft, disableAt: event.target.value })}
              />
            </Field>
            <Field label={t("agentReleases.link.perHour")} help={t("agentReleases.link.perHourHelp")}>
              <input
                type="number"
                min={1}
                max={500}
                className={inputClass}
                value={draft.hourlyLimit}
                onChange={(event) => setDraft({ ...draft, hourlyLimit: event.target.value })}
              />
            </Field>
            {noNetwork ? (
              <p role="alert" className="form-error">
                {t("agentReleases.link.noNetwork")}
              </p>
            ) : null}
            {failure ? (
              <p role="alert" className="form-error">
                {t("agentReleases.link.notChanged", { detail: describeSubmitError(failure) })}
              </p>
            ) : null}
            <div className="form-actions">
              <Btn
                type="submit"
                variant="primary"
                label={t("agentReleases.link.save")}
                disabled={save.isPending || noNetwork}
              />
              {link.data.has_code ? (
                <Btn
                  label={t("agentReleases.link.regenerate")}
                  onClick={() => setConfirmRegenerate(true)}
                  disabled={regenerate.isPending}
                />
              ) : null}
            </div>
          </form>
        ) : null}
      </QueryState>

      {revealed ? (
        <section className="reveal" role="region" aria-label={t("agentReleases.link.newLinkRegion")}>
          <p>
            <Trans k="agentReleases.link.copyNow" components={{ strong: <strong /> }} />
          </p>
          {/* `.code`, like every other credential shown once on this page: it
              wraps, so a 60-character link does not widen a phone screen. */}
          <pre className="code mt-2">{revealed}</pre>
          <div className="form-actions">
            <Btn label={copied === "done" ? t("agentReleases.link.copied") : t("agentReleases.link.copyLink")} onClick={() => void copy(revealed)} />
            <Btn label={t("agentReleases.link.haveCopied")} onClick={() => setRevealed(null)} />
          </div>
          {copied === "failed" ? (
            <p role="alert" className="form-error">{t("agentReleases.link.copyFailed")}</p>
          ) : null}
        </section>
      ) : null}

      {link.data ? (
        <MsiDownload linkOn={link.data.enabled} revealed={revealed} siteId={siteId} />
      ) : null}

      <ConfirmDialog
        open={confirmRegenerate}
        title={t("agentReleases.link.confirmTitle")}
        subject={t("agentReleases.link.confirmSubject")}
        body={t("agentReleases.link.confirmBody")}
        confirmLabel={t("agentReleases.link.confirmLabel")}
        destructive
        onConfirm={() => {
          setConfirmRegenerate(false);
          regenerate.mutate();
        }}
        onCancel={() => setConfirmRegenerate(false)}
      />
    </Panel>
  );
}
