/**
 * THE THIRD WALL (`docs/modules/documentation/spec-attachments.md` §0.4).
 *
 * This file is a constant, and it is the entire client-side half of this
 * module's XSS containment. Read §0.4 before editing it.
 *
 * THE SHAPE OF THE PROBLEM. A stored document is bytes an attacker may have
 * chosen. The containment is three walls:
 *
 *   1. The transport is always download-shaped: the content route answers
 *      `application/octet-stream`, `Content-Disposition: attachment` and
 *      `Cache-Control: no-store` whatever the bytes are, so direct navigation
 *      to a document URL downloads a file and never renders a page.
 *   2. The type is decided SERVER-SIDE from the leading bytes
 *      (`filetypes.py::sniff`) and stored in a column. The client never
 *      declares one and there is no field in the upload request in which it
 *      could.
 *   3. This map. Exactly one entry per server-declared viewer kind, and no
 *      entry produces a document context for a type that can carry script.
 *
 * WHY WALL 3 IS LOAD-BEARING RATHER THAN BELT-AND-BRACES. The spec chose
 * three walls over a separate file-serving origin, and stated the cost in the
 * same breath: "With a separate origin, wall 3 could be wrong and nothing
 * would happen. Here, wall 3 is a client-side constant, and if someone later
 * adds `text/html` to the render map, walls 1 and 2 do not save us -- a `Blob`
 * of type `text/html` opened from the console origin executes in it."
 *
 * And "the console origin" is not a small blast radius. `deploy/docker-
 * compose.yml` routes MinIO on the console's own hostname behind the same
 * Traefik entrypoint, which is also why §0.3 forbids presigned URLs outright.
 * A rendering Blob built here runs with the session of every operator in the
 * tenant.
 *
 * SO THE RULE IS: THE MIME COMES FROM THIS FILE, NEVER FROM THE WIRE. Not
 * from a filename, not from an extension, not from a response header, and not
 * from the stored `content_type` -- which selects a row inside the image
 * branch and is otherwise ignored. `viewer.test.ts` asserts the closed set
 * over the map object rather than over rendered output, because a finite
 * constant can be checked exhaustively and rendered output can only be checked
 * for the kinds a test remembers to render.
 *
 * HTML, SVG AND XML ARE ACCEPTED AND ARE NEVER RENDERED AS MARKUP. They decode
 * as UTF-8, so the server stores them as `text/plain` with `viewer="text"` and
 * this file sends them to the `text` branch, which sets them as TEXT CONTENT
 * in a `<pre>`. That is deliberate and useful -- reading a firewall config
 * export or a `.drawio` file as source is real operator work -- and the
 * upgrade path if the operator ever wants SVG *rendered* is the second origin
 * (§0.4, D3), not an exception here.
 */

/**
 * The kinds the server can send, mirroring `filetypes.py::VIEWER_KINDS`.
 *
 * A LIST, NOT `Object.keys(RENDER_MAP)`. The two are asserted equal in the
 * test, and deriving one from the other would make that assertion vacuous --
 * a rendering branch added to the map would silently become a "known kind"
 * rather than failing.
 */
export const VIEWER_KINDS = ["pdf", "image", "markdown", "text", "none"] as const;

export type ViewerKind = (typeof VIEWER_KINDS)[number];

/** How the viewer component draws a kind. One branch per kind, no overlap. */
export type RenderBranch = "embed" | "image" | "markdown" | "text" | "download";

export interface RenderRule {
  render: RenderBranch;
  /**
   * The Blob MIME this branch may construct, or `null` when it constructs no
   * Blob at all.
   *
   * `null` is not "unset": the two text branches decode the bytes as UTF-8 and
   * never build a Blob, so there is no MIME for a browser to act on and no
   * object URL to navigate to. The image branch is `null` here because its
   * type comes from `IMAGE_BLOB_TYPES` per stored content type; `resolveViewer`
   * is what fills it in, and what refuses to.
   */
  blobType: string | null;
}

/**
 * What a file that cannot be previewed becomes: bytes with no type a browser
 * will act on. Also what every unknown kind resolves to.
 */
export const DOWNLOAD_ONLY_MIME = "application/octet-stream";

