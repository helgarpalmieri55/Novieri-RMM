/**
 * The Documents half of `/docs` (`spec-attachments.md` §11).
 *
 * The library gains a file. Everything else on the page is unchanged: same
 * route, same search box, same category grouping, and the same left-column /
 * main-pane split articles already use.
 *
 * FOUR THINGS IN THIS FILE ARE NOT COSMETIC, so they are named here rather
 * than discovered:
 *
 *  1. **A document exists before its file does** (§4.1). `current_version` 0
 *     is legal and renders as "No file uploaded yet" with an Upload control,
 *     not as an error. The two-step create-then-upload shape is forced: there
 *     is no multipart parser in this project, so metadata and bytes cannot
 *     share a request.
 *  2. **Purge is per version and the control follows the version** (§11.3).
 *     There is no "purge this document" button, because there is no operation
 *     behind one — a document is a row and a name, and destroying its contents
 *     one version at a time is the honest shape of it.
 *  3. **Every refusal is a sentence on the screen** (§11.7). Filename
 *     rejected (400), too large (413), type not accepted (415, with the
 *     server's own accepted list), quota reached (507), document archived
 *     (409) — five distinct causes, and the server wrote a sentence for each,
 *     so `describeSubmitError` passes them through rather than inventing one.
 *  4. **The object store is a SOFT dependency** (§0.9). With MinIO down this
 *     page still lists everything from PostgreSQL and fails loudly on the byte
 *     paths only. A metadata list rendered as an empty list would tell an
 *     operator their library is gone.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import { ConfirmDialog } from "../../components/confirm";
import { attemptProblems, describeSubmitError, Form, FormActions } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Badge, Button, Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { DocDocument, DocDocumentListItem, DocDocumentVersion } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDateTime } from "../../lib/datetime";
import { formatBytes } from "../../lib/format";
import { humaniseEnum } from "../../lib/labels";
import { Panel, QueryState, StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { DocumentViewer, DownloadVersionButton } from "./DocumentViewer";
import { RowButton } from "./RowButton";

/** "1 link", never "1 link(s)": the console resolves its own plurals. */
function linksLine(count: number): string {
  return translate("docs.documents.links", { count });
}

/** The server's own bounds (`documents_api.py::DocumentCreate`). */
const TITLE_MAX = 300;
const CATEGORY_MAX = 100;

/**
 * `documents.MAX_DOCUMENT_BYTES`, mirrored so a 30 MiB file is refused before
 * it is uploaded rather than after.
 *
 * THIS IS A COURTESY, NOT A CONTROL, and the difference matters enough to
 * write down. The enforcement is `reject_oversized_content_length` and then
 * `read_bounded_body` on the server, in that order, and neither can be routed
 * around by a client that lies about `Content-Length` or sends a chunked body.
 * What this constant buys is that an operator on a slow link learns the answer
 * before spending four minutes on the upload.
 */
const MAX_DOCUMENT_BYTES = 25 * 1024 * 1024;

/**
 * The link vocabulary, from the SHIPPED `_LINK_TARGET_MODELS`
 * (`documentation/service.py`) — the same dictionary articles resolve
 * through, deliberately (§0.7).
 *
 * A second vocabulary tuned to documents is the trap the shipped code already
 * carries a comment about: the dict and the route's regex have to move
 * together or you get a 404 on one side and a 422 on the other. Two
 * vocabularies would be that trap permanently, in two modules that share a
 * page.
 *
 * `ticket` IS DELIBERATELY ABSENT. Tickets have attachments, with their own
 * terminal-status freeze and their own portal exposure rules; a second
 * file-attachment path onto the same object would have to answer both of those
 * questions again, differently (§15.5).
 */
export type DocLinkTarget =
  | "device"
  | "device_group"
  | "credential"
  | "record"
  | "ipam_subnet"
  | "ipam_address";

const LINK_TARGET_TYPES: readonly DocLinkTarget[] = [
  "device",
  "device_group",
  "credential",
  "record",
  "ipam_subnet",
  "ipam_address",
];

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The server's length bound as a field rule's check, worded when the rule is read. */
export const tooLong = (label: string, max: number) => (value: string) =>
  value.length > max ? translate("docs.validation.tooLong", { label, max }) : null;

/** The rules of the two forms that carry a title and a category. */
function titleCategoryRules(): FieldRule[] {
  return [
    {
      name: "title",
      label: translate("docs.documents.dialog.title"),
      validate: tooLong(translate("docs.documents.dialog.title"), TITLE_MAX),
    },
    {
      name: "category",
      label: translate("docs.documents.dialog.category"),
      validate: tooLong(translate("docs.documents.dialog.category"), CATEGORY_MAX),
    },
  ];
}

