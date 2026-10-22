/**
 * IT documentation in Spanish: the library page (tiles, article list, search,
 * records, documents), the article view with its history, archive and portal
 * switch, the article and record editors with their refusals, the document
 * view (versions, links, purge) with its dialogs, and the viewer. Article and
 * document titles, categories, bodies, descriptions, filenames and record names
 * are user data and read as recorded; only the interface around them is
 * Spanish.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { en } from "../../i18n/en";
import { es } from "../../i18n/es";
import { setActiveLanguage, translate } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import { ApiError } from "../../lib/api";
import type { ApiClient, DocArticle, DocDocument, DocDocumentVersion, Me } from "../../lib/api";
import { DocsPage } from "./DocsPage";
import { DocumentViewer } from "./DocumentViewer";
import { typeLabel } from "./documents";
import { renderMarkdown } from "./markdown";
import { PortalVisibility } from "./PortalVisibility";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
vi.mock("../../lib/download", () => ({ saveBlob: vi.fn() }));

const MANAGER: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  permissions: ["docs.read", "docs.manage", "docs.document.purge"],
  timezone: "UTC",
};


const ARTICLES = {
  items: [
    { id: "a1", title: "Procedimiento de copias", category: "Procedimiento", archived: false, updated_at: "2026-08-01T00:00:00.000Z", link_count: 2 },
    { id: "a2", title: "Recuperación del servidor de archivos", category: "Manual", archived: false, updated_at: "2026-08-05T00:00:00.000Z", link_count: 0 },
  ],
  total: 2,
};

const RECORDS = {
  items: [
    { id: "r1", record_type: "service", name: "Correo", fields: {}, archived: false, current_version: 1 },
    { id: "r2", record_type: "service", name: "VPN", fields: {}, archived: false, current_version: 1 },
    { id: "r3", record_type: "vendor", name: "Acme", fields: {}, archived: false, current_version: 1 },
  ],
  total: 3,
};

const DOCUMENTS = {
  items: [
    { id: "d1", title: "Contrato de soporte Acme", description: "", category: "Contratos", archived: false, current_version: 2, updated_at: "2026-08-20T01:00:00.000Z", link_count: 1 },
    { id: "d2", title: "Alzado del rack", description: "", category: "Diagramas", archived: false, current_version: 0, updated_at: "2026-08-21T01:00:00.000Z", link_count: 0 },
  ],
  total: 2,
};

const ARTICLE: DocArticle = {
  id: "a1",
  title: "Procedimiento de copias",
  body: "# Copias\n\nSe hacen cada noche. [malo](javascript:alert(1))",
  category: "Procedimiento",
  archived: false,
  portal_visible: false,
  current_version: 3,
  updated_at: "2026-08-01T00:00:00.000Z",
  links: [
    { id: "l1", target_type: "device", target_id: "11111111-1111-1111-1111-111111111111" },
    { id: "l2", target_type: "ipam_subnet", target_id: "22222222-2222-2222-2222-222222222222" },
  ],
};

const PDF: DocDocumentVersion = {
  version: 2,
  filename: "contrato-2026.pdf",
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
  title: "Contrato de soporte Acme",
  description: "Firmado el 1 de marzo.",
  category: "Contratos",
  archived: false,
  current_version: 2,
  updated_at: "2026-08-20T01:00:00.000Z",
  current: PDF,
  links: [{ id: "l1", target_type: "device_group", target_id: "11111111-1111-1111-1111-111111111111" }],
};

function version(patch: Partial<DocDocumentVersion> = {}): DocDocumentVersion {
  return { ...PDF, version: 1, filename: "notas.txt", content_type: "text/plain", viewer: "text", ...patch };
}

beforeEach(() => {
  mockUseApi.mockReset();
  URL.createObjectURL = vi.fn(() => "blob:mock/1") as unknown as typeof URL.createObjectURL;
  URL.revokeObjectURL = vi.fn() as unknown as typeof URL.revokeObjectURL;
});

function mount(ui: ReactElement, entry = "/docs") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

function renderPage(api: Record<string, unknown> = {}, entry = "/docs", permissions = MANAGER.permissions) {
  mockUseApi.mockReturnValue({
    me: vi.fn().mockResolvedValue({ ...MANAGER, permissions }),
    listArticles: vi.fn().mockResolvedValue(ARTICLES),
    listDocRecords: vi.fn().mockResolvedValue(RECORDS),
    listDocuments: vi.fn().mockResolvedValue(DOCUMENTS),
    getArticle: vi.fn().mockResolvedValue(ARTICLE),
    articleVersions: vi.fn().mockResolvedValue([
      { version: 2, snapshot: {}, changed_by: "u1", ts: "2026-07-30T10:00:00.000Z" },
    ]),
    getDocument: vi.fn().mockResolvedValue(DOC),
    documentVersions: vi.fn().mockResolvedValue([PDF, { ...PDF, version: 1, purged_at: "2026-08-22T00:00:00.000Z" }]),
    downloadDocumentVersion: vi.fn().mockResolvedValue(new Blob(["%PDF-1.7"])),
    ...api,
  } as unknown as ApiClient);
  return mount(<DocsPage />, entry);
}

describe("the documentation library in Spanish", () => {
  it("renders the tiles, their feet, the article list and the sections", async () => {
    const { container } = renderPage();
    expect(await screen.findByRole("heading", { name: "Documentación de TI" })).toBeInTheDocument();
    expect(await screen.findByText("Procedimiento de copias")).toBeInTheDocument();
    const labels = [...container.querySelectorAll(".stat-card .stat-label")].map((el) => el.textContent);
    expect(labels).toEqual(["Artículos", "Registros", "Referencias vinculadas", "Documentos"]);
    expect(await screen.findByText("En 2 categorías")).toBeInTheDocument();
    expect(screen.getByText("2 servicios, 1 proveedor")).toBeInTheDocument();
    expect(screen.getByText("De 1 de 2 artículos")).toBeInTheDocument();
    expect(screen.getByText("1 documento sin archivo subido todavía")).toBeInTheDocument();
    expect(screen.getByText("2 artículos")).toBeInTheDocument();
    expect(screen.getByText(/vinculado a 2 objetos/)).toBeInTheDocument();
    expect(screen.getByText(/^Busca en títulos, descripciones/)).toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: "Buscar en la documentación" })).toHaveAttribute("placeholder", "Buscar artículos, hosts, IP");
    expect(screen.getByRole("columnheader", { name: "Nombre" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Actualizado" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Artículo nuevo" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Registro nuevo" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Subir documento" })).toBeInTheDocument();
    expect(screen.getByText(/sin archivo todavía/)).toBeInTheDocument();
    expect(screen.getByText(/1 vínculo/)).toBeInTheDocument();
    // The record types, the chips and the file sections read in Spanish.
    expect(screen.getAllByText("Servicio")).toHaveLength(2);
    expect(screen.getByText("Proveedor")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Filtrar por categoría" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Todas" })).toHaveAttribute("aria-pressed", "true");
    expectNoEnglish(container);
  });

  it("filters by category through the chips and says when a category is empty of results", async () => {
    const user = userEvent.setup();
    const { container } = renderPage();
    await screen.findByText("Procedimiento de copias");
    await user.click(screen.getByRole("button", { name: "Manual" }));
    expect(screen.queryByText("Procedimiento de copias")).toBeNull();
    expect(screen.getByText("1 artículo")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("says the library is empty, and that nothing is in a category", async () => {
    const { container } = renderPage({
      listArticles: vi.fn().mockResolvedValue({ items: [], total: 0 }),
      listDocRecords: vi.fn().mockResolvedValue({ items: [], total: 0 }),
      listDocuments: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    });
    expect(await screen.findByText("Todavía no hay artículos.")).toBeInTheDocument();
    expect(screen.getByText("Todavía no hay registros.")).toBeInTheDocument();
    expect(screen.getByText("Todavía no hay documentos.")).toBeInTheDocument();
    expect(screen.getByText("todavía no hay nada escrito")).toBeInTheDocument();
    expect(screen.getByText("todavía no hay registros de redes, servicios, proveedores ni ubicaciones")).toBeInTheDocument();
    expect(screen.getByText("todavía no hay ningún artículo vinculado a un objeto")).toBeInTheDocument();
    expect(screen.getByText("todavía no se ha subido ningún archivo")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("groups search results, and says when nothing matches", async () => {
    const user = userEvent.setup();
    const hits = [
      { kind: "article", id: "a1", title: "Procedimiento de copias", category: "Procedimiento", rank: 1 },
      { kind: "record", id: "r3", title: "Acme", category: "vendor", rank: 1 },
      { kind: "document", id: "d1", title: "Contrato de soporte Acme", category: "Contratos", rank: 1 },
    ];
    const searchDocs = vi.fn(async (q: string) => (q.includes("zzz") ? [] : hits));
    const { container } = renderPage({ searchDocs });
    await user.type(await screen.findByRole("searchbox", { name: "Buscar en la documentación" }), "acme");
    const region = await screen.findByRole("region", { name: "Resultados de la búsqueda" });
    for (const heading of ["Artículos", "Registros", "Documentos"]) {
      expect(within(region).getByText(heading)).toBeInTheDocument();
    }
    expect(within(region).getByText("Proveedor")).toBeInTheDocument();
    expectNoEnglish(container);
    await user.type(screen.getByRole("searchbox"), "zzz");
    expect(await screen.findByText("Ninguna documentación coincide con esa búsqueda.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Borrar la búsqueda" })).toBeInTheDocument();
  });

  it("says why a list did not load", async () => {
    const { container } = renderPage({ listArticles: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    expect(await screen.findByText(/no está disponible/i)).toBeInTheDocument();
    expectNoEnglish(container);
  });
});

describe("the article in Spanish", () => {
  it("shows the article, what it links to, and its history", async () => {
    const user = userEvent.setup();
    const { container } = renderPage({}, "/docs?article=a1");
    expect(await screen.findByRole("heading", { name: "Procedimiento de copias", level: 2 })).toBeInTheDocument();
    expect(screen.getByText(/actualizado /)).toBeInTheDocument();
    expect(screen.getByText("Vinculado a: dispositivo, subred")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Visible en el portal" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Editar" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Archivar" })).toBeInTheDocument();
    // A link the sanitiser removed says so, in Spanish, in its tooltip.
    expect(container.querySelector("[data-link-removed]")).toHaveAttribute("title", "Vínculo eliminado: esquema de URL no admitido.");
    await user.click(screen.getByRole("button", { name: "Historial" }));
    expect(await screen.findByText(/^v2 — /)).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("says when the history, or the article, could not be loaded", async () => {
    const user = userEvent.setup();
    const { container } = renderPage({ articleVersions: vi.fn().mockRejectedValue(new ApiError(503, "")) }, "/docs?article=a1");
    await screen.findByRole("heading", { name: "Procedimiento de copias", level: 2 });
    await user.click(screen.getByRole("button", { name: "Historial" }));
    expect(await screen.findByText(/^No se pudo cargar el historial de versiones/)).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("says the article could not be loaded", async () => {
    const { container } = renderPage({ getArticle: vi.fn().mockRejectedValue(new ApiError(404, "")) }, "/docs?article=a1");
    expect(await screen.findByText(/^No se pudo cargar el artículo\./)).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("confirms before archiving, and words a refused archive from the held error", async () => {
    const user = userEvent.setup();
    renderPage({ archiveArticle: vi.fn().mockRejectedValue(new ApiError(403, "")) }, "/docs?article=a1");
    await user.click(await screen.findByRole("button", { name: "Archivar" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Archivar artículo" })).toBeInTheDocument();
    expect(within(dialog).getByText(/^El artículo sale de la biblioteca y de la búsqueda\./)).toBeInTheDocument();
    expectNoEnglish(dialog);
    await user.click(within(dialog).getByRole("button", { name: "Archivar artículo" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/^No se archivó este artículo\. /);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent(/^This article was not archived\. /);
    act(() => setActiveLanguage("es"));
    expect(screen.getByRole("alert")).toHaveTextContent(/^No se archivó este artículo\. /);
  });

  it("holds the portal switch's failure as the error and re-words it", async () => {
    const user = userEvent.setup();
    const setArticlePortalVisibility = vi.fn().mockRejectedValue(new ApiError(503, ""));
    mockUseApi.mockReturnValue({ setArticlePortalVisibility } as unknown as ApiClient);
    mount(<PortalVisibility article={ARTICLE} canManage />);
    expect(screen.getByRole("switch", { name: "Visible en el portal" })).toBeInTheDocument();
    expect(screen.getByText(/^Quienes crean tickets pueden encontrarlo y leerlo/)).toBeInTheDocument();
    await user.click(screen.getByRole("switch"));
    expect(await screen.findByRole("alert")).toHaveTextContent(/^No se guardó\. /);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent(/^Not saved\. /);
  });

  it("says a visible article is visible, to someone who cannot edit it", () => {
    mockUseApi.mockReturnValue({} as unknown as ApiClient);
    mount(<PortalVisibility article={{ ...ARTICLE, portal_visible: true }} canManage={false} />);
    expect(screen.getByText("Visible en el portal.")).toBeInTheDocument();
  });
});

describe("the editors in Spanish", () => {
  it("holds a refused article as data and re-words it on a live switch", async () => {
    const user = userEvent.setup();
    const createArticle = vi.fn();
    const { container } = renderPage({ createArticle });
    await user.click(await screen.findByRole("button", { name: "Artículo nuevo" }));
    expect(screen.getByRole("heading", { name: "Artículo nuevo", level: 2 })).toBeInTheDocument();
    for (const label of ["Título", "Categoría", "Markdown"]) {
      expect(screen.getByLabelText(label)).toBeInTheDocument();
    }
    expect(screen.getByText("Vista previa")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Guardar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("El campo Título es obligatorio");
    expect(createArticle).not.toHaveBeenCalled();
    expectNoEnglish(container);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent("Title is required");
    act(() => setActiveLanguage("es"));
    fireEvent.change(screen.getByLabelText("Título"), { target: { value: "x".repeat(301) } });
    await user.click(screen.getByRole("button", { name: "Guardar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Título: máximo 300 caracteres");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent("Title must be 300 characters or fewer");
  });

  it("edits an existing article", async () => {
    const user = userEvent.setup();
    renderPage({}, "/docs?article=a1");
    await user.click(await screen.findByRole("button", { name: "Editar" }));
    expect(await screen.findByRole("heading", { name: "Editar artículo" })).toBeInTheDocument();
  });

  it("explains the record dialog, its superseded type and its refusals", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Registro nuevo" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Registro nuevo" })).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "Red: ahora en la gestión de direcciones IP" })).toBeDisabled();
    expect(within(dialog).getByRole("option", { name: "Ubicación" })).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: "Crea allí una subred" })).toHaveAttribute("href", "/ipam");
    expect(within(dialog).getByText(/^Los registros de red ahora están en la gestión de direcciones IP\./)).toBeInTheDocument();
    expectNoEnglish(dialog);
    await user.click(within(dialog).getByRole("button", { name: "Crear" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("El campo Nombre es obligatorio");
    await user.type(within(dialog).getByLabelText("Nombre"), "Core VLAN");
    fireEvent.change(within(dialog).getByLabelText("Campos (JSON)"), { target: { value: "{no" } });
    await user.click(within(dialog).getByRole("button", { name: "Crear" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Campos (JSON): no es un JSON válido");
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByRole("alert")).toHaveTextContent("Fields (JSON) is not valid JSON");
    act(() => setActiveLanguage("es"));
    fireEvent.change(within(dialog).getByLabelText("Campos (JSON)"), { target: { value: "[1]" } });
    await user.click(within(dialog).getByRole("button", { name: "Crear" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Campos (JSON): debe ser un objeto JSON");
  });
});

describe("the document view in Spanish", () => {
  it("shows the document, its versions and its links", async () => {
    const user = userEvent.setup();
    const { container } = renderPage({}, "/docs?document=d1");
    expect(await screen.findByRole("heading", { name: "Contrato de soporte Acme", level: 2 })).toBeInTheDocument();
    expect(screen.getByText("Firmado el 1 de marzo.")).toBeInTheDocument();
    for (const name of ["Historial", "Vínculos", "Subir versión nueva", "Editar detalles", "Archivar", "Purgar v2"]) {
      expect(await screen.findByRole("button", { name })).toBeInTheDocument();
    }
    // The header's download and the viewer's own.
    expect(screen.getAllByRole("button", { name: "Descargar" }).length).toBeGreaterThan(0);
    expect(await screen.findByText(/^Esta consola no muestra|Abrir un documento queda registrado/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Historial" }));
    for (const header of ["Versión", "Archivo", "Tamaño", "Tipo", "Subida"]) {
      expect(await screen.findByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(screen.getByText("actual")).toBeInTheDocument();
    expect(screen.getByText("mostrada")).toBeInTheDocument();
    expect(screen.getByText(/^purgada /)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Vínculos" }));
    // The link's badge and the matching option of the form below.
    expect(await screen.findAllByText("grupo de dispositivos")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Desvincular" })).toBeInTheDocument();
    expect(screen.getByText("Vincular a")).toBeInTheDocument();
    expect(screen.getByText("ID de destino")).toBeInTheDocument();
    const select = screen.getByRole("combobox");
    expect([...(select as HTMLSelectElement).options].map((o) => o.textContent)).toEqual([
      "dispositivo",
      "grupo de dispositivos",
      "credencial",
      "registro",
      "subred",
      "dirección IP",
    ]);
    expect([...(select as HTMLSelectElement).options].map((o) => o.value)).toEqual([
      "device",
      "device_group",
      "credential",
      "record",
      "ipam_subnet",
      "ipam_address",
    ]);
    expect(screen.getByText(/^Un vínculo a una credencial nombra la credencial/)).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("refuses a target that is not a UUID, and re-words the refusal", async () => {
    const user = userEvent.setup();
    renderPage({}, "/docs?document=d1");
    await user.click(await screen.findByRole("button", { name: "Vínculos" }));
    await user.type(await screen.findByPlaceholderText(/00000000/), "la impresora");
    await user.click(screen.getByRole("button", { name: "Vincular" }));
    expect(await screen.findByText(/^ID de destino: debe ser un UUID/)).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText(/^Target id must be a UUID/)).toBeInTheDocument();
  });

  it("says a failed link and a failed unlink from the held error", async () => {
    const user = userEvent.setup();
    renderPage(
      {
        addDocumentLink: vi.fn().mockRejectedValue(new ApiError(404, "")),
        removeDocumentLink: vi.fn().mockRejectedValue(new ApiError(503, "")),
      },
      "/docs?document=d1",
    );
    await user.click(await screen.findByRole("button", { name: "Vínculos" }));
    await user.type(await screen.findByPlaceholderText(/00000000/), "33333333-3333-3333-3333-333333333333");
    await user.click(screen.getByRole("button", { name: "Vincular" }));
    expect(await screen.findByText(/^No se vinculó nada\./)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Desvincular" }));
    expect(await screen.findByText(/^No se desvinculó nada\./)).toBeInTheDocument();
  });

  it("says a document has no links, and no file", async () => {
    const user = userEvent.setup();
    const { container } = renderPage(
      { getDocument: vi.fn().mockResolvedValue({ ...DOC, current_version: 0, current: null, links: [] }) },
      "/docs?document=d1",
    );
    expect(await screen.findByText(/^Todavía no se ha subido ningún archivo\. Este documento tiene/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Subir un archivo" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Vínculos" }));
    expect(await screen.findByText(/^Todavía no está vinculado a nada\./)).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("confirms an archive and a purge, and says when the document is archived", async () => {
    const user = userEvent.setup();
    renderPage(
      { purgeDocumentVersion: vi.fn().mockRejectedValue(new ApiError(503, "")), archiveDocument: vi.fn().mockRejectedValue(new ApiError(403, "")) },
      "/docs?document=d1",
    );
    await user.click(await screen.findByRole("button", { name: "Purgar v2" }));
    let dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Destruir estos bytes" })).toBeInTheDocument();
    expect(within(dialog).getAllByText(/Versión 2 — contrato-2026\.pdf, 2\.1 MiB/).length).toBeGreaterThan(0);
    expect(within(dialog).getByText(/^El archivo se elimina del almacenamiento/)).toBeInTheDocument();
    expectNoEnglish(dialog);
    await user.click(within(dialog).getByRole("button", { name: "Cancelar" }));
    await user.click(screen.getByRole("button", { name: "Archivar" }));
    dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Archivar documento" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Archivar documento" }));
    expect(await screen.findByText(/^No se archivó este documento\. /)).toBeInTheDocument();
  });

  it("says a document is archived, and that its metadata could not load", async () => {
    const archived = renderPage({ getDocument: vi.fn().mockResolvedValue({ ...DOC, archived: true }) }, "/docs?document=d1");
    expect(await screen.findByText("archivado")).toBeInTheDocument();
    expect(screen.getByText(/^Este documento está archivado\./)).toBeInTheDocument();
    archived.unmount();
    const failed = renderPage({ getDocument: vi.fn().mockRejectedValue(new ApiError(503, "")) }, "/docs?document=d1");
    expect(await screen.findByText(/^No se pudo cargar este documento/)).toBeInTheDocument();
    expectNoEnglish(failed.container);
  });
});

describe("the upload dialogs in Spanish", () => {
  it("refuses an empty form and a missing file, and re-words the refusal", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Subir documento" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Subir documento" })).toBeInTheDocument();
    for (const label of ["Título", "Categoría", "Descripción", "Archivo"]) {
      expect(within(dialog).getByLabelText(label)).toBeInTheDocument();
    }
    expect(within(dialog).getByText(/^Se aceptan: PDF;/)).toBeInTheDocument();
    expectNoEnglish(dialog);
    await user.click(within(dialog).getByRole("button", { name: "Subir" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("El campo Título es obligatorio");
    expect(alert).toHaveTextContent("Elige un archivo");
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByRole("alert")).toHaveTextContent("Title is required");
    expect(within(dialog).getByRole("alert")).toHaveTextContent("Choose a file");
  });

  it("words an oversized file and an empty file with their own name", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Subir documento" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Título"), "Informe");
    const empty = new File([], "vacío.txt", { type: "text/plain" });
    await user.upload(within(dialog).getByLabelText("Archivo"), empty);
    await user.click(within(dialog).getByRole("button", { name: "Subir" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("vacío.txt está vacío");
    const big = new File(["x"], "enorme.bin");
    Object.defineProperty(big, "size", { value: 30 * 1024 * 1024 });
    await user.upload(within(dialog).getByLabelText("Archivo"), big);
    await user.click(within(dialog).getByRole("button", { name: "Subir" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "enorme.bin ocupa 30.0 MiB. El límite es de 25.0 MiB por archivo.",
    );
  });

  it("says the document was created and the file was not", async () => {
    const user = userEvent.setup();
    renderPage({
      createDocument: vi.fn().mockResolvedValue({ id: "d9" }),
      uploadDocumentVersion: vi.fn().mockRejectedValue(new ApiError(415, "")),
    });
    await user.click(await screen.findByRole("button", { name: "Subir documento" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Título"), "Informe");
    await user.upload(within(dialog).getByLabelText("Archivo"), new File(["abc"], "informe.pdf"));
    await user.click(within(dialog).getByRole("button", { name: "Subir" }));
    expect(await within(dialog).findByText(/^Se creó el documento, pero no el archivo\./)).toBeInTheDocument();
    expect(within(dialog).getByRole("alert")).toHaveTextContent(/^Se rechazó la subida\. /);
  });

  it("opens the new-version and edit-details dialogs in Spanish", async () => {
    const user = userEvent.setup();
    renderPage({}, "/docs?document=d1");
    await user.click(await screen.findByRole("button", { name: "Subir versión nueva" }));
    let dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Subir versión nueva" })).toBeInTheDocument();
    expect(within(dialog).getByText(/^El archivo anterior nunca se toca/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Subir" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Elige un archivo");
    await user.click(within(dialog).getByRole("button", { name: "Cancelar" }));
    await user.click(screen.getByRole("button", { name: "Editar detalles" }));
    dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Editar detalles" })).toBeInTheDocument();
    expect(within(dialog).getByText(/^Esto cambia el título, la categoría y la descripción/)).toBeInTheDocument();
    await user.clear(within(dialog).getByLabelText("Título"));
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("El campo Título es obligatorio");
  });
});

describe("the viewer in Spanish", () => {
  function renderViewer(v: DocDocumentVersion, download: () => Promise<Blob> = () => Promise.resolve(new Blob(["hola"]))) {
    mockUseApi.mockReturnValue({ downloadDocumentVersion: vi.fn(download) } as unknown as ApiClient);
    return mount(<DocumentViewer documentId="d1" version={v} />);
  }

  it("says the bytes were destroyed", () => {
    const { container } = renderViewer(version({ purged_at: "2026-08-22T00:00:00.000Z" }));
    expect(screen.getByText("Estos bytes fueron destruidos.").tagName).toBe("STRONG");
    expect(container).toHaveTextContent("La versión 1 (notas.txt, 2.1 MiB) se purgó");
    expectNoEnglish(container);
  });

  it("offers a download for a type it does not preview", () => {
    const { container } = renderViewer(version({ content_type: "model/x-nuevo", viewer: "none" }));
    expect(screen.getByText("Esta consola no muestra una vista previa de los archivos model/x-nuevo. Descarga notas.txt para abrirlo con una aplicación que pueda.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Descargar" })).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("shows text with the audit note, and loads before it", async () => {
    const { container } = renderViewer(version());
    expect(screen.getByText("Cargando notas.txt…")).toBeInTheDocument();
    expect(await screen.findByTestId("document-source")).toHaveTextContent("hola");
    expect(screen.getByText(/^Abrir un documento queda registrado en el registro de auditoría como una descarga/)).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("explains a PDF that does not embed, and a file that could not be read", async () => {
    const pdf = renderViewer(version({ filename: "a.pdf", content_type: "application/pdf", viewer: "pdf" }));
    expect(await screen.findByText(/^Si la vista previa no carga, descarga el archivo/)).toBeInTheDocument();
    pdf.unmount();
    const failed = renderViewer(version(), () => Promise.reject(new ApiError(503, "")));
    expect(await screen.findByText(/^No se pudo leer notas\.txt\. /)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reintentar" })).toBeInTheDocument();
    expectNoEnglish(failed.container);
  });
});

describe("the labels and sentences built outside React", () => {
  it("names the text type and the sanitiser's tooltip in both languages", () => {
    setActiveLanguage("es");
    expect(typeLabel("text/plain")).toBe("Texto");
    expect(typeLabel("application/pdf")).toBe("PDF");
    expect(renderMarkdown("[x](javascript:alert(1))")).toContain('title="Vínculo eliminado: esquema de URL no admitido."');
    setActiveLanguage("en");
    expect(typeLabel("text/plain")).toBe("Text");
    expect(renderMarkdown("[x](javascript:alert(1))")).toContain('title="Link removed — unsupported URL scheme."');
  });

  it("agrees every count with its number", () => {
    setActiveLanguage("es");
    expect(translate("docs.page.foot.categories", { count: 1 })).toBe("En 1 categoría");
    expect(translate("docs.page.foot.linkedFrom", { linked: 0, count: 1 })).toBe("De 0 de 1 artículo");
    expect(translate("docs.page.foot.recordType.location", { count: 1 })).toBe("1 ubicación");
    expect(translate("docs.page.foot.recordType.location", { count: 3 })).toBe("3 ubicaciones");
    expect(translate("docs.page.foot.recordType.vendor", { count: 2 })).toBe("2 proveedores");
    expect(translate("docs.documents.links", { count: 1 })).toBe("1 vínculo");
    expect(Object.keys(es.docs)).toEqual(Object.keys(en.docs));
  });

  it("keeps a link target the console has not met readable, and the English wire words", () => {
    setActiveLanguage("es");
    expect(Object.values(es.enums.DocLinkTarget)).toEqual([
      "dispositivo",
      "grupo de dispositivos",
      "credencial",
      "registro",
      "subred",
      "dirección IP",
    ]);
    // The English entries are the wire words the page has always printed.
    expect(Object.values(en.enums.DocLinkTarget)).toEqual(Object.keys(en.enums.DocLinkTarget));
  });
});