/**
 * The image branch's four entries, CLIENT-SIDE (§5.2).
 *
 * Keyed on the stored `content_type`, but the VALUE is this file's, not the
 * server's. The distinction is the whole of F3c's image half: the server
 * saying `image/png` selects a row here; it does not become the Blob type by
 * being echoed back. Anything not on these four rows falls through to
 * download-only.
 *
 * Scripts and external references are disabled for content loaded through
 * `<img>`, and none of these four can execute in that context. `image/svg+xml`
 * is absent for the obvious reason and can never arrive anyway -- the server
 * stores an SVG as `text/plain` -- but the client does not lean on that,
 * because leaning on it would make the two walls one wall.
 */
export const IMAGE_BLOB_TYPES: Readonly<Record<string, string>> = {
  "image/png": "image/png",
  "image/jpeg": "image/jpeg",
  "image/gif": "image/gif",
  "image/webp": "image/webp",
};

/**
 * ONE ENTRY PER KIND. Adding a row here is a security decision, not a feature.
 */
export const RENDER_MAP: Readonly<Record<ViewerKind, RenderRule>> = {
  // The browser's own PDF viewer, which runs in a separate process and cannot
  // touch the embedder's DOM. `<embed>` rather than `<iframe sandbox>`: a blob
  // URL carries its creating origin, and a sandbox WITHOUT `allow-same-origin`
  // gets an opaque one, which Chrome then refuses to load a blob into at all
  // -- while WITH `allow-same-origin` the attribute is no longer isolating
  // anything. The sandboxed-iframe-over-blob combination does not do what its
  // name suggests (§5.2).
  pdf: { render: "embed", blobType: "application/pdf" },
  // Filled in per stored content type by `resolveViewer`, from the four-entry
  // map above.
  image: { render: "image", blobType: null },
  // The bytes are decoded as UTF-8 and handed to the SHIPPED `renderMarkdown`,
  // whose allow-list sanitizer is the coverage (F3e). No second sanitizer and
  // no second allow-list -- a fork of one is a fork that drifts.
  markdown: { render: "markdown", blobType: null },
  // Decoded as UTF-8 and set as TEXT CONTENT. This is where HTML, SVG and XML
  // land, and it is why they are safe.
  text: { render: "text", blobType: null },
  none: { render: "download", blobType: DOWNLOAD_ONLY_MIME },
};

/** The row every unknown thing lands on. */
const DOWNLOAD_ONLY: RenderRule = { render: "download", blobType: DOWNLOAD_ONLY_MIME };

function isViewerKind(value: string): value is ViewerKind {
  // `.includes` ON THE LIST, NOT `value in RENDER_MAP` AND NOT `MAP[value] ??`.
  // A plain object literal answers property access for `__proto__`,
  // `constructor` and `toString`, so a lookup written the obvious way resolves
  // `constructor` to a function and then branches on whatever that coerces to.
  // The kind list has no prototype chain to fall down.
  return (VIEWER_KINDS as readonly string[]).includes(value);
}

/**
 * The one function the viewer is allowed to ask "how do I draw this".
 *
 * `viewer` and `contentType` are both raw strings off the wire and are treated
 * as such: neither is trusted to be one of the known values, and neither is
 * used to construct a MIME. What comes back is a row of this file.
 *
 * An unknown kind degrades to download, NOT to guessing -- the last row of
 * §5.2's table, which the spec calls the point of the table.
 */
export function resolveViewer(viewer: string, contentType: string): RenderRule {
  if (!isViewerKind(viewer)) return DOWNLOAD_ONLY;
  const rule = RENDER_MAP[viewer];
  if (rule.render !== "image") return rule;
  const blobType = Object.hasOwn(IMAGE_BLOB_TYPES, contentType)
    ? IMAGE_BLOB_TYPES[contentType]
    : undefined;
  // A raster kind whose type is not one of the four is a file this console
  // will not paint. It is still downloadable; it is just not previewed.
  return blobType === undefined ? DOWNLOAD_ONLY : { render: "image", blobType };
}

/** The Blob MIME for a resolved rule, or null when the branch builds none. */
export function blobTypeFor(rule: RenderRule): string | null {
  return rule.blobType;
}

/**
 * Whether a version can be previewed at all, for the copy above the panel.
 *
 * Deliberately derived from the resolved rule rather than from the kind, so
 * "this is previewable" and "this is what will be drawn" can never disagree --
 * a WebP-with-a-broken-content-type would otherwise be announced as an image
 * and then rendered as a download prompt.
 */
export function isPreviewable(rule: RenderRule): boolean {
  return rule.render !== "download";
}
