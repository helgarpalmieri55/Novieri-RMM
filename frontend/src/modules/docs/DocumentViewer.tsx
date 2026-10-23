/**
 * The five-kind viewer (`spec-attachments.md` §5.2), and the download control
 * that every branch of it ends in.
 *
 * READ `viewer.ts` FIRST. It is the wall; this file is what stands on it. The
 * only thing here that decides how bytes are drawn is `resolveViewer`, and the
 * only MIME any `Blob` below carries is the one that function returned. There
 * is no branch keyed on a filename, an extension, a response header, or a
 * guess.
 *
 * EVERY VIEW IS AN AUDITED DOWNLOAD (§12.3, D10). The server cannot tell
 * viewing from saving -- the bytes left the control plane either way, and the
 * client could always keep them -- so opening this panel writes
 * `docs.document_downloaded` exactly as pressing Download does. That is stated
 * on screen rather than hidden, and NOTHING HERE MAY BE BUILT AS A "preview
 * without downloading" AFFORDANCE, because there is no such operation behind
 * the API and offering one would be the console claiming a distinction the
 * audit log cannot verify.
 *
 * NO PRESIGNED URL, NO `<a href>` TO THE API (§0.3, `lib/download.ts`). The
 * bytes arrive through the authenticated client and become an object URL in
 * this tab, or text in this tab, and nothing else. A presigned GET would be an
 * unauthenticated same-origin URL for the whole tenant, because MinIO is
 * routed on the console's own hostname; a plain `<a href="/api/v1/…">` would
 * send no `Authorization` header and save the 401 body under the file's name.
 */

import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect } from "react";

import { Button } from "../../components/ui";
import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import { Trans } from "../../i18n/Trans";
import type { DocDocumentVersion } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { saveBlob } from "../../lib/download";
import { formatBytes } from "../../lib/format";
import { renderMarkdown } from "./markdown";
import { blobTypeFor, isPreviewable, resolveViewer } from "./viewer";

/**
 * Save a version to disk, on the caller's own token.
 *
 * IT HAS AN ERROR CHANNEL AND THAT IS THE POINT (§11.7). The shipped
 * `downloadError` state on the ticket attachment panel exists because
 * `void download(...)` once had no rejection path, and a failed download
 * looked exactly like no click at all. A 503 from a down object store, a 410
 * from a purged version and a 403 from a revoked permission all arrive here,
 * and all three are sentences the server wrote.
 */
export function DownloadVersionButton({
  documentId,
  version,
  label,
  variant = "outline",
}: {
  documentId: string;
  version: DocDocumentVersion;
  label?: string;
  variant?: "default" | "outline" | "ghost";
}) {
  const t = useT();
  const api = useApi();
  const shownLabel = label ?? t("docs.viewer.download");
  const download = useMutation({
    mutationFn: async () => {
      const blob = await api.downloadDocumentVersion(documentId, version.version);
      // `saveBlob`, not an anchor at the API URL: the browser navigates to an
      // `<a download>` itself and sends no bearer token, so the route answers
      // 401 and the browser cheerfully saves the refusal under the file's name.
      saveBlob(blob, version.filename);
    },
  });

  return (
    <>
      <Button
        type="button"
        variant={variant}
        onClick={() => download.mutate()}
        disabled={download.isPending || version.purged_at != null}
      >
        {download.isPending ? `${shownLabel}…` : shownLabel}
      </Button>
      {download.isError && (
        <p role="alert" className="notice red mt-2">
          <span>
            {t("docs.viewer.notDownloaded", {
              filename: version.filename,
              reason: describeSubmitError(download.error),
            })}
          </span>
        </p>
      )}
    </>
  );
}

/** The line under every previewable panel. It says what actually happened. */
function AuditNote() {
  const t = useT();
  return <p className="note mt-2">{t("docs.viewer.auditNote")}</p>;
}

