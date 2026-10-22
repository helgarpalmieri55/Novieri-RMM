/**
 * F3c — THE RENDER-MAP TEST. This is the assertion that stands where a second
 * origin would.
 *
 * `spec-attachments.md` §0.4 builds the XSS containment out of three walls
 * instead of one hostname, and the client holds the third: "the console's
 * render map has exactly one entry per server-declared viewer kind, and no
 * entry produces a document context for a type that can carry script".
 *
 * WHAT IT COSTS TO BE WRONG, stated by the spec itself: "With a separate
 * origin, wall 3 could be wrong and nothing would happen. Here, wall 3 is a
 * client-side constant, and if someone later adds `text/html` to the render
 * map, walls 1 and 2 do not save us -- a `Blob` of type `text/html` opened
 * from the console origin executes in it." MinIO is routed on the console's
 * own hostname (`deploy/docker-compose.yml`), so "the console origin" is the
 * whole tenant. That constant is therefore pinned by this file rather than by
 * care.
 *
 * WHY IT ASSERTS OVER THE MAP OBJECT AND NOT OVER RENDERED OUTPUT. §13 F3c
 * says so, and the reason is coverage: rendered output can only be checked for
 * the kinds some test remembers to render, while the map is finite and can be
 * checked exhaustively. `DocumentViewer.test.tsx` covers the rendering half
 * (F3d); this covers the constant.
 */

import { describe, expect, it } from "vitest";

import {
  DOWNLOAD_ONLY_MIME,
  IMAGE_BLOB_TYPES,
  RENDER_MAP,
  VIEWER_KINDS,
  blobTypeFor,
  resolveViewer,
} from "./viewer";

/**
 * The four values that must never be constructible, by name.
 *
 * `text/html` and `image/svg+xml` are the two the server can never emit
 * (`filetypes.py::ACCEPTED_CONTENT_TYPES` is closed and excludes both, pinned
 * server-side by F3b). `application/xhtml+xml` and `text/xml` are here because
 * a browser gives a document context to those too, and the server not being
 * able to emit them is not a reason for the client to be able to construct
 * them -- the two halves of the wall are independent on purpose.
 */
const RENDERING_TYPES = [
  "text/html",
  "image/svg+xml",
  "application/xhtml+xml",
  "text/xml",
];

/** Every Blob MIME this module can produce, from anywhere in it. */
const EVERY_BLOB_TYPE = [
  ...Object.values(RENDER_MAP).map((rule) => rule.blobType),
  ...Object.values(IMAGE_BLOB_TYPES),
  DOWNLOAD_ONLY_MIME,
].filter((type): type is string => type != null);

describe("the render map (F3c)", () => {
  it("has exactly one entry per server-declared viewer kind, and no others", () => {
    // Both directions. A missing kind falls through to download-only, which is
    // safe but silently loses a viewer; an EXTRA key is a kind the server can
    // never send, which is a rendering branch nothing on the wire selects and
    // an attacker's first place to look.
    expect(Object.keys(RENDER_MAP).sort()).toEqual([...VIEWER_KINDS].sort());
  });

  it("mirrors the server's own kind list", () => {
    // `filetypes.py::VIEWER_KINDS` is the source of truth; this is the
    // transcription. Written out literally rather than imported (there is no
    // Python here) so a change on either side has to be made on both.
    expect([...VIEWER_KINDS].sort()).toEqual(
      ["image", "markdown", "none", "pdf", "text"],
    );
  });

  it.each(RENDERING_TYPES)("can never construct a Blob of type %s", (type) => {
    expect(EVERY_BLOB_TYPE).not.toContain(type);
  });

  it("constructs no Blob type outside a closed list of five", () => {
    // The stronger form of the assertion above: not merely "none of the four
    // named types", but "nothing this file has not enumerated". A future edit
    // introducing `text/html+something` fails here even though it is on no
    // named list.
    expect([...new Set(EVERY_BLOB_TYPE)].sort()).toEqual([
      "application/octet-stream",
      "application/pdf",
      "image/gif",
      "image/jpeg",
      "image/png",
      "image/webp",
    ]);
  });

  it("gives the image branch exactly the four raster types, keyed client-side", () => {
    // §5.2: the image Blob's MIME comes from "a four-entry CLIENT-side map
    // keyed on the stored `content_type`", not from a server field. The server
    // saying `image/png` selects a row here; it does not become the Blob type
    // by being repeated back.
    expect(Object.keys(IMAGE_BLOB_TYPES).sort()).toEqual([
      "image/gif",
      "image/jpeg",
      "image/png",
      "image/webp",
    ]);
  });

  it("renders the five kinds through five distinct branches", () => {
    expect(
      Object.fromEntries(
        Object.entries(RENDER_MAP).map(([kind, rule]) => [kind, rule.render]),
      ),
    ).toEqual({
      pdf: "embed",
      image: "image",
      markdown: "markdown",
      text: "text",
      none: "download",
    });
  });
});