/**
 * A chosen file as a snapshot value. A refused submission is held as data (the
 * rules and what they were checked against), and a rule must not read the
 * component, so the file travels as the two facts the rule checks. `none` is
 * not empty, so the rule words "choose a file" itself instead of the generic
 * "is required"; the terminator keeps a filename's own edge spaces.
 */
const FILE_NONE = "none";
function fileSnapshot(file: File | null): string {
  return file ? `${file.size}:${file.name}\u0001` : FILE_NONE;
}

/**
 * What a file input has to say before anything is sent, and the one client-side
 * refusal (§11.7). Everything else is the server's sentence.
 */
function fileRule(): FieldRule {
  return {
    name: "file",
    label: translate("docs.documents.dialog.file"),
    validate: (value) => {
      if (value === FILE_NONE) return translate("docs.validation.chooseFile");
      const colon = value.indexOf(":");
      const size = Number(value.slice(0, colon));
      const name = value.slice(colon + 1, -1);
      if (size > MAX_DOCUMENT_BYTES) {
        return translate("docs.validation.fileTooBig", {
          name,
          size: sizeLabel(size),
          limit: sizeLabel(MAX_DOCUMENT_BYTES),
        });
      }
      if (size === 0) return translate("docs.validation.fileEmpty", { name });
      return null;
    },
  };
}

/** A version's size, or the raw count when `formatBytes` declines to guess. */
export function sizeLabel(bytes: number): string {
  return formatBytes(bytes) || `${bytes} B`;
}

/**
 * A short badge for a stored type. The SERVER'S sniffed `content_type` is the
 * only input — never the filename, never its extension.
 *
 * This is a LABEL and nothing branches on it. The one place a type decides
 * behaviour is `viewer.ts`, off `viewer`.
 */
