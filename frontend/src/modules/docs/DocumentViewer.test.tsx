/**
 * F3d and the rendering half of §0.4's third wall.
 *
 * `viewer.test.ts` holds the CONSTANT — the map, exhaustively. This file holds
 * what the component does with it, and the assertion the whole module is built
 * around: an uploaded `.html`, `.svg` or `.xml` renders as SOURCE CHARACTERS
 * and never as markup.
 *
 * WHAT JSDOM CAN AND CANNOT PROVE HERE, STATED. jsdom parses HTML and builds a
 * real DOM, so "did a `<script>` element get created" and "is this string in
 * `textContent` rather than in `innerHTML`" are questions it answers exactly.
 * What it cannot answer is whether a `<embed type="application/pdf">` actually
 * hands the bytes to a separate-process PDF viewer, or whether an object URL
 * loads at all — jsdom implements no plugin surface and no navigation. Those
 * claims are browser-verified in `scripts/a11y-browser.mjs`'s harness page and
 * are described that way in the report, not here.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../lib/api";
import type { ApiClient, DocDocumentVersion } from "../../lib/api";
import { DocumentViewer } from "./DocumentViewer";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const saveBlob = vi.fn();
vi.mock("../../lib/download", () => ({ saveBlob: (...args: unknown[]) => saveBlob(...args) }));

/**
 * jsdom defines neither `URL.createObjectURL` nor `revokeObjectURL`. They are
 * stubbed rather than skipped so the REVOCATION is observable: an object URL
 * pins its blob for the life of the document, and a viewer that never revokes
 * accumulates every 25 MiB file an operator opens.
 */
const created: Blob[] = [];
const revoked: string[] = [];
beforeEach(() => {
  created.length = 0;
  revoked.length = 0;
  saveBlob.mockReset();
  mockUseApi.mockReset();
  URL.createObjectURL = vi.fn((blob: Blob) => {
    created.push(blob);
    return `blob:mock/${created.length}`;
  }) as unknown as typeof URL.createObjectURL;
  URL.revokeObjectURL = vi.fn((url: string) => {
    revoked.push(url);
  }) as unknown as typeof URL.revokeObjectURL;
});

function version(patch: Partial<DocDocumentVersion> = {}): DocDocumentVersion {
  return {
    version: 1,
    filename: "notes.txt",
    byte_size: 42,
    content_type: "text/plain",
    viewer: "text",
    sha256: "a".repeat(64),
    created_at: "2026-08-26T09:00:00.000Z",
    uploaded_by: "u1",
    purged_at: null,
    ...patch,
  };
}