export function DocumentViewer({
  documentId,
  version,
}: {
  documentId: string;
  version: DocDocumentVersion;
}) {
  const t = useT();
  const api = useApi();
  // THE ONE DECISION. `version.viewer` is the server's sniff of the leading
  // bytes and `version.content_type` selects a row inside the image branch;
  // neither is used to construct a MIME, and nothing else on the version row
  // is consulted at all.
  const rule = resolveViewer(version.viewer, version.content_type);
  const purged = version.purged_at != null;
  const previewable = isPreviewable(rule) && !purged;

  const bytes = useQuery({
    queryKey: ["doc-document-bytes", documentId, version.version],
    queryFn: async () => {
      const blob = await api.downloadDocumentVersion(documentId, version.version);
      const type = blobTypeFor(rule);
      // NULL MEANS NO BLOB IS BUILT AT ALL. The two text branches decode UTF-8
      // and hand a string to React, so there is no object URL, no MIME and
      // nothing for a browser to navigate to -- which is exactly why HTML,
      // SVG and XML are safe here.
      if (type === null) return { url: null, text: await blob.text() };
      // A NEW `Blob` CARRYING THIS CONSOLE'S OWN TYPE. The response's type is
      // always `application/octet-stream` (§5.1 wall 1) and is deliberately
      // discarded: re-using the response Blob would make the wire decide what
      // the browser renders, which is the mistake the three walls exist to
      // make impossible.
      return { url: URL.createObjectURL(new Blob([await blob.arrayBuffer()], { type })), text: null };
    },
    enabled: previewable,
    // The bytes do not change: a version is immutable and a purge is a 410,
    // not a different body. Refetching would write a second audit event for
    // no new fact.
    staleTime: Infinity,
    // DROPPED THE MOMENT NOTHING IS RENDERING IT, because the effect below
    // revokes the object URL on unmount -- a cached entry outliving its URL
    // would be handed back to a remount as a dead `blob:` reference.
    gcTime: 0,
    retry: false,
  });

  const url = bytes.data?.url ?? null;
  useEffect(() => {
    // An object URL pins its blob in memory for the life of the document.
    // Browsing a library of 25 MiB scans without this accumulates every one.
    return () => {
      if (url) URL.revokeObjectURL(url);
    };
  }, [url]);

  if (purged) {
    // A TOMBSTONE, NOT A MISSING FILE (§6.3). The row survives on purpose:
    // filename, size and digest say what was destroyed, and the audit log says
    // by whom. "Deliberately destroyed" and "never existed" are different
    // facts and the console must not flatten them into one.
    return (
      <div role="status" className="notice gold">
        <span>
          <Trans
            k="docs.viewer.destroyed"
            vars={{
              version: version.version,
              filename: version.filename,
              size: formatBytes(version.byte_size) || `${version.byte_size} B`,
            }}
            components={{ b: <strong /> }}
          />
        </span>
      </div>
    );
  }

  if (!previewable) {
    // §5.2's last row, and the point of the table: an unknown kind degrades to
    // download, it does not degrade to guessing.
    return (
      <div>
        <p className="note">
          {t("docs.viewer.noPreview", { type: version.content_type, filename: version.filename })}
        </p>
        <div className="mt-3">
          <DownloadVersionButton documentId={documentId} version={version} />
        </div>
      </div>
    );
  }

  if (bytes.isPending) {
    return <p className="note">{t("docs.viewer.loading", { filename: version.filename })}</p>;
  }

  if (bytes.isError) {
    // §0.9/F8b: object storage is a SOFT dependency, so the metadata above
    // this panel is intact and the failure belongs to the bytes alone. The
    // server's own 503 sentence names the dependency; a spinner that never
    // resolves would tell an operator their library is gone.
    return (
      <div>
        <p role="alert" className="notice red">
          <span>
            {t("docs.viewer.readFailed", {
              filename: version.filename,
              reason: describeSubmitError(bytes.error),
            })}
          </span>
        </p>
        <div className="mt-3">
          <DownloadVersionButton documentId={documentId} version={version} label={t("docs.viewer.tryAgain")} />
        </div>
      </div>
    );
  }

  if (rule.render === "embed" && url) {
    return (
      <div>
        {/* `<embed>`, NOT `<iframe sandbox>` -- §5.2 and `viewer.ts`. A blob
            URL carries its creating origin, so a sandbox without
            `allow-same-origin` gets an opaque one that Chrome refuses to load
            a blob into at all, and with `allow-same-origin` the attribute
            isolates nothing. What does the isolating is the browser's own PDF
            viewer, which runs in a separate process and cannot touch this
            DOM. `type` comes from the render map, never from the wire. */}
        <embed
          type={rule.blobType ?? undefined}
          src={url}
          title={version.filename}
          className="w-full"
          style={{ height: 620, border: "1px solid var(--line)", borderRadius: "var(--r-ctl)" }}
        />
        <p className="note mt-2">{t("docs.viewer.embedHint")}</p>
        <div className="mt-2">
          <DownloadVersionButton documentId={documentId} version={version} />
        </div>
        <AuditNote />
      </div>
    );
  }

  if (rule.render === "image" && url) {
    return (
      <div>
        {/* Scripts and external references are disabled for content loaded
            through `<img>`, and the four raster types the map allows cannot
            execute in that context. */}
        <img
          src={url}
          alt={version.filename}
          style={{ maxWidth: "100%", maxHeight: 620, border: "1px solid var(--line)", borderRadius: "var(--r-ctl)" }}
        />
        <div className="mt-3">
          <DownloadVersionButton documentId={documentId} version={version} />
        </div>
        <AuditNote />
      </div>
    );
  }

  if (rule.render === "markdown" && bytes.data?.text != null) {
    return (
      <div>
        {/* THE SHIPPED SANITIZER, UNCHANGED AND UN-FORKED (F3e). `markdown.ts`
            rebuilds the DOM against an allow-list, and its own tests are this
            branch's coverage. A second sanitizer here would be a second
            allow-list to drift from the first. */}
        <div
          className="overflow-x-auto"
          dangerouslySetInnerHTML={{ __html: renderMarkdown(bytes.data.text) }}
        />
        <div className="mt-3">
          <DownloadVersionButton documentId={documentId} version={version} />
        </div>
        <AuditNote />
      </div>
    );
  }

  if (rule.render === "text" && bytes.data?.text != null) {
    return (
      <div>
        {/* SOURCE TEXT, AND NOTHING ELSE (F3d). The string is a React CHILD,
            which React commits as a text node -- i.e. `textContent`. There is
            no `innerHTML` and no `dangerouslySetInnerHTML` on this branch, and
            there must never be one: this is where an uploaded `.html`, `.svg`
            or `.xml` lands, because `filetypes.py` stores all three as
            `text/plain` with `viewer="text"`. `<script>alert(1)</script>` in
            an uploaded file renders as those twenty-four characters. */}
        <pre
          className="code"
          style={{ maxHeight: 620, overflow: "auto", whiteSpace: "pre-wrap", wordBreak: "break-word" }}
          data-testid="document-source"
        >
          {bytes.data.text}
        </pre>
        <div className="mt-3">
          <DownloadVersionButton documentId={documentId} version={version} />
        </div>
        <AuditNote />
      </div>
    );
  }

  // Unreachable in practice -- `previewable` already excluded the download
  // branch and each branch above pairs its render with the payload it needs.
  // Present so a future kind added to the map without a branch here degrades
  // to download rather than to a blank panel.
  return (
    <div>
      <p className="note">
        {t("docs.viewer.noPreviewShort", { type: version.content_type, filename: version.filename })}
      </p>
      <div className="mt-3">
        <DownloadVersionButton documentId={documentId} version={version} />
      </div>
    </div>
  );
}