export function typeLabel(contentType: string): string {
  const known: Record<string, string> = {
    "application/pdf": "PDF",
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/gif": "GIF",
    "image/webp": "WebP",
    "application/zip": "ZIP",
    "text/markdown": "Markdown",
    "text/csv": "CSV",
    "text/plain": translate("docs.documents.typeText"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Excel",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PowerPoint",
    "application/vnd.ms-visio.drawing": "Visio",
    "application/msword": "Word",
    "application/vnd.ms-excel": "Excel",
    "application/vnd.ms-powerpoint": "PowerPoint",
    "application/vnd.visio": "Visio",
    "application/x-ole-storage": "Office",
  };
  return known[contentType] ?? contentType;
}

/* ------------------------------------------------------------ the library */

/**
 * The left column's third section (§11.2), grouped by category exactly as
 * Articles is, so the two group under the same headings on the same page.
 */
export function DocumentsSection({
  documents,
  selectedId,
  onSelect,
  canManage,
  onUpload,
}: {
  documents: ReturnType<typeof useQuery<{ items: DocDocumentListItem[]; total: number }>>;
  selectedId: string | null;
  onSelect: (id: string) => void;
  canManage: boolean;
  onUpload: () => void;
}) {
  const t = useT();
  const byCategory = useMemo(() => {
    const map = new Map<string, DocDocumentListItem[]>();
    for (const item of documents.data?.items ?? []) {
      map.set(item.category, [...(map.get(item.category) ?? []), item]);
    }
    return [...map.entries()];
  }, [documents.data]);

  return (
    <div id="documents">
      <Panel
        title={t("docs.documents.title")}
        actions={
          canManage && (
            <Button variant="outline" onClick={onUpload}>
              <Icon name="upload" size={14} />
              {t("docs.documents.upload")}
            </Button>
          )
        }
      >
        <QueryState
          loading={documents.isLoading}
          error={documents.error}
          empty={byCategory.length === 0}
          emptyLabel={t("docs.documents.empty")}
          emptyIcon="file-text"
        >
          {/* v3's mini list, grouped under the same category captions the
              article list used to carry. The row is a list OPTION rather
              than a `Btn`: it selects what the pane beside it shows. */}
          {byCategory.map(([category, items]) => (
            <div key={category} className="mini-list">
              {/* THE KIT'S CAPTION RUNG, not muted body text. `.k` (10.5px,
                  uppercase, letter-spaced) is what the design gives a group
                  heading over a list; `note` is the size of the rows it sits
                  over, so the group read as another line of the list rather
                  than as its caption -- and the articles table beside it
                  captions its own groups properly. */}
              {/* SPACING INLINE, NOT `mt-3`: `shell/base.css` resets
                  `p { margin: 0 }` unlayered and Tailwind's utilities are
                  layered, so a margin class on a paragraph computes to 0. */}
              <p className="k" style={{ margin: "14px 0 4px" }}>
                {category}
              </p>
              {items.map((item) => (
                <div
                  key={item.id}
                  className="mini-row"
                  style={item.id === selectedId ? { background: "var(--surface-2)" } : undefined}
                >
                  <span className="mini-icon">
                    <Icon name="file-text" size={18} />
                  </span>
                  <RowButton selected={item.id === selectedId} onClick={() => onSelect(item.id)}>
                    {item.title}
                    {/* A ZERO-VERSION DOCUMENT SAYS SO IN THE LIST. It is a
                        legal state (§4.1) and the row that carries no file
                        must not read the same as one that does. */}
                    <span className="cell-sub">
                      {" "}
                      {item.current_version === 0 ? t("docs.documents.noFileYet") : `v${item.current_version}`}
                      {item.link_count > 0 ? ` · ${linksLine(item.link_count)}` : ""}
                    </span>
                  </RowButton>
                </div>
              ))}
            </div>
          ))}
        </QueryState>
      </Panel>
    </div>
  );
}

/* ------------------------------------------------------- the document view */

export function DocumentView({
  documentId,
  canManage,
  canPurge,
  zone,
  onCleared,
}: {
  documentId: string;
  canManage: boolean;
  canPurge: boolean;
  zone: string | null;
  /** Called when the document leaves the page — archived. */
  onCleared: () => void;
}) {
  const t = useT();
  const api = useApi();
  const queryClient = useQueryClient();
  const [showVersions, setShowVersions] = useState(false);
  const [showLinks, setShowLinks] = useState(false);
  const [editing, setEditing] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [confirmingArchive, setConfirmingArchive] = useState(false);
  const [purging, setPurging] = useState<DocDocumentVersion | null>(null);
  // WHICH version the body is showing. `null` means the current one. Selecting
  // an older version from the History drawer moves the viewer, and the Purge
  // control in the header follows it — §11.3's "the control follows the
  // version".
  const [viewingVersion, setViewingVersion] = useState<number | null>(null);

  const document = useQuery({
    queryKey: ["doc-document", documentId],
    queryFn: () => api.getDocument(documentId),
  });
  const versions = useQuery({
    queryKey: ["doc-document-versions", documentId],
    queryFn: () => api.documentVersions(documentId),
    enabled: showVersions || viewingVersion != null,
  });

  // A refusal, a drawer and a selected version all describe ONE document.
  // Moving to another must carry none of them across.
  useEffect(() => {
    setShowVersions(false);
    setShowLinks(false);
    setEditing(false);
    setViewingVersion(null);
  }, [documentId]);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["doc-documents"] });
    void queryClient.invalidateQueries({ queryKey: ["doc-document", documentId] });
    void queryClient.invalidateQueries({ queryKey: ["doc-document-versions", documentId] });
    void queryClient.invalidateQueries({ queryKey: ["doc-document-bytes", documentId] });
  };

  const archive = useMutation({
    mutationFn: () => api.archiveDocument(documentId),
    onSuccess: () => {
      invalidate();
      onCleared();
    },
  });

  const purge = useMutation({
    mutationFn: (version: number) => api.purgeDocumentVersion(documentId, version),
    onSuccess: () => invalidate(),
  });

  if (document.isPending) {
    return (
      <Panel>
        <p className="note">{t("docs.documents.loading")}</p>
      </Panel>
    );
  }
  if (document.isError) {
    return (
      <Panel>
        <p role="alert" className="notice red">
          <span>
            {t("docs.documents.loadFailed", { reason: describeSubmitError(document.error) })}
          </span>
        </p>
      </Panel>
    );
  }

  const doc: DocDocument = document.data;
  const shown =
    viewingVersion == null
      ? doc.current
      : (versions.data ?? []).find((v) => v.version === viewingVersion) ?? doc.current;

  return (
    /* The kit's panel, written out: the head carries the title, the pills and
       the version line, and the action row; `Panel`'s own `title` takes a
       string and this head is three things. */
    <section className="panel">
      <div className="panel-h">
        <div>
          <h2 className="panel-title">{doc.title}</h2>
          <p className="flex flex-wrap items-center gap-2">
            <Badge variant="muted">{doc.category}</Badge>
            {doc.archived && <StatusPill value="archived" kind="warn" label={t("docs.documents.archivedPill")} />}
            <span className="mono">
              {doc.current_version === 0 ? t("docs.documents.noFileYet") : `v${doc.current_version}`}
            </span>
            {shown && (
              <span>
                {shown.filename} · {sizeLabel(shown.byte_size)} · {typeLabel(shown.content_type)}
              </span>
            )}
          </p>
        </div>
        <div className="panel-a" style={{ flexWrap: "wrap" }}>
          {shown && shown.purged_at == null && (
            <DownloadVersionButton documentId={doc.id} version={shown} variant="ghost" />
          )}
          <Button variant="ghost" onClick={() => setShowVersions(!showVersions)}>
            {t("docs.documents.history")}
          </Button>
          <Button variant="ghost" onClick={() => setShowLinks(!showLinks)}>
            {t("docs.documents.linksButton")}
          </Button>
          {canManage && (
            <>
              <Button variant="outline" onClick={() => setUploading(true)} disabled={doc.archived}>
                <Icon name="upload" size={14} />
                {t("docs.documents.uploadNewVersion")}
              </Button>
              <Button variant="outline" onClick={() => setEditing(true)}>
                <Icon name="edit" size={14} />
                {t("docs.documents.editDetails")}
              </Button>
              {!doc.archived && (
                <Button
                  variant="outline"
                  className="danger"
                  onClick={() => setConfirmingArchive(true)}
                  disabled={archive.isPending}
                >
                  {t("docs.documents.archive")}
                </Button>
              )}
            </>
          )}
          {/* PURGE ACTS ON THE VERSION THE BODY IS SHOWING, and the dialog
              names it. Every other version is purged from its own row in the
              History drawer, which is why there is no document-level purge
              here. */}
          {canPurge && shown && shown.purged_at == null && (
            <Button variant="outline" className="danger" onClick={() => setPurging(shown)}>
              {t("docs.documents.purgeVersion", { version: shown.version })}
            </Button>
          )}
        </div>
      </div>
      <div className="panel-b">

      {doc.description && <p className="note mb-3">{doc.description}</p>}

      {doc.archived && (
        <p role="status" className="notice gold mb-3">
          <span>{t("docs.documents.archivedNotice")}</span>
        </p>
      )}

      {archive.isError && (
        <p role="alert" className="notice red mb-3">
          <span>{t("docs.documents.archiveFailed", { reason: describeSubmitError(archive.error) })}</span>
        </p>
      )}
      {purge.isError && (
        <p role="alert" className="notice red mb-3">
          <span>{t("docs.documents.purgeFailed", { reason: describeSubmitError(purge.error) })}</span>
        </p>
      )}

      {shown == null ? (
        // §4.1: a document with no versions is a legal, visible state, not an
        // error. Deploy's package-before-artifact shape, and the same wording.
        <div>
          <p className="note">{t("docs.documents.noFile")}</p>
          {canManage && !doc.archived && (
            <div className="mt-3">
              <Button onClick={() => setUploading(true)}>{t("docs.documents.uploadFile")}</Button>
            </div>
          )}
        </div>
      ) : (
        <DocumentViewer key={`${doc.id}:${shown.version}`} documentId={doc.id} version={shown} />
      )}

      {showVersions && (
        <VersionHistory
          documentId={doc.id}
          versions={versions}
          zone={zone}
          canPurge={canPurge}
          currentVersion={doc.current_version}
          viewingVersion={viewingVersion ?? doc.current_version}
          onView={setViewingVersion}
          onPurge={setPurging}
        />
      )}

      {showLinks && <DocumentLinks doc={doc} canManage={canManage} onChanged={invalidate} />}

      <ConfirmDialog
        open={confirmingArchive}
        title={t("docs.documents.archiveTitle")}
        subject={doc.title}
        body={t("docs.documents.archiveBody")}
        confirmLabel={t("docs.documents.archiveConfirm")}
        destructive
        onCancel={() => setConfirmingArchive(false)}
        onConfirm={() => {
          setConfirmingArchive(false);
          archive.mutate();
        }}
      />

      {/* THE ONE IRREVERSIBLE ACT IN THIS MODULE (§6.3). The body says what is
          destroyed and what survives, because both are true and the second is
          the reason a tombstone exists at all. `requireTyping` because a
          misclick here is unrecoverable and there is no restore path anywhere
          in this platform. */}
      <ConfirmDialog
        open={purging != null}
        title={t("docs.documents.purgeTitle")}
        subject={
          purging
            ? t("docs.documents.purgeSubject", {
                version: purging.version,
                filename: purging.filename,
                size: sizeLabel(purging.byte_size),
              })
            : ""
        }
        body={t("docs.documents.purgeBody")}
        confirmLabel={t("docs.documents.purgeConfirm")}
        destructive
        requireTyping
        onCancel={() => setPurging(null)}
        onConfirm={() => {
          const target = purging;
          setPurging(null);
          if (target) purge.mutate(target.version);
        }}
      />

      <UploadVersionDialog
        open={uploading}
        documentId={doc.id}
        onClose={() => setUploading(false)}
        onUploaded={() => {
          setUploading(false);
          setViewingVersion(null);
          invalidate();
        }}
      />

      <EditDocumentDialog
        open={editing}
        doc={doc}
        onClose={() => setEditing(false)}
        onSaved={() => {
          setEditing(false);
          invalidate();
        }}
      />
      </div>
    </section>
  );
}

