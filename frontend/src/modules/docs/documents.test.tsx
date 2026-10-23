/**
 * The Documents screens: §11.7's four distinct states, the upload refusals,
 * the version drawer, the links editor and the purge gate.
 *
 * THE RULE THIS FILE ENFORCES OVER AND OVER is the one the shipped page
 * already learned twice: a failed query and an empty result must never render
 * the same pixels. On a documentation library that mistake tells an operator
 * their contracts are gone; on the version drawer it claims a file has one
 * version when nobody knows how many it has.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../lib/api";
import type { ApiClient, DocDocument, DocDocumentVersion, Me } from "../../lib/api";
import { DocsPage } from "./DocsPage";
import { DocumentView, sizeLabel, typeLabel } from "./documents";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
vi.mock("../../lib/download", () => ({ saveBlob: vi.fn() }));

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  permissions: ["docs.read", "docs.manage"],
  // Asia/Tokyo on a UTC runner, so every timestamp assertion can tell "read
  // the operator's profile" from "read the machine the browser is on".
  timezone: "Asia/Tokyo",
};

const PDF: DocDocumentVersion = {
  version: 2,
  filename: "vendor-contract-2026.pdf",
  byte_size: 2_202_010,
  content_type: "application/pdf",
  viewer: "pdf",
  sha256: "b".repeat(64),
  created_at: "2026-08-20T01:00:00.000Z",
  uploaded_by: "u1",
  purged_at: null,
};

const DOC: DocDocument = {
  id: "d1",
  title: "Acme support contract",
  description: "Signed 2026-03-01, renews annually.",
  category: "Contracts",
  archived: false,
  current_version: 2,
  updated_at: "2026-08-20T01:00:00.000Z",
  current: PDF,
  links: [{ id: "l1", target_type: "device", target_id: "11111111-1111-1111-1111-111111111111" }],
};

beforeEach(() => {
  mockUseApi.mockReset();
  URL.createObjectURL = vi.fn(() => "blob:mock/1") as unknown as typeof URL.createObjectURL;
  URL.revokeObjectURL = vi.fn() as unknown as typeof URL.revokeObjectURL;
});

function renderView(api: Partial<ApiClient>, permissions: string[] = ME.permissions) {
  mockUseApi.mockReturnValue({
    getDocument: vi.fn().mockResolvedValue(DOC),
    documentVersions: vi.fn().mockResolvedValue([PDF]),
    downloadDocumentVersion: vi.fn().mockResolvedValue(new Blob(["%PDF-1.7"])),
    ...api,
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui: ReactElement = (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/docs?document=d1"]}>
        <DocumentView
          documentId="d1"
          canManage={permissions.includes("docs.manage")}
          canPurge={permissions.includes("docs.document.purge")}
          zone="Asia/Tokyo"
          onCleared={() => undefined}
        />
      </MemoryRouter>
    </QueryClientProvider>
  );
  return render(ui);
}

function renderPage(api: Partial<ApiClient>, permissions: string[] = ME.permissions) {
  mockUseApi.mockReturnValue({
    me: vi.fn().mockResolvedValue({ ...ME, permissions }),
    listArticles: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    listDocRecords: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    listDocuments: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    ...api,
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/docs"]}>
        <DocsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("the library list (§11.2)", () => {
  it("groups documents by category, as Articles is grouped", async () => {
    renderPage({
      listDocuments: vi.fn().mockResolvedValue({
        items: [
          { id: "d1", title: "Acme support contract", description: "", category: "Contracts", archived: false, current_version: 2, updated_at: "2026-08-20T01:00:00.000Z", link_count: 1 },
          { id: "d2", title: "Rack elevation", description: "", category: "Diagrams", archived: false, current_version: 1, updated_at: "2026-08-21T01:00:00.000Z", link_count: 0 },
        ],
        total: 2,
      }),
    });
    expect(await screen.findByText("Acme support contract")).toBeInTheDocument();
    expect(screen.getByText("Rack elevation")).toBeInTheDocument();
    expect(screen.getByText("Contracts")).toBeInTheDocument();
    expect(screen.getByText("Diagrams")).toBeInTheDocument();
  });

  it("says a document has NO FILE YET rather than showing it as v0", async () => {
    // §4.1: a document with zero versions is legal and visible. "v0" is a
    // number an operator has to decode; "no file yet" is the fact.
    renderPage({
      listDocuments: vi.fn().mockResolvedValue({
        items: [
          { id: "d3", title: "Firewall config export", description: "", category: "General", archived: false, current_version: 0, updated_at: "2026-08-22T01:00:00.000Z", link_count: 0 },
        ],
        total: 1,
      }),
    });
    expect(await screen.findByText("Firewall config export")).toBeInTheDocument();
    expect(screen.getByText(/no file yet/)).toBeInTheDocument();
    expect(screen.queryByText("v0")).not.toBeInTheDocument();
  });

  it("says the DOCUMENTS request FAILED rather than showing an empty library", async () => {
    // §11.7, first state. The whole reason `QueryState` exists, applied to the
    // new list: a dead metadata path and an empty library must not read alike.
    renderPage({ listDocuments: vi.fn().mockRejectedValue(new Error("boom")) });
    await waitFor(() => expect(screen.getAllByRole("alert").length).toBeGreaterThan(0));
    expect(screen.queryByText("No documents yet.")).not.toBeInTheDocument();
  });

  it("says the library is empty when it is genuinely empty", async () => {
    renderPage({});
    expect(await screen.findByText("No documents yet.")).toBeInTheDocument();
  });

  it("hides Upload document from a caller without docs.manage", async () => {
    renderPage({}, ["docs.read"]);
    await screen.findByText("No documents yet.");
    expect(screen.queryByRole("button", { name: "Upload document" })).not.toBeInTheDocument();
  });

  it("states what search does and does not reach (§11.6)", async () => {
    renderPage({});
    expect(
      await screen.findByText(/does not search inside stored files/),
    ).toBeInTheDocument();
  });
});

describe("the document view (§11.3)", () => {
  it("heads the pane with title, category, version, filename, size and type", async () => {
    renderView({});
    expect(await screen.findByRole("heading", { name: "Acme support contract" })).toBeInTheDocument();
    expect(screen.getByText("Contracts")).toBeInTheDocument();
    expect(screen.getByText("v2")).toBeInTheDocument();
    expect(
      screen.getByText(/vendor-contract-2026\.pdf · 2\.1 MiB · PDF/),
    ).toBeInTheDocument();
  });

  it("says the document could not be LOADED rather than rendering a blank pane", async () => {
    renderView({ getDocument: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /could not be loaded, so nothing below describes it/,
    );
  });

  it("renders a zero-version document as a state, not an error (§4.1)", async () => {
    renderView({
      getDocument: vi.fn().mockResolvedValue({ ...DOC, current_version: 0, current: null }),
    });
    expect(await screen.findByText(/No file uploaded yet/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Upload a file" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("refuses a new version on an archived document before the request (§4.4)", async () => {
    renderView({ getDocument: vi.fn().mockResolvedValue({ ...DOC, archived: true }) });
    await screen.findByRole("heading", { name: "Acme support contract" });
    expect(screen.getByRole("button", { name: "Upload new version" })).toBeDisabled();
    // And it says WHY, rather than leaving a dead control (the silent-no-op
    // class the form kernel exists to end).
    expect(screen.getByRole("status")).toHaveTextContent(/accepts no new versions/);
    // The archive control is gone rather than disabled: it is already archived.
    expect(screen.queryByRole("button", { name: "Archive" })).not.toBeInTheDocument();
  });

  it("keeps every control off a caller without docs.manage", async () => {
    renderView({}, ["docs.read"]);
    await screen.findByRole("heading", { name: "Acme support contract" });
    for (const name of ["Upload new version", "Edit details", "Archive"]) {
      expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
    }
    // Reading is not gated: `docs.read` downloads and views, and the control
    // on reading is the audit event, not a second permission (§9).
    expect(screen.getAllByRole("button", { name: "Download" }).length).toBeGreaterThan(0);
  });
});

describe("purge, the only irreversible act (§6.3, §9)", () => {
  it("is invisible without docs.document.purge, even to a manager", async () => {
    renderView({}, ["docs.read", "docs.manage"]);
    await screen.findByRole("heading", { name: "Acme support contract" });
    // A technician can archive; only an admin can destroy. The console hides
    // it and the server refuses regardless -- the frontend never gates
    // security (invariant 6).
    expect(screen.queryByRole("button", { name: /Purge/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Archive" })).toBeInTheDocument();
  });

  it("names the exact version, file and size, and makes it typed", async () => {
    renderView({}, ["docs.read", "docs.manage", "docs.document.purge"]);
    await userEvent.click(await screen.findByRole("button", { name: "Purge v2" }));
    const dialog = await screen.findByRole("dialog", { name: "Destroy these bytes" });
    expect(dialog).toHaveTextContent("Version 2 — vendor-contract-2026.pdf, 2.1 MiB");
    // Both halves of the sentence: what is destroyed, and what survives.
    expect(dialog).toHaveTextContent(/cannot be recovered/);
    expect(dialog).toHaveTextContent(/survive in the version history and the audit log/);
  });

  it("offers no document-level purge, because there is no such operation", async () => {
    // §11.3: purge is per version and the control follows the version. A
    // document is a row and a name; destroying every version one at a time is
    // the honest shape of destroying its contents.
    renderView({}, ["docs.read", "docs.manage", "docs.document.purge"]);
    await screen.findByRole("heading", { name: "Acme support contract" });
    const purgeButtons = screen
      .getAllByRole("button")
      .filter((b) => /purge/i.test(b.textContent ?? ""));
    expect(purgeButtons.map((b) => b.textContent)).toEqual(["Purge v2"]);
  });

  it("surfaces a refused purge and says nothing was destroyed", async () => {
    renderView(
      {
        purgeDocumentVersion: vi
          .fn()
          .mockRejectedValue(
            new ApiError(
              503,
              "object storage is unavailable, so these bytes could not be destroyed; nothing was changed (EndpointConnectionError)",
            ),
          ),
      },
      ["docs.read", "docs.manage", "docs.document.purge"],
    );
    await userEvent.click(await screen.findByRole("button", { name: "Purge v2" }));
    const dialog = await screen.findByRole("dialog", { name: "Destroy these bytes" });
    await userEvent.type(
      within(dialog).getByRole("textbox"),
      "Version 2 — vendor-contract-2026.pdf, 2.1 MiB",
    );
    await userEvent.click(within(dialog).getByRole("button", { name: "Destroy the bytes" }));
    // A purge that cannot reach the store changes NOTHING -- no tombstone, no
    // audit event (§16.1 note 3). The console must not report a destruction
    // that did not happen.
    expect(await screen.findByText(/Nothing was destroyed/)).toBeInTheDocument();
  });
});

describe("the version drawer (§11.3)", () => {
  it("says the history FAILED rather than claiming there is one version", async () => {
    renderView({ documentVersions: vi.fn().mockRejectedValue(new Error("boom")) });
    await userEvent.click(await screen.findByRole("button", { name: "History" }));
    expect(await screen.findByText(/not a list of this document's files/)).toBeInTheDocument();
  });

  it("renders a purged version as a tombstone, not as a missing row", async () => {
    renderView({
      documentVersions: vi.fn().mockResolvedValue([
        PDF,
        {
          ...PDF,
          version: 1,
          filename: "vendor-contract-2025.pdf",
          purged_at: "2026-08-25T02:00:00.000Z",
        },
      ]),
    });
    await userEvent.click(await screen.findByRole("button", { name: "History" }));
    expect(await screen.findByText("vendor-contract-2025.pdf")).toBeInTheDocument();
    // Its digest, size and filename survive; its controls do not.
    expect(screen.getByText(/^purged /)).toBeInTheDocument();
  });

  it("dates every version in the OPERATOR'S timezone, not the runner's", async () => {
    // 2026-08-20T01:00Z is 2026-08-20 10:00 in Asia/Tokyo. A version history
    // is where a wrong zone is least visible and most expensive.
    renderView({});
    await userEvent.click(await screen.findByRole("button", { name: "History" }));
    const row = (await screen.findByText("vendor-contract-2026.pdf")).closest("tr");
    expect(row?.textContent).toMatch(/10:00/);
  });

  it("gives every column a header a screen reader can announce", async () => {
    renderView({});
    await userEvent.click(await screen.findByRole("button", { name: "History" }));
    await screen.findByText("vendor-contract-2026.pdf");
    for (const th of document.querySelectorAll("th")) {
      expect(th.textContent?.trim()).not.toBe("");
    }
  });
});

describe("the links editor (§7)", () => {
  it("lists what the document describes, and says a credential link is metadata", async () => {
    renderView({});
    await userEvent.click(await screen.findByRole("button", { name: "Links" }));
    expect(await screen.findByText("11111111-1111-1111-1111-111111111111")).toBeInTheDocument();
    // Invariant 3 is untouched: nothing in this module reads the vault.
    expect(screen.getByText(/names the credential and never its secret/)).toBeInTheDocument();
  });

  it("refuses a target id that is not a UUID, on the field", async () => {
    renderView({});
    await userEvent.click(await screen.findByRole("button", { name: "Links" }));
    await userEvent.type(await screen.findByPlaceholderText(/00000000/), "the printer");
    await userEvent.click(screen.getByRole("button", { name: "Link" }));
    expect(await screen.findByText(/Target id must be a UUID/)).toBeInTheDocument();
  });

  it("offers exactly the shipped link vocabulary, and not `ticket` (§0.7, §15.5)", async () => {
    renderView({});
    await userEvent.click(await screen.findByRole("button", { name: "Links" }));
    const select = (await screen.findByPlaceholderText(/00000000/))
      .closest("form")
      ?.querySelector("select");
    expect([...(select?.options ?? [])].map((o) => o.value)).toEqual([
      "device",
      "device_group",
      "credential",
      "record",
      "ipam_subnet",
      "ipam_address",
    ]);
  });
});

describe("labels", () => {
  it("names the sniffed type rather than echoing a MIME at an operator", () => {
    expect(typeLabel("application/pdf")).toBe("PDF");
    expect(typeLabel("text/plain")).toBe("Text");
    expect(
      typeLabel("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ).toBe("Word");
  });

  it("falls back to the raw type rather than to a guess", () => {
    // A type this console has not heard of is a server ahead of it. Printing
    // the type is honest; inventing a friendly name for it is not.
    expect(typeLabel("application/x-newthing")).toBe("application/x-newthing");
  });

  it("prints a real byte count when the formatter declines to guess", () => {
    expect(sizeLabel(2_202_010)).toBe("2.1 MiB");
    expect(sizeLabel(0)).toBe("0 B");
  });
});

describe("upload refusals — every cause is a sentence on the screen (§11.7)", () => {
  async function openUpload(uploadDocumentVersion: ApiClient["uploadDocumentVersion"]) {
    renderPage({
      createDocument: vi.fn().mockResolvedValue({ ...DOC, id: "dNew", current: null }),
      uploadDocumentVersion,
    });
    await userEvent.click(await screen.findByRole("button", { name: "Upload document" }));
    const dialog = await screen.findByRole("dialog", { name: "Upload document" });
    await userEvent.type(within(dialog).getByLabelText("Title"), "Signed contract");
    return dialog;
  }

  /** A `File` of a stated size without allocating it — `size` is read-only. */
  function fileOf(name: string, bytes: number): File {
    const file = new File(["x"], name);
    Object.defineProperty(file, "size", { value: bytes });
    return file;
  }

  it("refuses a file over the per-file cap BEFORE sending it", async () => {
    // A courtesy, not the control: `reject_oversized_content_length` and then
    // `read_bounded_body` are what actually enforce this, and neither can be
    // routed around. What this buys is that an operator on a slow link learns
    // the answer before spending four minutes on the upload.
    const upload = vi.fn();
    const dialog = await openUpload(upload);
    await userEvent.upload(
      within(dialog).getByLabelText("File"),
      fileOf("scan.pdf", 30 * 1024 * 1024),
    );
    await userEvent.click(within(dialog).getByRole("button", { name: "Upload" }));
    expect(await screen.findByText(/The limit is 25\.0 MiB per file/)).toBeInTheDocument();
    expect(upload).not.toHaveBeenCalled();
  });

  it("shows the server's ACCEPTED LIST when the type is refused (415)", async () => {
    const dialog = await openUpload(
      vi.fn().mockRejectedValue(
        new ApiError(
          415,
          "unsupported file type. This library accepts: PDF; PNG, JPEG, GIF or WebP images; ZIP and OOXML (.docx/.xlsx/.pptx/.vsdx); legacy Office (.doc/.xls/.ppt/.vsd); and UTF-8 text of any kind, including Markdown, CSV, HTML, SVG and XML (stored and shown as source text). UTF-16 text is not accepted -- re-save it as UTF-8, or zip it.",
        ),
      ),
    );
    await userEvent.upload(within(dialog).getByLabelText("File"), fileOf("disk.iso", 10));
    await userEvent.click(within(dialog).getByRole("button", { name: "Upload" }));
    // The wall is DISCOVERABLE rather than mysterious: the message names what
    // is accepted, and the console prints the server's sentence rather than
    // inventing a shorter one that would drift from `filetypes.py`.
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/unsupported file type/);
    expect(alert).toHaveTextContent(/UTF-16 text is not accepted/);
  });

  it("shows the quota sentence when the organisation is full (507)", async () => {
    const dialog = await openUpload(
      vi
        .fn()
        .mockRejectedValue(
          new ApiError(507, "this organisation's document storage is full (5368709120 of 5368709120 bytes used). Archive and purge old versions, or ask an administrator to raise the quota."),
        ),
    );
    await userEvent.upload(within(dialog).getByLabelText("File"), fileOf("a.pdf", 10));
    await userEvent.click(within(dialog).getByRole("button", { name: "Upload" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/document storage is full/);
  });

  it("shows the filename refusal (400) rather than a silent failure", async () => {
    const dialog = await openUpload(
      vi.fn().mockRejectedValue(new ApiError(400, "filename contains characters that are not allowed")),
    );
    await userEvent.upload(within(dialog).getByLabelText("File"), fileOf("../../etc/passwd", 10));
    await userEvent.click(within(dialog).getByRole("button", { name: "Upload" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/not allowed/);
  });

  it("says the ROW was created when only the FILE was refused", async () => {
    // The two-step shape of §4.1 is visible exactly when the second step
    // fails. Swallowing this would leave a document the operator did not know
    // they had created and cannot explain.
    const dialog = await openUpload(vi.fn().mockRejectedValue(new ApiError(415, "unsupported file type.")));
    await userEvent.upload(within(dialog).getByLabelText("File"), fileOf("disk.iso", 10));
    await userEvent.click(within(dialog).getByRole("button", { name: "Upload" }));
    expect(
      await screen.findByText(/document was created and the file was not/),
    ).toBeInTheDocument();
  });

  it("names what the allow-list is NOT, so nobody reads it as malware scanning", async () => {
    // §4.3/§15.10: ZIP is on the list, so arbitrary bytes can be stored by
    // zipping them, deliberately. Describing the allow-list as a malware
    // control would be claiming a control that does not exist.
    renderPage({});
    await userEvent.click(await screen.findByRole("button", { name: "Upload document" }));
    expect(await screen.findByText(/not scanned for malware/)).toBeInTheDocument();
  });
});
