/**
 * The MSI for Group Policy (GPO MSI spec 2026-09-30 §2.1, §2.4, §2.6).
 *
 * ONE MSI PER ORGANISATION AND LINK, BUILT ON REQUEST. It carries the
 * published agent and this organisation's install link; a computer that
 * installs it joins through the link and waits in Devices → Waiting for
 * review, like any other computer that used the link.
 *
 * THE LINK IS PASTED, BECAUSE THE SERVER DOES NOT HAVE IT. The server keeps
 * only the code's hash, so the MSI can only be built from a link the
 * administrator still holds: the one on screen right after it was generated
 * (pre-filled here), or one they saved. The code goes to the server in a
 * request BODY, never a URL, and is not kept in the query cache.
 *
 * THE SHA-256 IS SHOWN AFTER THE DOWNLOAD, not before: the MSI is built per
 * request, so each download is a different file. The share's permissions are
 * the only integrity control once it is on SYSVOL, and this is the value to
 * check it against once, before it is copied there.
 *
 * THE MSI ITSELF IS NOT KEPT. It holds the link code, and a few megabytes of
 * it have no use once the browser has the file: the mutation hands the Blob to
 * `saveBlob` and returns only the file's name and SHA-256, so neither this
 * component's state nor React Query's mutation cache holds the bytes.
 *
 * INVARIANT 6: rendered for a holder of `enrollment.link.manage`; the route
 * checks the permission, the link, the release and its signature itself.
 */

import { useMutation } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { describeSubmitError } from "../../components/form";
import { Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import { Trans } from "../../i18n/Trans";
import type { InstallLinkMsi, MsiArch } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { saveBlob } from "../../lib/download";
import { Btn, FieldGroup } from "../../shell/components";

const CODE = /^[A-Za-z0-9_-]{32}$/;

/**
 * The code in a pasted install link (`https://<console>/i/<code>`), or a
 * bare code; null for anything else. Cosmetic: the server checks the code
 * against the link's hash and refuses anything else.
 */
export function codeFromLink(text: string): string | null {
  const value = text.trim();
  const at = value.lastIndexOf("/i/");
  const tail = at >= 0 ? value.slice(at + 3).replace(/\/+$/, "") : value;
  return CODE.test(tail) ? tail : null;
}

const DOWNLOADS: { arch: MsiArch; label: MessageKey }[] = [
  { arch: "amd64", label: "agentReleases.msi.downloadAmd64" },
  { arch: "arm64", label: "agentReleases.msi.downloadArm64" },
];

/** What is kept of a download: its name and digest, never the bytes. */
type SavedMsi = Pick<InstallLinkMsi, "filename" | "sha256">;

export function MsiDownload({ linkOn, revealed }: { linkOn: boolean; revealed: string | null }) {
  const t = useT();
  const api = useApi();
  const [link, setLink] = useState(revealed ?? "");
  const [done, setDone] = useState<SavedMsi | null>(null);
  useEffect(() => {
    if (revealed) setLink(revealed);
  }, [revealed]);
  const code = codeFromLink(link);
  const download = useMutation({
    mutationFn: async (arch: MsiArch): Promise<SavedMsi> => {
      const msi = await api.downloadInstallLinkMsi({ code: code ?? "", arch });
      saveBlob(msi.blob, msi.filename);
      return { filename: msi.filename, sha256: msi.sha256 };
    },
    onSuccess: setDone,
  });

  return (
    <FieldGroup title={t("agentReleases.msi.title")} icon="package">
      <p className="note">{t("agentReleases.msi.intro")}</p>
      {!linkOn ? (
        <p className="note">{t("agentReleases.msi.switchOn")}</p>
      ) : (
        <>
          <Field
            label={t("agentReleases.msi.linkLabel")}
            help={t("agentReleases.msi.linkHelp")}
          >
            <input
              className={inputClass}
              value={link}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => setLink(event.target.value)}
            />
          </Field>
          {link.trim() !== "" && code === null ? (
            <p role="alert" className="form-error">
              {t("agentReleases.msi.notALink")}
            </p>
          ) : null}
          <div className="form-actions">
            {DOWNLOADS.map(({ arch, label }) => (
              <Btn
                key={arch}
                label={t(label)}
                icon="download"
                disabled={code === null || download.isPending}
                onClick={() => {
                  setDone(null);
                  download.mutate(arch);
                }}
              />
            ))}
          </div>
          {download.error ? (
            <p role="alert" className="form-error">
              {t("agentReleases.msi.noMsi", { detail: describeSubmitError(download.error) })}
            </p>
          ) : null}
          {done ? (
            <div role="status">
              <p className="note">
                <Trans k="agentReleases.msi.saved" vars={{ filename: done.filename }} components={{ code: <code /> }} />
              </p>
              <pre className="code">{done.sha256}</pre>
            </div>
          ) : null}
          <p className="note">{t("agentReleases.msi.rollout")}</p>
          <p className="note">{t("agentReleases.msi.shareWarning")}</p>
        </>
      )}
    </FieldGroup>
  );
}