/* ------------------------------------------------------ the version drawer */

function VersionHistory({
  documentId,
  versions,
  zone,
  canPurge,
  currentVersion,
  viewingVersion,
  onView,
  onPurge,
}: {
  documentId: string;
  versions: ReturnType<typeof useQuery<DocDocumentVersion[]>>;
  zone: string | null;
  canPurge: boolean;
  currentVersion: number;
  viewingVersion: number;
  onView: (version: number | null) => void;
  onPurge: (version: DocDocumentVersion) => void;
}) {
  const t = useT();
  return (
    <section className="mt-4" style={{ borderTop: "1px solid var(--line)", paddingTop: 12 }}>
      <h3 className="panel-title mb-2">{t("docs.documents.history")}</h3>
      {/* A FAILED HISTORY FETCH MUST NOT RENDER AS NO HISTORY. `(data ?? [])`
          over an errored query produces an empty list, which on an
          audit-adjacent surface claims "this document has one version" — a
          claim the page has no evidence for. The shipped article drawer
          learned this; the same three outcomes are distinct here. */}
      {versions.isError ? (
        <p role="alert" className="notice red">
          <span>{t("docs.documents.versions.failed", { reason: describeSubmitError(versions.error) })}</span>
        </p>
      ) : versions.isPending ? (
        <p className="note">{t("docs.documents.loading")}</p>
      ) : versions.data.length === 0 ? (
        <p className="note">{t("docs.documents.versions.none")}</p>
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>{t("docs.documents.versions.version")}</Th>
              <Th>{t("docs.documents.versions.file")}</Th>
              <Th>{t("docs.documents.versions.size")}</Th>
              <Th>{t("docs.documents.versions.type")}</Th>
              <Th>{t("docs.documents.versions.uploaded")}</Th>
              <Th />
            </tr>
          </thead>
          <tbody>
            {versions.data.map((version) => (
              <tr key={version.version}>
                <Td>
                  <span className="mono">v{version.version}</span>
                  {version.version === currentVersion && (
                    <Badge variant="info" className="ml-2">
                      {t("docs.documents.versions.current")}
                    </Badge>
                  )}
                  {version.version === viewingVersion && (
                    <span className="cell-sub ml-2">{t("docs.documents.versions.shown")}</span>
                  )}
                </Td>
                <Td>{version.filename}</Td>
                <Td>
                  <span className="mono">{sizeLabel(version.byte_size)}</span>
                </Td>
                <Td>{typeLabel(version.content_type)}</Td>
                {/* THE OPERATOR'S PROFILE ZONE, not the browser's. A version
                    history is where a wrong zone is least visible and most
                    expensive: "was this before or after the outage" is
                    answered by these lines. */}
                <Td>
                  <span className="mono" style={{ whiteSpace: "nowrap" }}>
                    {formatDateTime(version.created_at, zone)}
                  </span>
                </Td>
                <Td>
                  {version.purged_at != null ? (
                    // A TOMBSTONE ROW. It stays in the list on purpose: a
                    // destroyed file leaves a mark, and that is the point.
                    <span className="cell-sub">
                      {t("docs.documents.versions.purged", { date: formatDateTime(version.purged_at, zone) })}
                    </span>
                  ) : (
                    <span className="flex flex-wrap gap-2">
                      <Button
                        variant="outline"
                        className="small"
                        onClick={() =>
                          onView(version.version === currentVersion ? null : version.version)
                        }
                      >
                        {t("docs.documents.versions.view")}
                      </Button>
                      <DownloadVersionButton
                        documentId={documentId}
                        version={version}
                        variant="outline"
                      />
                      {canPurge && (
                        <Button
                          variant="outline"
                          className="danger small"
                          onClick={() => onPurge(version)}
                        >
                          {t("docs.documents.versions.purge")}
                        </Button>
                      )}
                    </span>
                  )}
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </section>
  );
}

/* ------------------------------------------------------- the links editor */

/** The link form's one rule, worded when read. */
function targetRules(): FieldRule[] {
  return [
    {
      name: "target_id",
      label: translate("docs.documents.linkEditor.targetId"),
      validate: (value) =>
        UUID_PATTERN.test(value) ? null : translate("docs.validation.targetNotUuid"),
    },
  ];
}

function DocumentLinks({
  doc,
  canManage,
  onChanged,
}: {
  doc: DocDocument;
  canManage: boolean;
  onChanged: () => void;
}) {
  const t = useT();
  const api = useApi();
  const [targetType, setTargetType] = useState<string>(LINK_TARGET_TYPES[0] ?? "device");
  const [targetId, setTargetId] = useState("");
  // The refused submission as data; worded at render, so a switch re-words it.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  const add = useMutation({
    mutationFn: () => api.addDocumentLink(doc.id, { target_type: targetType, target_id: targetId }),
    onSuccess: () => {
      setTargetId("");
      onChanged();
    },
  });
  const remove = useMutation({
    mutationFn: (linkId: string) => api.removeDocumentLink(doc.id, linkId),
    onSuccess: onChanged,
  });

  const submit = () => {
    const next: Attempt = { rules: targetRules, values: { target_id: targetId } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    add.mutate();
  };

  return (
    <section className="mt-4" style={{ borderTop: "1px solid var(--line)", paddingTop: 12 }}>
      <h3 className="panel-title mb-2">{t("docs.documents.linkEditor.title")}</h3>
      {doc.links.length === 0 ? (
        <p className="note">{t("docs.documents.linkEditor.none")}</p>
      ) : (
        <ul className="mini-list" style={{ listStyle: "none", margin: 0, padding: 0 }}>
          {doc.links.map((link) => (
            <li key={link.id} className="mini-row">
              <Badge variant="muted">{humaniseEnum(link.target_type, "DocLinkTarget")}</Badge>
              <span className="mono">{link.target_id}</span>
              {canManage && (
                <Button
                  variant="ghost"
                  className="small"
                  onClick={() => remove.mutate(link.id)}
                  disabled={remove.isPending}
                >
                  {t("docs.documents.linkEditor.unlink")}
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
      {remove.isError && (
        <p role="alert" className="notice red mt-2">
          <span>{t("docs.documents.linkEditor.unlinkFailed", { reason: describeSubmitError(remove.error) })}</span>
        </p>
      )}
      {canManage && (
        <Form onSubmit={submit} className="mt-3">
          <div className="flex flex-wrap items-end gap-2">
            <label className="field" style={{ marginBottom: 0 }}>
              <span>{t("docs.documents.linkEditor.linkTo")}</span>
              <select
                className="select-field"
                value={targetType}
                onChange={(event) => {
                  setTargetType(event.target.value);
                  if (add.isError) add.reset();
                }}
              >
                {LINK_TARGET_TYPES.map((type) => (
                  <option key={type} value={type}>
                    {humaniseEnum(type, "DocLinkTarget")}
                  </option>
                ))}
              </select>
            </label>
            <label className="field" style={{ marginBottom: 0 }}>
              <span>{t("docs.documents.linkEditor.targetId")}</span>
              <input
                className={inputClass}
                value={targetId}
                placeholder="00000000-0000-0000-0000-000000000000"
                onChange={(event) => {
                  setTargetId(event.target.value);
                  if (add.isError) add.reset();
                  if (attempt) setAttempt(null);
                }}
              />
            </label>
            <FormActions
              label={t("docs.documents.linkEditor.link")}
              pending={add.isPending}
              error={add.isError ? add.error : undefined}
              problems={attemptProblems(attempt)}
              failedMessage={t("docs.documents.linkEditor.linkFailed")}
            />
          </div>
          {/* A CREDENTIAL LINK IS METADATA AND NEVER A SECRET (invariant 3).
              Nothing in this module reads the vault, and saying so here is
              cheaper than someone assuming the opposite. */}
          <p className="note mt-2">{t("docs.documents.linkEditor.note")}</p>
        </Form>
      )}
    </section>
  );
}

/* -------------------------------------------------------------- the dialogs */

/**
 * The accepted list, in the console, before an upload rather than after a 415.
 *
 * PHRASED AS WHAT THE VIEWER CAN BE ASKED TO RENDER, never as a malware
 * control (§4.3). ZIP is on the list, so arbitrary bytes can be stored by
 * zipping them — deliberately, as the escape hatch for the binaries the sniffer
 * genuinely refuses. What makes an uploaded file inert lives elsewhere: no
 * agent can resolve a documents key, no job type references one, and no
 * presigned URL exists, so this platform never hands an uploaded file to an
 * endpoint.
 */
function AcceptedTypes() {
  const t = useT();
  return <p className="note">{t("docs.documents.dialog.accepted")}</p>;
}

/**
 * Create a document AND upload its first file (§4.1's two steps, one dialog).
 *
 * THE TWO STEPS ARE VISIBLE WHEN THE SECOND ONE FAILS, and that is deliberate
 * rather than a rough edge: if the row is created and the upload is then
 * refused, the document EXISTS with no file — which is a legal state — and the
 * dialog says so instead of pretending nothing happened and leaving an orphan
 * the operator cannot see.
 */
export function UploadDocumentDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (id: string) => void;
}) {
  const t = useT();
  const api = useApi();
  const [title, setTitle] = useState("");
  const [category, setCategory] = useState("General");
  const [description, setDescription] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [createdWithoutFile, setCreatedWithoutFile] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    setTitle("");
    setCategory("General");
    setDescription("");
    setFile(null);
    setAttempt(null);
    setCreatedWithoutFile(null);
    if (fileInput.current) fileInput.current.value = "";
  }, [open]);

  const create = useMutation({
    mutationFn: async () => {
      const doc = await api.createDocument({ title, description, category });
      if (!file) return doc;
      try {
        await api.uploadDocumentVersion(doc.id, file);
      } catch (error) {
        // The row landed. Say so, then re-throw so the refusal is rendered:
        // silently swallowing this would leave a document the operator did not
        // know they had created.
        setCreatedWithoutFile(doc.id);
        throw error;
      }
      return doc;
    },
    onSuccess: (doc) => {
      onCreated(doc.id);
      onClose();
    },
  });

  const clearStale = () => {
    if (create.isError) create.reset();
    if (attempt) setAttempt(null);
  };

  const submit = () => {
    const next: Attempt = {
      rules: () => [...titleCategoryRules(), fileRule()],
      values: { title, category, file: fileSnapshot(file) },
    };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    create.mutate();
  };

  return (
    <Dialog open={open} onClose={onClose} title={t("docs.documents.dialog.uploadTitle")}>
      <Form onSubmit={submit}>
        <div className="form-grid">
          <Field label={t("docs.documents.dialog.title")}>
            <input
              className={inputClass}
              maxLength={TITLE_MAX}
              value={title}
              onChange={(event) => {
                setTitle(event.target.value);
                clearStale();
              }}
            />
          </Field>
          <Field label={t("docs.documents.dialog.category")}>
            <input
              className={inputClass}
              maxLength={CATEGORY_MAX}
              value={category}
              onChange={(event) => {
                setCategory(event.target.value);
                clearStale();
              }}
            />
          </Field>
          <Field label={t("docs.documents.dialog.description")} full>
            <textarea
              className={`${inputClass} min-h-24`}
              value={description}
              onChange={(event) => {
                setDescription(event.target.value);
                clearStale();
              }}
            />
          </Field>
          <Field label={t("docs.documents.dialog.file")} full>
            <input
              ref={fileInput}
              className={inputClass}
              type="file"
              onChange={(event) => {
                setFile(event.target.files?.[0] ?? null);
                clearStale();
                setCreatedWithoutFile(null);
              }}
            />
          </Field>
        </div>
        <AcceptedTypes />
        {createdWithoutFile && (
          <p role="status" className="notice gold mt-2">
            <span>{t("docs.documents.dialog.createdWithoutFile")}</span>
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("docs.documents.dialog.cancel")}
          </Button>
          <FormActions
            label={t("docs.documents.dialog.upload")}
            pending={create.isPending}
            error={create.isError ? create.error : undefined}
            problems={attemptProblems(attempt)}
            failedMessage={t("docs.documents.dialog.uploadRefused")}
          />
        </div>
      </Form>
    </Dialog>
  );
}

/** A new version of an existing document (§6.1): one more append, never an overwrite. */
export function UploadVersionDialog({
  open,
  documentId,
  onClose,
  onUploaded,
}: {
  open: boolean;
  documentId: string;
  onClose: () => void;
  onUploaded: () => void;
}) {
  const t = useT();
  const api = useApi();
  const [file, setFile] = useState<File | null>(null);
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    setFile(null);
    setAttempt(null);
    if (fileInput.current) fileInput.current.value = "";
  }, [open]);

  const upload = useMutation({
    mutationFn: () => api.uploadDocumentVersion(documentId, file as File),
    onSuccess: onUploaded,
  });

  const submit = () => {
    const next: Attempt = { rules: () => [fileRule()], values: { file: fileSnapshot(file) } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    upload.mutate();
  };

  return (
    <Dialog open={open} onClose={onClose} title={t("docs.documents.dialog.newVersionTitle")}>
      <Form onSubmit={submit}>
        <div className="form-grid">
          <Field label={t("docs.documents.dialog.file")} full>
            <input
              ref={fileInput}
              className={inputClass}
              type="file"
              onChange={(event) => {
                setFile(event.target.files?.[0] ?? null);
                if (upload.isError) upload.reset();
                if (attempt) setAttempt(null);
              }}
            />
          </Field>
        </div>
        {/* §6.1/D7: there is no rollback verb, and this is where an operator
            would look for one. "Make version 2 current again" is
            download-then-upload — one more append, and the history stays
            literally true. */}
        <p className="note">{t("docs.documents.dialog.newVersionNote")}</p>
        <AcceptedTypes />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("docs.documents.dialog.cancel")}
          </Button>
          <FormActions
            label={t("docs.documents.dialog.upload")}
            pending={upload.isPending}
            error={upload.isError ? upload.error : undefined}
            problems={attemptProblems(attempt)}
            failedMessage={t("docs.documents.dialog.uploadRefused")}
          />
        </div>
      </Form>
    </Dialog>
  );
}

function EditDocumentDialog({
  open,
  doc,
  onClose,
  onSaved,
}: {
  open: boolean;
  doc: DocDocument;
  onClose: () => void;
  onSaved: () => void;
}) {
  const t = useT();
  const api = useApi();
  const [title, setTitle] = useState(doc.title);
  const [category, setCategory] = useState(doc.category);
  const [description, setDescription] = useState(doc.description);
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  useEffect(() => {
    if (!open) return;
    setTitle(doc.title);
    setCategory(doc.category);
    setDescription(doc.description);
    setAttempt(null);
  }, [open, doc.title, doc.category, doc.description]);

  const save = useMutation({
    mutationFn: () => api.updateDocument(doc.id, { title, description, category }),
    onSuccess: onSaved,
  });

  const submit = () => {
    const next: Attempt = { rules: titleCategoryRules, values: { title, category } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    save.mutate();
  };

  const clearStale = () => {
    if (save.isError) save.reset();
    if (attempt) setAttempt(null);
  };

  return (
    <Dialog open={open} onClose={onClose} title={t("docs.documents.dialog.editTitle")}>
      <Form onSubmit={submit}>
        <div className="form-grid">
          <Field label={t("docs.documents.dialog.title")}>
            <input
              className={inputClass}
              maxLength={TITLE_MAX}
              value={title}
              onChange={(event) => {
                setTitle(event.target.value);
                clearStale();
              }}
            />
          </Field>
          <Field label={t("docs.documents.dialog.category")}>
            <input
              className={inputClass}
              maxLength={CATEGORY_MAX}
              value={category}
              onChange={(event) => {
                setCategory(event.target.value);
                clearStale();
              }}
            />
          </Field>
          <Field label={t("docs.documents.dialog.description")} full>
            <textarea
              className={`${inputClass} min-h-24`}
              value={description}
              onChange={(event) => {
                setDescription(event.target.value);
                clearStale();
              }}
            />
          </Field>
        </div>
        {/* Metadata only. The file is a version, and versions are appended. */}
        <p className="note">{t("docs.documents.dialog.editNote")}</p>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("docs.documents.dialog.cancel")}
          </Button>
          <FormActions
            label={t("docs.documents.dialog.save")}
            pending={save.isPending}
            error={save.isError ? save.error : undefined}
            problems={attemptProblems(attempt)}
            failedMessage={t("docs.documents.dialog.saveFailed")}
          />
        </div>
      </Form>
    </Dialog>
  );
}