function renderViewer(
  v: DocDocumentVersion,
  download: () => Promise<Blob> = () => Promise.resolve(new Blob(["x"])),
) {
  mockUseApi.mockReturnValue({
    downloadDocumentVersion: vi.fn(download),
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui: ReactElement = (
    <QueryClientProvider client={queryClient}>
      <DocumentViewer documentId="d1" version={v} />
    </QueryClientProvider>
  );
  return render(ui);
}

/** The uploaded payload every one of these files is really about. */
const HOSTILE = '<script>alert(document.cookie)</script><img src=x onerror=alert(1)>';

describe("the text branch, which is where HTML, SVG and XML land (F3d)", () => {
  it.each([
    ["an uploaded .html", "exfiltrate.html"],
    ["an uploaded .svg", "network-diagram.svg"],
    ["an uploaded .xml", "firewall-export.xml"],
    ["a .txt full of markup", "notes.txt"],
  ])("renders %s as literal characters, never as markup", async (_name, filename) => {
    // Every one of these arrives from the server as `text/plain` /
    // `viewer="text"` — `filetypes.py`'s text branch emits exactly three
    // content types and `text/html` and `image/svg+xml` are not among them
    // (F3b, server-side). This is the client half: even given that row, the
    // characters have to reach the DOM as TEXT.
    renderViewer(
      version({ filename, content_type: "text/plain", viewer: "text" }),
      () => Promise.resolve(new Blob([HOSTILE])),
    );

    const pre = await screen.findByTestId("document-source");
    // The characters are there, in order, as content.
    expect(pre.textContent).toBe(HOSTILE);
    // And no element was ever built from them. `querySelector` searches the
    // real parsed DOM, so this is the assertion that would fail the instant
    // someone reached for `dangerouslySetInnerHTML` on this branch.
    expect(pre.querySelector("script")).toBeNull();
    expect(pre.querySelector("img")).toBeNull();
    expect(document.querySelector("script")).toBeNull();
    // Escaped on the way in, which is what a text node serialises to.
    expect(pre.innerHTML).toContain("&lt;script&gt;");
    expect(pre.innerHTML).not.toContain("<script>");
  });

  it("builds no Blob and no object URL at all for a text document", async () => {
    // The safety property behind the one above: there is no `blob:` URL with a
    // rendering MIME in existence, so there is nothing for a browser to be
    // navigated to even if the markup had survived.
    renderViewer(version(), () => Promise.resolve(new Blob([HOSTILE])));
    await screen.findByTestId("document-source");
    expect(created).toEqual([]);
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it("does not treat a lying content_type as a rendering instruction", async () => {
    // The server cannot emit `text/html` (F3b). If it somehow did, the KIND
    // still decides the branch — `content_type` is consulted only inside the
    // image branch.
    renderViewer(
      version({ content_type: "text/html", viewer: "text", filename: "evil.html" }),
      () => Promise.resolve(new Blob([HOSTILE])),
    );
    const pre = await screen.findByTestId("document-source");
    expect(pre.querySelector("script")).toBeNull();
    expect(created).toEqual([]);
  });
});

describe("the pdf and image branches take their MIME from the map", () => {
  it("constructs a Blob of application/pdf and embeds it", async () => {
    renderViewer(
      version({ filename: "contract.pdf", content_type: "application/pdf", viewer: "pdf" }),
      () => Promise.resolve(new Blob(["%PDF-1.7"], { type: "application/octet-stream" })),
    );
    await waitFor(() => expect(created).toHaveLength(1));
    // The RESPONSE was `application/octet-stream` (§5.1 wall 1). What the
    // browser gets is the map's type, built here.
    expect(created[0]?.type).toBe("application/pdf");
    const embed = document.querySelector("embed");
    expect(embed).not.toBeNull();
    expect(embed?.getAttribute("type")).toBe("application/pdf");
    expect(embed?.getAttribute("src")).toBe("blob:mock/1");
    // No iframe, sandboxed or otherwise — §5.2 explains why the obvious
    // choice does not do what its name suggests.
    expect(document.querySelector("iframe")).toBeNull();
  });

  it.each([
    ["image/png", "rack.png"],
    ["image/jpeg", "rack.jpg"],
    ["image/gif", "blink.gif"],
    ["image/webp", "rack.webp"],
  ])("constructs a Blob of %s for the raster branch", async (contentType, filename) => {
    renderViewer(
      version({ filename, content_type: contentType, viewer: "image" }),
      () => Promise.resolve(new Blob(["\x89PNG"], { type: "application/octet-stream" })),
    );
    await waitFor(() => expect(created).toHaveLength(1));
    expect(created[0]?.type).toBe(contentType);
    expect(await screen.findByAltText(filename)).toBeInTheDocument();
  });

  it("refuses to paint an image whose content type is not one of the four", async () => {
    // An SVG can never reach this state — the server stores it `text/plain` —
    // but the client does not lean on that, because leaning on it is what
    // makes two independent walls into one.
    renderViewer(
      version({ filename: "diagram.svg", content_type: "image/svg+xml", viewer: "image" }),
    );
    expect(
      await screen.findByText(/does not preview image\/svg\+xml files/),
    ).toBeInTheDocument();
    expect(document.querySelector("img")).toBeNull();
    expect(created).toEqual([]);
  });

  it("degrades an unknown viewer kind to download, not to guessing", async () => {
    renderViewer(version({ filename: "thing.dat", content_type: "text/plain", viewer: "html" }));
    expect(await screen.findByText(/does not preview text\/plain files/)).toBeInTheDocument();
    expect(created).toEqual([]);
  });

  it("revokes the object URL when the viewer goes away", async () => {
    const { unmount } = renderViewer(
      version({ filename: "contract.pdf", content_type: "application/pdf", viewer: "pdf" }),
    );
    await waitFor(() => expect(created).toHaveLength(1));
    unmount();
    expect(revoked).toEqual(["blob:mock/1"]);
  });
});

describe("the markdown branch goes through the shipped sanitizer (F3e)", () => {
  it("renders markdown as markup and strips what the allow-list refuses", async () => {
    renderViewer(
      version({ filename: "runbook.md", content_type: "text/markdown", viewer: "markdown" }),
      () => Promise.resolve(new Blob([`# Recovery\n\n${HOSTILE}\n\nStep one.`])),
    );
    expect(await screen.findByText("Recovery")).toBeInTheDocument();
    // `markdown.ts`'s allow-list is the coverage (`markdown.test.ts`); this
    // asserts the branch actually goes through it rather than adding a second
    // sanitizer to drift from the first.
    expect(document.querySelector("script")).toBeNull();
    expect(document.querySelector("img")).toBeNull();
  });
});

describe("the four states of §11.7", () => {
  it("says it is LOADING rather than showing an empty panel", () => {
    renderViewer(version(), () => new Promise<never>(() => {}));
    expect(screen.getByText("Loading notes.txt…")).toBeInTheDocument();
  });

  it("says the bytes FAILED, and names the dependency the server named", async () => {
    // §0.9/F8b: object storage is a soft dependency, so the metadata is intact
    // and only the bytes are gone. A spinner that never resolves, or an empty
    // panel, would tell an operator their library was destroyed.
    renderViewer(version(), () =>
      Promise.reject(
        new ApiError(
          503,
          "object storage is unavailable, so this document's bytes could not be read; its metadata is intact (EndpointConnectionError)",
        ),
      ),
    );
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/object storage is unavailable/);
    expect(alert).toHaveTextContent(/metadata is intact/);
    expect(screen.queryByTestId("document-source")).not.toBeInTheDocument();
  });

  it("says a PURGED version was destroyed on purpose, not that it is missing", async () => {
    renderViewer(
      version({ filename: "leaked.pdf", byte_size: 2_202_010, purged_at: "2026-08-26T10:00:00.000Z" }),
    );
    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent(/These bytes were destroyed/);
    expect(status).toHaveTextContent(/leaked\.pdf/);
    expect(status).toHaveTextContent(/2\.1 MiB/);
    expect(status).toHaveTextContent(/audit log/);
    // No fetch is attempted: the content route would answer 410 and the round
    // trip buys nothing the tombstone does not already say.
    expect(screen.queryByRole("button", { name: /Download/ })).not.toBeInTheDocument();
  });

  it("says a file is NOT PREVIEWABLE and offers the download instead", async () => {
    renderViewer(
      version({ filename: "package.zip", content_type: "application/zip", viewer: "none" }),
    );
    expect(await screen.findByText(/does not preview application\/zip files/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download" })).toBeInTheDocument();
  });
});

describe("downloading, and saying so", () => {
  it("saves through the authenticated client, never through a bare link", async () => {
    renderViewer(
      version({ filename: "package.zip", content_type: "application/zip", viewer: "none" }),
    );
    await userEvent.click(await screen.findByRole("button", { name: "Download" }));
    await waitFor(() => expect(saveBlob).toHaveBeenCalledTimes(1));
    expect(saveBlob.mock.calls[0]?.[1]).toBe("package.zip");
    // NOT an `<a href="/api/v1/…" download>`: the browser navigates to one
    // itself and sends no bearer token, so the route answers 401 and the
    // browser saves the refusal under the file's name (`lib/download.ts`).
    expect(
      [...document.querySelectorAll("a")].filter((a) => a.getAttribute("href")?.includes("/api/")),
    ).toEqual([]);
  });

  it("surfaces a REFUSED download rather than looking like no click at all", async () => {
    mockUseApi.mockReturnValue({
      downloadDocumentVersion: vi
        .fn()
        .mockRejectedValue(new ApiError(403, "You do not have permission to do that.")),
    } as unknown as ApiClient);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <DocumentViewer
          documentId="d1"
          version={version({ filename: "package.zip", content_type: "application/zip", viewer: "none" })}
        />
      </QueryClientProvider>,
    );
    await userEvent.click(await screen.findByRole("button", { name: "Download" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /package\.zip was not downloaded/,
    );
  });

  it("says out loud that a view IS a download, and offers no way to avoid it", async () => {
    // D10: the server cannot tell viewing from saving, so the console must not
    // imply it can. There is no "preview without downloading" control here and
    // there must never be one — the operation does not exist behind the API.
    renderViewer(version(), () => Promise.resolve(new Blob(["plain"])));
    await screen.findByTestId("document-source");
    expect(screen.getByText(/recorded in the audit log as a download/)).toBeInTheDocument();
    expect(screen.queryByText(/without downloading/i)).not.toBeInTheDocument();
  });
});