describe("resolving a viewer kind off the wire (F3c)", () => {
  it("resolves each known kind to its own branch", () => {
    expect(resolveViewer("pdf", "application/pdf").render).toBe("embed");
    expect(resolveViewer("image", "image/png").render).toBe("image");
    expect(resolveViewer("markdown", "text/markdown").render).toBe("markdown");
    expect(resolveViewer("text", "text/plain").render).toBe("text");
    expect(resolveViewer("none", "application/zip").render).toBe("download");
  });

  it.each([
    "html",
    "svg",
    "xml",
    "PDF",
    "",
    "__proto__",
    "constructor",
    "toString",
  ])("degrades an unknown viewer kind %s to download-only, never to guessing", (kind) => {
    // "An unknown `viewer` value degrades to download, it does not degrade to
    // guessing" (§5.2, the last row of the table, which the spec calls the
    // point of the table).
    //
    // `__proto__`, `constructor` and `toString` are here because a plain
    // object literal answers `in` and property access for all three: a lookup
    // written as `RENDER_MAP[kind] ?? NONE` would resolve `constructor` to a
    // function and `toString` to a method, and the branch taken next would be
    // whatever that coerced to. The resolver uses the kind list, not property
    // presence, and this is what says so.
    const resolved = resolveViewer(kind, "text/html");
    expect(resolved.render).toBe("download");
    expect(resolved.blobType).toBe(DOWNLOAD_ONLY_MIME);
  });

  it("degrades an image whose content type is not one of the four", () => {
    // The spec's own words: "anything else falls through to the `none` row".
    // An SVG can never arrive with `viewer="image"` -- the server stores it as
    // `text/plain`/`text` -- but the client does not get to rely on that,
    // because relying on it is what makes the two walls one wall.
    for (const type of ["image/svg+xml", "image/bmp", "image/tiff", ""]) {
      const resolved = resolveViewer("image", type);
      expect(resolved.render).toBe("download");
      expect(resolved.blobType).toBe(DOWNLOAD_ONLY_MIME);
    }
  });

  it("gives the four raster types their own Blob MIME and nothing else", () => {
    expect(resolveViewer("image", "image/png").blobType).toBe("image/png");
    expect(resolveViewer("image", "image/jpeg").blobType).toBe("image/jpeg");
    expect(resolveViewer("image", "image/gif").blobType).toBe("image/gif");
    expect(resolveViewer("image", "image/webp").blobType).toBe("image/webp");
  });

  it("builds no Blob at all for the two text branches", () => {
    // Markdown and text are decoded as UTF-8 and never become a Blob, so
    // there is no MIME for a browser to act on and no object URL to navigate
    // to. `blobTypeFor` returning null is what the component branches on.
    expect(blobTypeFor(resolveViewer("markdown", "text/markdown"))).toBeNull();
    expect(blobTypeFor(resolveViewer("text", "text/plain"))).toBeNull();
  });

  it("sends a PDF to the browser's own viewer, in a separate process", () => {
    expect(resolveViewer("pdf", "application/pdf").blobType).toBe("application/pdf");
  });

  it("ignores the content type entirely for every non-image kind", () => {
    // The stored `content_type` selects a row ONLY inside the image branch.
    // A `text` document claiming `text/html`, or a `pdf` claiming
    // `image/svg+xml`, changes nothing: the kind decides the branch.
    expect(resolveViewer("text", "text/html").render).toBe("text");
    expect(resolveViewer("text", "text/html").blobType).toBeNull();
    expect(resolveViewer("pdf", "image/svg+xml").blobType).toBe("application/pdf");
    expect(resolveViewer("markdown", "text/html").blobType).toBeNull();
  });
});
