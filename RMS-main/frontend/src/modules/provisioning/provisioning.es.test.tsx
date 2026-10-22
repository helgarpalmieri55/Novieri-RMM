/**
 * Provisioning and imaging in Spanish: the runs page (tiles, filter, table and
 * the Provision dialog with its refusal and its confirmation), the images page
 * and its New image dialog, the profiles page and its editor, a device's own
 * Imaging tab, the prerequisites panel and the labels the module builds
 * outside React. Profile, image and device names, MAC addresses and the
 * server's error text read as recorded; the words around them are Spanish.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage, translate } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import type { ApiClient, Me } from "../../lib/api";
import { expectCta, meWith } from "../../test/prerequisiteHarness";
import { DeviceProvisioningTab } from "./DeviceProvisioningTab";
import { ImagesPage } from "./ImagesPage";
import {
  IMAGE_PICKER_EMPTY_HINT,
  imagePickerEmptyHint,
  imagePickerOptions,
  osImageStateLabel,
  profilePickerEmptyHint,
  profilePickerOptions,
} from "./pickerOptions";
import { ProfilesPage } from "./ProfilesPage";
import { PROVISIONING_NAV_ITEMS } from "./provisioningNav";
import { provisionRunStateLabel } from "./provisioningState";
import { ProvisioningPrerequisites } from "./ProvisioningPrerequisites";
import { ProvisioningRunsPage } from "./ProvisioningRunsPage";
import type { FleetProvisioningReport, OsImage, ProvisionProfile, ProvisionRun } from "./types";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function renderPage(ui: ReactElement, path = "/provisioning") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  permissions: ["provisioning.read", "provisioning.run", "provisioning.manage", "agent.release.read"],
};

const RELEASE = { id: "r1", version: "1.0.0", os: "windows", arch: "amd64", published_at: "2026-09-01T00:00:00Z", has_artifact: true };

function run(over: Partial<ProvisionRun> = {}): ProvisionRun {
  return {
    id: "run1",
    profile_id: "profile1",
    device_id: null,
    target_ref: "AA:BB:CC:DD:EE:FF",
    state: "awaiting_enrollment",
    answer_ref: null,
    started_at: "2026-01-02T00:00:00.000Z",
    finished_at: null,
    error: null,
    ...over,
  };
}

const RUNS: ProvisionRun[] = [
  run(),
  run({ id: "run2", state: "pending", target_ref: null, device_id: "dev-recepcion" }),
  run({
    id: "run3",
    state: "complete",
    started_at: "2026-01-02T00:00:00.000Z",
    finished_at: "2026-01-02T00:12:00.000Z",
  }),
  run({ id: "run4", state: "enrolled" }),
  run({ id: "run5", state: "failed", error: "El disco no responde" }),
];

function profile(over: Partial<ProvisionProfile> = {}): ProvisionProfile {
  return {
    id: "profile1",
    name: "estación-estándar",
    image_id: "image1",
    post_config: {},
    target_scope: {},
    enabled: true,
    created_at: "2026-01-01T00:00:00.000Z",
    has_domain_join_credential: true,
    has_local_admin_credential: false,
    ...over,
  };
}

const PROFILES: ProvisionProfile[] = [
  profile(),
  profile({ id: "profile2", name: "quiosco-recepción", enabled: false, has_domain_join_credential: false, has_local_admin_credential: true }),
];

function image(over: Partial<OsImage> = {}): OsImage {
  return {
    id: "image1",
    os_family: "windows",
    name: "Windows 11 Estándar",
    kind: "install",
    artifact_ref: "s3://images/win11",
    artifact_sha256: "deadbeefcafe0123456789",
    params: {},
    state: "active",
    created_at: "2026-01-01T00:00:00.000Z",
    ...over,
  };
}

const IMAGES: OsImage[] = [
  image(),
  image({ id: "image2", name: "Ubuntu servidor", os_family: "linux", kind: "build", artifact_sha256: null, state: "draft" }),
  image({ id: "image3", name: "Respuestas 24H2", kind: "answer_file", state: "active" }),
];

const EMPTY_REPORT: FleetProvisioningReport = {
  run_counts_by_state: {},
  success_rate: 0,
  machines_provisioned: 0,
  per_profile: {},
};
const REPORT: FleetProvisioningReport = {
  run_counts_by_state: { complete: 1, failed: 1 },
  success_rate: 0.5,
  machines_provisioned: 1,
  per_profile: {},
};

function baseApi(over: Record<string, unknown> = {}) {
  const api = {
    me: vi.fn().mockResolvedValue(ME),
    listProvisionRuns: vi.fn().mockResolvedValue(RUNS),
    listProvisionProfiles: vi.fn().mockResolvedValue(PROFILES),
    listOsImages: vi.fn().mockResolvedValue(IMAGES),
    getProvisioningReport: vi.fn().mockResolvedValue(REPORT),
    startProvisionRun: vi.fn().mockResolvedValue({ run_id: "run9", state: "pending" }),
    createOsImage: vi.fn().mockResolvedValue(IMAGES[0]),
    createProvisionProfile: vi.fn().mockResolvedValue(PROFILES[0]),
    updateProvisionProfile: vi.fn().mockResolvedValue(PROFILES[0]),
    getDeviceProvisioning: vi.fn().mockResolvedValue({
      device_id: "dev1",
      profile_id: "profile1",
      image_id: "image1",
      enrolled_at: "2026-01-03T10:00:00.000Z",
      latest_run_state: "complete",
    }),
    listAgentReleases: vi.fn().mockResolvedValue([RELEASE]),
    ...over,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

beforeEach(() => {
  baseApi();
});

// -------------------------------------------------------------------- runs

describe("the provisioning runs page, in Spanish", () => {
  it("words the head, the tabs, the tiles, the filter and the table", async () => {
    const { container } = renderPage(<ProvisioningRunsPage />);
    expect(await screen.findByRole("heading", { name: "Aprovisionamiento e imágenes de disco" })).toBeInTheDocument();
    await screen.findByText("run1");
    expect(screen.getByRole("button", { name: "Aprovisionar" })).toBeInTheDocument();
    expect(screen.getByRole("tablist", { name: "Aprovisionamiento" })).toBeInTheDocument();
    for (const tab of ["Ejecuciones", "Imágenes", "Perfiles"]) {
      expect(screen.getByRole("tab", { name: tab })).toBeInTheDocument();
    }
    // The tiles.
    expect(screen.getByText("Tasa de éxito")).toBeInTheDocument();
    expect(screen.getByText("50 %")).toBeInTheDocument();
    expect(screen.getByText("Equipos aprovisionados")).toBeInTheDocument();
    expect(screen.getByText("Ejecuciones fallidas")).toBeInTheDocument();
    expect(screen.getByText("Mediana del tiempo de construcción")).toBeInTheDocument();
    expect(screen.getByText("12 min")).toBeInTheDocument();
    // The filter.
    const filter = screen.getByRole("combobox", { name: "Filtrar las ejecuciones por estado" });
    expect(within(filter).getByRole("option", { name: "Todas las ejecuciones" })).toBeInTheDocument();
    for (const state of ["Pendiente", "Esperando inscripción", "Inscrita", "Completada", "Fallida"]) {
      expect(within(filter).getByRole("option", { name: state })).toBeInTheDocument();
    }
    // The table.
    for (const header of ["Destino", "Perfil", "Estado", "Iniciada", "Finalizada", "Error"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    const row = (id: string) => screen.getByText(id).closest("tr") as HTMLElement;
    expect(within(row("run1")).getByText("Esperando inscripción")).toBeInTheDocument();
    expect(within(row("run1")).getByText("estación-estándar")).toBeInTheDocument();
    expect(within(row("run2")).getByText("Pendiente")).toBeInTheDocument();
    expect(within(row("run3")).getByText("Completada")).toBeInTheDocument();
    expect(within(row("run4")).getByText("Inscrita")).toBeInTheDocument();
    expect(within(row("run5")).getByText("Fallida")).toBeInTheDocument();
    expect(within(row("run5")).getByText("El disco no responde")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("says a fleet that never built anything has never run, and what each zero means", async () => {
    baseApi({ getProvisioningReport: vi.fn().mockResolvedValue(EMPTY_REPORT), listProvisionRuns: vi.fn().mockResolvedValue([]) });
    const { container } = renderPage(<ProvisioningRunsPage />);
    expect(await screen.findByText("Todavía no hay ejecuciones de aprovisionamiento.")).toBeInTheDocument();
    expect(screen.getByText("Nunca ejecutado")).toBeInTheDocument();
    expect(screen.getByText("Todavía no se ha aprovisionado ningún equipo")).toBeInTheDocument();
    expect(screen.getByText("Nunca se ha ejecutado ninguna construcción")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("says no run matches a filter, and that no build failed", async () => {
    const user = userEvent.setup();
    baseApi({
      getProvisioningReport: vi.fn().mockResolvedValue({ ...REPORT, run_counts_by_state: { complete: 2 } }),
      listProvisionRuns: vi.fn().mockResolvedValue([run()]),
    });
    const { container } = renderPage(<ProvisioningRunsPage />);
    await screen.findByText("run1");
    expect(screen.getByText("Ninguna construcción ha fallado")).toBeInTheDocument();
    await user.selectOptions(screen.getByRole("combobox", { name: "Filtrar las ejecuciones por estado" }), "failed");
    expect(await screen.findByText("Ninguna ejecución coincide con ese filtro.")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("says the prerequisites panel in Spanish while no agent package is published", async () => {
    baseApi({ listAgentReleases: vi.fn().mockResolvedValue([]) });
    const { container } = renderPage(<ProvisioningRunsPage />);
    expect(await screen.findByText("Antes de que un equipo construido pueda unirse")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Obtener el instalador del agente" })).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("re-words the page, the filter and the rows when the language is switched", async () => {
    renderPage(<ProvisioningRunsPage />);
    await screen.findByText("run1");
    expect(screen.getByText("Tasa de éxito")).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("heading", { name: "Provisioning & Imaging" })).toBeInTheDocument();
    expect(screen.getByText("Success rate")).toBeInTheDocument();
    expect(screen.getByText("50%")).toBeInTheDocument();
    expect(screen.getByText("12m")).toBeInTheDocument();
    const filter = screen.getByRole("combobox", { name: "Filter runs by state" });
    expect(within(filter).getByRole("option", { name: "Awaiting enrollment" })).toBeInTheDocument();
    expect(within(screen.getByText("run5").closest("tr") as HTMLElement).getByText("Failed")).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(screen.getByText("Tasa de éxito")).toBeInTheDocument();
  });
});

describe("the Provision dialog, in Spanish", () => {
  async function open(user: ReturnType<typeof userEvent.setup>) {
    renderPage(<ProvisioningRunsPage />);
    await screen.findByText("run1");
    await user.click(screen.getByRole("button", { name: "Aprovisionar" }));
    return screen.findByRole("dialog", { name: "Aprovisionar un dispositivo" });
  }

  it("names the missing profile on submit, and re-words the refusal on a switch", async () => {
    const user = userEvent.setup();
    const dialog = await open(user);
    expect(within(dialog).getByText("Referencia del destino (MAC / etiqueta de activo)")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Iniciar" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("El campo Perfil es obligatorio.");
    expectNoEnglish(dialog);
    act(() => setActiveLanguage("en"));
    expect(within(screen.getByRole("dialog", { name: "Provision a device" })).getByRole("alert")).toHaveTextContent(
      "Profile is required",
    );
  });

  it("asks before starting a build, with the profile, the target and the consequences", async () => {
    const user = userEvent.setup();
    const dialog = await open(user);
    await user.click(within(dialog).getByRole("combobox", { name: "Perfil" }));
    await user.click(await screen.findByRole("option", { name: /estación-estándar/ }));
    await user.type(within(dialog).getByRole("textbox", { name: /Referencia del destino/ }), "AA:BB:CC:00:11:22");
    await user.click(within(dialog).getByRole("button", { name: "Iniciar" }));
    const confirm = await screen.findByRole("dialog", { name: "Iniciar una ejecución de aprovisionamiento" });
    expect(confirm).toHaveTextContent("estación-estándar");
    expect(confirm).toHaveTextContent("Windows 11 Estándar · habilitado se instalará en el equipo en AA:BB:CC:00:11:22.");
    expect(confirm).toHaveTextContent(
      "El aprovisionamiento sustituye el contenido del disco del equipo; todo lo que no tenga copia de seguridad se pierde.",
    );
    expect(confirm).toHaveTextContent("Una ejecución iniciada no se puede cancelar desde la consola.");
    expect(within(confirm).getByRole("button", { name: "Iniciar la construcción" })).toBeInTheDocument();
    expectNoEnglish(confirm);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("dialog", { name: "Start provisioning run" })).toHaveTextContent(
      "Windows 11 Estándar · enabled will be laid down on the machine at AA:BB:CC:00:11:22.",
    );
  });

  it("says the machine that claims the build when no target is given", async () => {
    const user = userEvent.setup();
    const dialog = await open(user);
    await user.click(within(dialog).getByRole("combobox", { name: "Perfil" }));
    await user.click(await screen.findByRole("option", { name: /estación-estándar/ }));
    await user.click(within(dialog).getByRole("button", { name: "Iniciar" }));
    const confirm = await screen.findByRole("dialog", { name: "Iniciar una ejecución de aprovisionamiento" });
    expect(confirm).toHaveTextContent("se instalará en el equipo que reclame esta construcción.");
    expectNoEnglish(confirm);
  });

  it("explains the dead end when there are no profiles, and links to the Profiles tab", async () => {
    const user = userEvent.setup();
    baseApi({ listProvisionProfiles: vi.fn().mockResolvedValue([]) });
    const dialog = await open(user);
    await user.click(await within(dialog).findByRole("combobox", { name: "Perfil" }));
    expect(await within(dialog).findByText(profilePickerEmptyHint())).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: "Crear un perfil de aprovisionamiento" })).toBeInTheDocument();
    expect(
      within(dialog).getByText("Todavía no hay perfiles de aprovisionamiento. Crea primero uno en la pestaña Perfiles."),
    ).toBeInTheDocument();
    expectNoEnglish(dialog);
  });
});

// ------------------------------------------------------------------ images

describe("the images page, in Spanish", () => {
  it("words the head, the tiles, the filter, the table and the pills", async () => {
    const { container } = renderPage(<ImagesPage />, "/provisioning/images");
    expect(await screen.findByRole("heading", { name: "Imágenes" })).toBeInTheDocument();
    await screen.findByText("Windows 11 Estándar");
    expect(screen.getByRole("button", { name: "Nueva imagen" })).toBeInTheDocument();
    expect(screen.getByText("En 2 plataformas")).toBeInTheDocument();
    expect(screen.getByText("Listas para construir a partir de ellas")).toBeInTheDocument();
    expect(screen.getByText("Registradas pero sin poder construir a partir de ellas")).toBeInTheDocument();
    const filter = screen.getByRole("combobox", { name: "Filtrar las imágenes por estado" });
    for (const option of ["Todas las imágenes", "Activas", "No activas"]) {
      expect(within(filter).getByRole("option", { name: option })).toBeInTheDocument();
    }
    for (const header of ["Nombre", "Plataforma", "Tipo", "Estado", "Añadida"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    const row = (name: string) => screen.getByText(name).closest("tr") as HTMLElement;
    expect(within(row("Windows 11 Estándar")).getByText("Instalación")).toBeInTheDocument();
    expect(within(row("Windows 11 Estándar")).getByText("Activa")).toBeInTheDocument();
    expect(within(row("Windows 11 Estándar")).getByText("sha256 dead…6789")).toBeInTheDocument();
    expect(within(row("Ubuntu servidor")).getByText("Construcción")).toBeInTheDocument();
    expect(within(row("Ubuntu servidor")).getByText("Borrador")).toBeInTheDocument();
    expect(within(row("Ubuntu servidor")).getByText("No se ha subido ningún archivo")).toBeInTheDocument();
    expect(within(row("Respuestas 24H2")).getByText("Archivo de respuestas")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("says why there are no images", async () => {
    baseApi({ listOsImages: vi.fn().mockResolvedValue([]) });
    const { container } = renderPage(<ImagesPage />, "/provisioning/images");
    expect(await screen.findByText("No hay imágenes de sistema operativo registradas.")).toBeInTheDocument();
    expect(screen.getByText("No hay ninguna imagen de sistema operativo registrada")).toBeInTheDocument();
    expect(screen.getByText("No existe ninguna imagen que activar")).toBeInTheDocument();
    expect(screen.getByText("No hay ninguna imagen registrada")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("says a single platform in the singular, and that every image is active", async () => {
    baseApi({ listOsImages: vi.fn().mockResolvedValue([image()]) });
    const { container } = renderPage(<ImagesPage />, "/provisioning/images");
    await screen.findByText("Windows 11 Estándar");
    expect(screen.getByText("En 1 plataforma")).toBeInTheDocument();
    expect(screen.getByText("Todas las imágenes registradas están activas")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("names the missing fields of a new image, and re-words the refusal on a switch", async () => {
    const user = userEvent.setup();
    renderPage(<ImagesPage />, "/provisioning/images");
    await screen.findByText("Windows 11 Estándar");
    await user.click(screen.getByRole("button", { name: "Nueva imagen" }));
    const dialog = await screen.findByRole("dialog", { name: "Nueva imagen de sistema operativo" });
    expect(within(dialog).getByText("Familia de sistema operativo")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Crear" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("El campo Nombre es obligatorio.");
    expectNoEnglish(dialog);
    act(() => setActiveLanguage("en"));
    expect(within(screen.getByRole("dialog", { name: "New OS image" })).getByRole("alert")).toHaveTextContent(
      "Name is required",
    );
  });

  it("re-words a mounted page when the language is switched", async () => {
    renderPage(<ImagesPage />, "/provisioning/images");
    await screen.findByText("Windows 11 Estándar");
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Across 2 platforms")).toBeInTheDocument();
    const row = screen.getByText("Ubuntu servidor").closest("tr") as HTMLElement;
    expect(within(row).getByText("Build")).toBeInTheDocument();
    expect(within(row).getByText("Draft")).toBeInTheDocument();
    expect(within(row).getByText("No file uploaded")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- profiles

describe("the profiles page, in Spanish", () => {
  it("words the head, the tiles, the filter, the table and the credential pills", async () => {
    const { container } = renderPage(<ProfilesPage />, "/provisioning/profiles");
    expect(await screen.findByRole("heading", { name: "Perfiles" })).toBeInTheDocument();
    await screen.findByText("estación-estándar");
    expect(screen.getByRole("button", { name: "Nuevo perfil" })).toBeInTheDocument();
    expect(screen.getByText("Usan 1 imagen")).toBeInTheDocument();
    expect(screen.getByText("Listos para aprovisionar")).toBeInTheDocument();
    expect(screen.getByText("Configurados pero sin aprovisionar nada")).toBeInTheDocument();
    const filter = screen.getByRole("combobox", { name: "Filtrar los perfiles por estado" });
    for (const option of ["Todos los perfiles", "Habilitados", "Deshabilitados"]) {
      expect(within(filter).getByRole("option", { name: option })).toBeInTheDocument();
    }
    for (const header of ["Nombre", "Credenciales", "Estado"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    const first = screen.getByText("estación-estándar").closest("tr") as HTMLElement;
    expect(within(first).getByText("Credencial de unión al dominio establecida")).toBeInTheDocument();
    expect(within(first).getByText("Administrador local sin establecer")).toBeInTheDocument();
    expect(within(first).getByText("Habilitado")).toBeInTheDocument();
    expect(within(first).getByRole("button", { name: "Editar" })).toBeInTheDocument();
    const second = screen.getByText("quiosco-recepción").closest("tr") as HTMLElement;
    expect(within(second).getByText("Unión al dominio sin establecer")).toBeInTheDocument();
    expect(within(second).getByText("Credencial de administrador local establecida")).toBeInTheDocument();
    expect(within(second).getByText("Deshabilitado")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("says why there are no profiles", async () => {
    baseApi({ listProvisionProfiles: vi.fn().mockResolvedValue([]) });
    const { container } = renderPage(<ProfilesPage />, "/provisioning/profiles");
    expect(await screen.findByText("No hay perfiles de aprovisionamiento configurados.")).toBeInTheDocument();
    expect(screen.getByText("No hay ningún perfil de aprovisionamiento configurado")).toBeInTheDocument();
    expect(screen.getByText("No existe ningún perfil que habilitar")).toBeInTheDocument();
    expect(screen.getByText("No existe ningún perfil que deshabilitar")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("says every profile is disabled, and every profile enabled", async () => {
    baseApi({ listProvisionProfiles: vi.fn().mockResolvedValue([PROFILES[1]]) });
    const { container, unmount } = renderPage(<ProfilesPage />, "/provisioning/profiles");
    await screen.findByText("quiosco-recepción");
    expect(screen.getByText("Todos los perfiles están deshabilitados, así que no se puede aprovisionar nada")).toBeInTheDocument();
    expectNoEnglish(container);
    unmount();
    baseApi({ listProvisionProfiles: vi.fn().mockResolvedValue([PROFILES[0]]) });
    const second = renderPage(<ProfilesPage />, "/provisioning/profiles");
    await screen.findByText("estación-estándar");
    expect(screen.getByText("Todos los perfiles están habilitados")).toBeInTheDocument();
    expectNoEnglish(second.container);
  });

  it("opens the editor for a new profile, names the missing fields, and re-words them on a switch", async () => {
    const user = userEvent.setup();
    renderPage(<ProfilesPage />, "/provisioning/profiles");
    await screen.findByText("estación-estándar");
    await user.click(screen.getByRole("button", { name: "Nuevo perfil" }));
    const dialog = await screen.findByRole("dialog", { name: "Nuevo perfil" });
    for (const label of ["Nombre", "Dominio", "Administrador local", "Unión al dominio"]) {
      expect(within(dialog).getAllByText(label).length).toBeGreaterThan(0);
    }
    expect(within(dialog).getAllByText("Contraseña (déjala en blanco para conservar la existente)")).toHaveLength(2);
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("El campo Nombre es obligatorio.");
    expectNoEnglish(dialog);
    act(() => setActiveLanguage("en"));
    expect(within(screen.getByRole("dialog", { name: "New profile" })).getByRole("alert")).toHaveTextContent(
      "Name is required",
    );
  });

  it("opens the editor for an existing profile with its credential markers", async () => {
    const user = userEvent.setup();
    renderPage(<ProfilesPage />, "/provisioning/profiles");
    await screen.findByText("estación-estándar");
    await user.click(within(screen.getByText("estación-estándar").closest("tr") as HTMLElement).getByRole("button", { name: "Editar" }));
    const dialog = await screen.findByRole("dialog", { name: "Editar perfil" });
    expect(within(dialog).getByText("Credencial establecida")).toBeInTheDocument();
    expect(within(dialog).getByText("Habilitado")).toBeInTheDocument();
    expectNoEnglish(dialog);
  });

  it("explains the dead end when no images are registered, in the editor", async () => {
    const user = userEvent.setup();
    baseApi({ listOsImages: vi.fn().mockResolvedValue([]) });
    renderPage(<ProfilesPage />, "/provisioning/profiles");
    await screen.findByText("estación-estándar");
    await user.click(screen.getByRole("button", { name: "Nuevo perfil" }));
    const dialog = await screen.findByRole("dialog", { name: "Nuevo perfil" });
    await user.click(await within(dialog).findByRole("combobox", { name: "Imagen" }));
    expect(await within(dialog).findByText(imagePickerEmptyHint())).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: "Registrar una imagen de sistema operativo" })).toBeInTheDocument();
    expectNoEnglish(dialog);
  });
});

// ------------------------------------------------------------- device tab

describe("a device's Imaging tab, in Spanish", () => {
  it("words the facts, with the profile and image named", async () => {
    const { container } = renderPage(<DeviceProvisioningTab deviceId="dev1" />);
    expect(await screen.findByText("Estado del aprovisionamiento")).toBeInTheDocument();
    expect(await screen.findByText("estación-estándar")).toBeInTheDocument();
    expect(screen.getByText("Windows 11 Estándar")).toBeInTheDocument();
    expect(screen.getByText("Estado de la última ejecución")).toBeInTheDocument();
    expect(screen.getByText("Completada")).toBeInTheDocument();
    expect(screen.getByText("Inscrito")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("says no run was recorded", async () => {
    baseApi({
      getDeviceProvisioning: vi.fn().mockResolvedValue({
        device_id: "dev1",
        profile_id: null,
        image_id: null,
        enrolled_at: null,
        latest_run_state: null,
      }),
    });
    const { container } = renderPage(<DeviceProvisioningTab deviceId="dev1" />);
    expect(await screen.findByText("No hay ninguna ejecución de aprovisionamiento registrada")).toBeInTheDocument();
    expectNoEnglish(container);
  });
});

// ----------------------------------------------------------- prerequisites

describe("the prerequisites panel, in Spanish", () => {
  it("sends an image-builder with no published package to get the installer", async () => {
    const api = {
      me: meWith(["provisioning.read", "agent.release.read"]),
      listAgentReleases: vi.fn().mockResolvedValue([]),
    };
    mockUseApi.mockReturnValue(api as unknown as ApiClient);
    const { container } = renderPage(<ProvisioningPrerequisites />);
    await expectCta("Obtener el instalador del agente", "/agent-packages#releases");
    expect(
      screen.getByText(/Un equipo construido se une ejecutando el agente desde su imagen/),
    ).toBeInTheDocument();
    expectNoEnglish(container);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Before a built machine can join")).toBeInTheDocument();
    expect(screen.getByText("Get the agent installer")).toBeInTheDocument();
  });
});

// ------------------------------------------------- labels built outside React

describe("the labels the module builds outside React, in Spanish", () => {
  it("puts a no-break space before the percent sign", () => {
    setActiveLanguage("es");
    expect(translate("provisioning.runs.percent", { percent: 50 })).toBe("50\u00a0%");
    setActiveLanguage("en");
    expect(translate("provisioning.runs.percent", { percent: 50 })).toBe("50%");
  });

  it("words the nav strip, and follows a switch", () => {
    setActiveLanguage("es");
    expect(PROVISIONING_NAV_ITEMS.map((item) => item.label)).toEqual(["Ejecuciones", "Imágenes", "Perfiles"]);
    setActiveLanguage("en");
    expect(PROVISIONING_NAV_ITEMS.map((item) => item.label)).toEqual(["Runs", "Images", "Profiles"]);
  });

  it("words the state pills, and a state nobody catalogued reads through the shared words", () => {
    setActiveLanguage("es");
    expect(provisionRunStateLabel("awaiting_enrollment")).toBe("Esperando inscripción");
    expect(provisionRunStateLabel("failed")).toBe("Fallida");
    // Not in the module's vocabulary: the shared status words, never the wire word.
    expect(provisionRunStateLabel("succeeded")).toBe("Correcto");
    expect(osImageStateLabel("active")).toBe("Activa");
    expect(osImageStateLabel("draft")).toBe("Borrador");
    expect(osImageStateLabel("inactive")).toBe("Inactivo");
    setActiveLanguage("en");
    expect(provisionRunStateLabel("awaiting_enrollment")).toBe("Awaiting enrollment");
    expect(provisionRunStateLabel("succeeded")).toBe("Succeeded");
    expect(osImageStateLabel("draft")).toBe("Draft");
  });

  it("words the picker sublabels and empty hints, and follows a switch", () => {
    setActiveLanguage("es");
    const [option] = profilePickerOptions([PROFILES[1]!], IMAGES);
    expect(option?.sublabel).toBe("Windows 11 Estándar · deshabilitado");
    expect(imagePickerOptions(IMAGES).map((o) => o.sublabel)).toEqual([
      "windows · instalación · activa",
      "linux · construcción · borrador",
      "windows · archivo de respuestas · activa",
    ]);
    expect(imagePickerOptions([image({ kind: "mystery", state: "retired" })])[0]?.sublabel).toBe(
      "windows · mystery · retired",
    );
    expect(profilePickerEmptyHint()).toBe(translate("provisioning.picker.profileEmptyHint"));
    expect(profilePickerEmptyHint()).toMatch(/pestaña Perfiles/);
    expect(imagePickerEmptyHint()).toMatch(/pestaña Imágenes/);
    setActiveLanguage("en");
    expect(profilePickerOptions([PROFILES[1]!], IMAGES)[0]?.sublabel).toBe("Windows 11 Estándar · disabled");
    expect(imagePickerOptions(IMAGES)[0]?.sublabel).toBe("windows · install · active");
    expect(imagePickerEmptyHint()).toBe(IMAGE_PICKER_EMPTY_HINT);
  });
});
