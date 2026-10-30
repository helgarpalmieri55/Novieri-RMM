/**
 * Software deployment in Spanish: the deployments page (tiles, filters, the
 * table, the execute confirmation and the create dialog with its refusals),
 * the packages page, the scripts page with its schedules and dialogs, a
 * device's own Deploy tab, the run-a-script-on-this-device dialog, the
 * readiness panel, and the sentences the module builds outside React. Package
 * and script names, hostnames and identifiers read as recorded; the words
 * around them are Spanish.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage, translate } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import { ApiError } from "../../lib/api";
import type { ApiClient, Device, DeviceGroup, Me } from "../../lib/api";
import { cronPlaceholder, cronProblem, windowLabel } from "./cron";
import { DEPLOY_NAV_ITEMS } from "./deployNav";
import { DeployPage } from "./DeployPage";
import { DeployReadiness } from "./DeployReadiness";
import { DeviceDeployTab } from "./DeviceDeployTab";
import { PackagesPage } from "./PackagesPage";
import {
  deploymentActionLabel,
  devicePickerEmptyHint,
  devicePickerOptions,
  groupPickerEmptyHint,
  groupPickerOptions,
  interpreterLabel,
  packagePickerEmptyHint,
  packagePickerOptions,
} from "./pickerOptions";
import { RunScriptOnDeviceDialog, scriptOptions } from "./RunScriptOnDeviceDialog";
import { intervalLabel, outcomeLabel, SchedulesPanel, whenLabel } from "./SchedulesPanel";
import { ScriptsPage } from "./ScriptsPage";
import type {
  Deployment,
  DeviceDeploymentHistory,
  Package,
  Script,
  ScriptListItem,
  ScriptPolicy,
} from "./types";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({
  useApi: () => ({
    listScriptPolicies: () => Promise.resolve([]),
    getCurrentOrg: () =>
      Promise.resolve({ id: "o1", name: "Org", slug: "org", status: "active", settings: {} }),
    listChangeGates: () => Promise.resolve([]),
    listChanges: () => Promise.resolve([]),
    ...mockUseApi(),
  }),
}));

function renderPage(ui: ReactElement, path = "/deploy") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

// ------------------------------------------------------------------ fixtures

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  permissions: [
    "deploy.read",
    "deploy.create",
    "deploy.execute",
    "deploy.package.manage",
    "deploy.script.manage",
    "deploy.script.run",
    "deploy.script.schedule",
    "devices.read",
  ],
};

const PACKAGES: Package[] = [
  {
    id: "pkg1",
    name: "Notepad++",
    version: "8.6",
    os_family: "windows",
    installer_type: "msi",
    artifact_ref: "sha256:abc",
    artifact_sha256: "abcdef0123456789abcdef",
    detection_rule: {},
    uninstall_rule: {},
    install_args: null,
    created_at: "2026-01-01T00:00:00.000Z",
  },
  {
    id: "pkg2",
    name: "Cliente VPN",
    version: "3.1",
    os_family: "linux",
    installer_type: "deb",
    artifact_ref: null,
    artifact_sha256: null,
    detection_rule: {},
    uninstall_rule: {},
    install_args: null,
    created_at: "2026-01-02T00:00:00.000Z",
  },
];

function device(overrides: Partial<Device> & Pick<Device, "id">): Device {
  return {
    hostname: null,
    fqdn: null,
    os_family: null,
    os_version: "",
    architecture: "",
    device_group_id: null,
    status: "active",
    agent_status: null,
    last_seen_at: null,
    cert_not_after: null,
    cert_state: null,
    cert_revoked: false,
    lifecycle: "in_service",
    asset_type: "computer",
    source: "agent",
    provenance: "agent",
    description: null,
    primary_ip: null,
    owner: null,
    location: null,
    ...overrides,
  };
}

const DEVICES: Device[] = [
  device({ id: "dev1", hostname: "WKS-042", os_family: "windows", agent_status: "online" }),
  device({ id: "dev2", hostname: "BUILD-01", os_family: "linux", agent_status: "offline" }),
  device({ id: "dev3", hostname: "PRINT-3", os_family: "linux", agent_status: null }),
];
const DEVICE_PAGE = { items: DEVICES, total: DEVICES.length };

const GROUP: DeviceGroup = {
  id: "grp-build",
  client_id: "o1",
  name: "Equipo de compilación",
  parent_group_id: null,
  description: "",
  dynamic: false,
  device_count: 3,
  precedence: 0,
  archived_at: null,
};
const SOLO_GROUP: DeviceGroup = { ...GROUP, id: "grp-solo", name: "Un solo equipo", device_count: 1 };
const GROUP_PAGE = { items: [GROUP, SOLO_GROUP], total: 2, ungrouped_device_count: 0, evaluation: null };

function deployment(overrides: Partial<Deployment> = {}): Deployment {
  return {
    id: "dep1",
    client_id: "o1",
    package_id: "pkg1",
    action: "install",
    target_type: "device",
    target_id: "dev1",
    window_cron: null,
    requested_by: "u1",
    state: "pending",
    created_at: "2020-01-02T00:00:00.000Z",
    ...overrides,
  };
}

const DEPLOYMENTS: Deployment[] = [
  deployment(),
  deployment({ id: "dep2", state: "dispatching", action: "update", window_cron: "0 22-23 * * 1-5" }),
  deployment({ id: "dep3", state: "complete", action: "uninstall", target_type: "group", target_id: "grp-build" }),
  deployment({ id: "dep4", state: "failed", window_cron: "30 2 1 * *", requested_by: null }),
];

const USERS = {
  users: [{ id: "u1", email: "operator@example.com", display_name: "Adaeze Okonkwo", status: "active", roles: [] }],
};

const SCRIPT_ITEM: ScriptListItem = {
  id: "scr1",
  name: "limpiar-temporales",
  os_family: "windows",
  interpreter: "powershell",
  version: 2,
  created_by: "u1",
  created_at: "2026-01-01T00:00:00.000Z",
};
const SCRIPT_LINUX: ScriptListItem = { ...SCRIPT_ITEM, id: "scr2", name: "rotar-logs", os_family: "linux", interpreter: "bash" };
const SCRIPTS = [SCRIPT_ITEM, SCRIPT_LINUX];
const SCRIPT_DETAIL: Script = { ...SCRIPT_ITEM, body: "Remove-Item -Recurse C:\\Temp\\*" };

const POLICY: ScriptPolicy = {
  id: "pol1",
  name: "Limpieza nocturna",
  script_id: "scr1",
  script_name: "limpiar-temporales",
  device_group_id: "grp-build",
  device_group_name: "Equipo de compilación",
  interval_hours: 24,
  in_maintenance_window: true,
  alert_on_failure: true,
  keep_runs: 30,
  enabled: true,
  devices_failing: 2,
  devices_succeeding: 10,
  last_run_at: "2026-09-23T01:00:00Z",
};

function baseApi(overrides: Record<string, unknown> = {}) {
  const api = {
    me: vi.fn().mockResolvedValue(ME),
    listDeployments: vi.fn().mockResolvedValue(DEPLOYMENTS),
    listPackages: vi.fn().mockResolvedValue(PACKAGES),
    listDevices: vi.fn().mockResolvedValue(DEVICE_PAGE),
    listDeviceGroups: vi.fn().mockResolvedValue(GROUP_PAGE),
    listUsers: vi.fn().mockResolvedValue(USERS),
    listScripts: vi.fn().mockResolvedValue(SCRIPTS),
    getScript: vi.fn().mockResolvedValue(SCRIPT_DETAIL),
    executeDeployment: vi.fn().mockResolvedValue({ job_ids: ["j1"] }),
    createDeployment: vi.fn().mockResolvedValue(DEPLOYMENTS[0]),
    createPackage: vi.fn().mockResolvedValue(PACKAGES[0]),
    deletePackage: vi.fn().mockResolvedValue(undefined),
    runScript: vi.fn().mockResolvedValue({ job_ids: ["j1"] }),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

async function pick(user: ReturnType<typeof userEvent.setup>, label: string, name: RegExp, scope?: HTMLElement) {
  const root = scope ? within(scope) : screen;
  await user.click(root.getByRole("combobox", { name: label }));
  await user.click(await screen.findByRole("option", { name }));
}

// ------------------------------------------------------------- deployments

describe("the deployments page, in Spanish", () => {
  beforeEach(() => {
    baseApi();
  });

  it("words the head, the tiles, the filter, the table and the row actions", async () => {
    const { container } = renderPage(<DeployPage />);
    expect(await screen.findByRole("heading", { name: "Despliegue de software" })).toBeInTheDocument();
    await screen.findByText("dep1");
    expect(screen.getByRole("button", { name: "Nuevo despliegue" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Despliegues" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Paquetes" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Scripts" })).toBeInTheDocument();
    // The tiles and their sub-lines.
    expect(screen.getByText("Desplegados esta semana")).toBeInTheDocument();
    expect(screen.getByText("Tasa de éxito")).toBeInTheDocument();
    expect(screen.getByText("Fallidos")).toBeInTheDocument();
    expect(screen.getByText("2 pendientes de ejecutar")).toBeInTheDocument();
    expect(screen.getByText("1 de 2 completada")).toBeInTheDocument();
    expect(screen.getByText("2 ejecuciones han finalizado")).toBeInTheDocument();
    // The filter.
    const filter = screen.getByRole("combobox", { name: "Filtrar por estado" });
    expect(within(filter).getByRole("option", { name: "Todos los despliegues" })).toBeInTheDocument();
    expect(within(filter).getByRole("option", { name: "Desplegados en los últimos 7 días" })).toBeInTheDocument();
    expect(within(filter).getByRole("option", { name: "En envío" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Buscar despliegues" })).toHaveAttribute(
      "placeholder",
      "Buscar por despliegue, paquete, acción, destino o estado",
    );
    // The table.
    for (const header of ["Paquete", "Acción", "Destino", "Ventana", "Estado", "Solicitado por"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    const row = (id: string) => screen.getByText(id).closest("tr") as HTMLElement;
    expect(within(row("dep1")).getByText("Instalar")).toBeInTheDocument();
    expect(within(row("dep1")).getByText("Pendiente")).toBeInTheDocument();
    expect(within(row("dep1")).getByText("Siempre abierta")).toBeInTheDocument();
    expect(within(row("dep1")).getByText("Adaeze Okonkwo")).toBeInTheDocument();
    expect(within(row("dep2")).getByText("En envío")).toBeInTheDocument();
    expect(within(row("dep2")).getByText("Actualizar")).toBeInTheDocument();
    expect(within(row("dep2")).getByText("Entre semana, de 22:00 a 24:00")).toBeInTheDocument();
    expect(within(row("dep3")).getByText("Completado")).toBeInTheDocument();
    expect(within(row("dep3")).getByText("Desinstalar")).toBeInTheDocument();
    expect(within(row("dep3")).getByText("Equipo de compilación")).toBeInTheDocument();
    expect(within(row("dep4")).getByText("Fallido")).toBeInTheDocument();
    expect(within(row("dep4")).getByText("30 2 1 * *")).toBeInTheDocument();
    expect(within(row("dep4")).getByText("n/d")).toBeInTheDocument();
    expect(within(row("dep1")).getByRole("button", { name: "Ejecutar" })).toBeInTheDocument();
    expectNoEnglish(container, ["Notepad++"]);
  });

  it("says why the list is empty, and why a filter found nothing", async () => {
    const user = userEvent.setup();
    baseApi({ listDeployments: vi.fn().mockResolvedValue([]) });
    const { container } = renderPage(<DeployPage />);
    expect(await screen.findByText("Aún no hay despliegues.")).toBeInTheDocument();
    expect(screen.getByText("Nunca se ha desplegado nada")).toBeInTheDocument();
    expect(screen.getByText("Nada en los últimos 7 días")).toBeInTheDocument();
    expect(screen.getByText("Ninguna ejecución ha fallado")).toBeInTheDocument();
    expect(screen.getByText("Aún no hay ejecuciones completadas")).toBeInTheDocument();
    expectNoEnglish(container);
    baseApi();
    await user.selectOptions(screen.getByRole("combobox", { name: "Filtrar por estado" }), "week");
  });

  it("asks before executing, naming the package, the target and the window", async () => {
    const user = userEvent.setup();
    renderPage(<DeployPage />);
    await screen.findByText("dep2");
    await user.click(within(screen.getByText("dep2").closest("tr") as HTMLElement).getByRole("button", { name: "Ejecutar" }));
    const confirm = await screen.findByRole("dialog", { name: "Ejecutar despliegue" });
    expect(confirm).toHaveTextContent("Actualizar Notepad++ 8.6 en WKS-042");
    expect(confirm).toHaveTextContent(
      "Lo envía a todos los dispositivos del destino. Se ejecuta la próxima vez que esté abierta la ventana de mantenimiento (Entre semana, de 22:00 a 24:00, UTC).",
    );
    expect(confirm).toHaveTextContent("El software ya instalado no se puede revertir desde aquí.");
    expectNoEnglish(confirm, ["Notepad++"]);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("dialog", { name: "Execute deployment" })).toHaveTextContent(
      "Update Notepad++ 8.6 on WKS-042",
    );
    expect(screen.getByRole("dialog", { name: "Execute deployment" })).toHaveTextContent(
      "Weekdays, 22:00 to 24:00, UTC",
    );
  });

  it("says no window holds back a deployment that has none", async () => {
    const user = userEvent.setup();
    renderPage(<DeployPage />);
    await screen.findByText("dep1");
    await user.click(within(screen.getByText("dep1").closest("tr") as HTMLElement).getByRole("button", { name: "Ejecutar" }));
    const confirm = await screen.findByRole("dialog", { name: "Ejecutar despliegue" });
    expect(confirm).toHaveTextContent("Lo envía ahora a todos los dispositivos del destino: ninguna ventana de mantenimiento lo retiene.");
    expectNoEnglish(confirm, ["Notepad++"]);
  });

  it("shows a refused execution in Spanish", async () => {
    const user = userEvent.setup();
    const api = baseApi({ executeDeployment: vi.fn().mockRejectedValue(new ApiError(403, "")) });
    const { container } = renderPage(<DeployPage />);
    await screen.findByText("dep1");
    await user.click(within(screen.getByText("dep1").closest("tr") as HTMLElement).getByRole("button", { name: "Ejecutar" }));
    await user.click(within(await screen.findByRole("dialog", { name: "Ejecutar despliegue" })).getByRole("button", { name: "Ejecutar" }));
    await waitFor(() => expect(api.executeDeployment).toHaveBeenCalled());
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expectNoEnglish(container, ["Notepad++"]);
  });

  it("re-words a mounted page when the language is switched", async () => {
    renderPage(<DeployPage />);
    await screen.findByText("dep2");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("heading", { name: "Software deployment" })).toBeInTheDocument();
    const row = screen.getByText("dep2").closest("tr") as HTMLElement;
    expect(within(row).getByText("Dispatching")).toBeInTheDocument();
    expect(within(row).getByText("Update")).toBeInTheDocument();
    expect(within(row).getByText("Weekdays, 22:00 to 24:00")).toBeInTheDocument();
    expect(screen.getByText("2 still to run")).toBeInTheDocument();
    expect(screen.getByText("1 of 2 completed")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Packages" })).toBeInTheDocument();
  });
});

describe("the New deployment dialog, in Spanish", () => {
  async function openDialog() {
    const user = userEvent.setup();
    const api = baseApi();
    const view = renderPage(<DeployPage />);
    await user.click(await screen.findByRole("button", { name: "Nuevo despliegue" }));
    const dialog = await screen.findByRole("dialog", { name: "Nuevo despliegue" });
    await within(dialog).findByRole("combobox", { name: "Paquete" });
    return { user, api, dialog, ...view };
  }

  it("words the form: the pickers, the target types and the window", async () => {
    const { user, dialog } = await openDialog();
    expect(within(dialog).getByRole("combobox", { name: "Paquete" })).toBeInTheDocument();
    expect(within(dialog).getByText("Tipo de destino")).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "Dispositivo" })).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "Grupo de dispositivos" })).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "Instalar" })).toBeInTheDocument();
    expect(within(dialog).getByText("Ventana de mantenimiento (UTC, opcional)")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Crear" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Cancelar" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("combobox", { name: "Destino" }));
    expect(await screen.findByRole("option", { name: /WKS-042/ })).toHaveTextContent("windows · en línea");
    expect(screen.getByRole("option", { name: /PRINT-3/ })).toHaveTextContent("sin agente");
    expectNoEnglish(dialog, ["Notepad++"]);
  });

  it("agrees the group sublabel with the number of devices", async () => {
    const { user, dialog } = await openDialog();
    await user.selectOptions(within(dialog).getByLabelText("Tipo de destino"), "group");
    await user.click(within(dialog).getByRole("combobox", { name: "Destino" }));
    expect(await screen.findByRole("option", { name: /Equipo de compilación/ })).toHaveTextContent("3 dispositivos");
    expect(screen.getByRole("option", { name: /Un solo equipo/ })).toHaveTextContent("1 dispositivo");
  });

  it("names the missing fields, and re-words the refusal on a switch", async () => {
    const { user, api, dialog } = await openDialog();
    await user.click(within(dialog).getByRole("button", { name: "Crear" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("El campo Paquete es obligatorio");
    expect(alert).toHaveTextContent("El campo Destino es obligatorio");
    expect(api.createDeployment).not.toHaveBeenCalled();
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent("Package is required");
    expect(screen.getByRole("alert")).toHaveTextContent("Target is required");
  });

  it("words an impossible maintenance window, and re-words it on a switch", async () => {
    const { user, api, dialog } = await openDialog();
    await pick(user, "Paquete", /Notepad/, dialog);
    await pick(user, "Destino", /WKS-042/, dialog);
    // The painter writes the window; the raw expression is reachable under Advanced.
    await user.type(within(dialog).getByLabelText(translate("components.weekPainter.cronExpression")), "0 25 * * 6");
    await user.click(within(dialog).getByRole("button", { name: "Crear" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "El campo de hora de la ventana debe estar entre 0 y 23.",
    );
    expect(api.createDeployment).not.toHaveBeenCalled();
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent("The hour of the window must be between 0 and 23.");
  });

  it("links to adding a package when the catalogue is empty, and to adding a computer", async () => {
    const user = userEvent.setup();
    baseApi({
      listPackages: vi.fn().mockResolvedValue([]),
      listDevices: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    });
    renderPage(<DeployPage />);
    await user.click(await screen.findByRole("button", { name: "Nuevo despliegue" }));
    const dialog = await screen.findByRole("dialog", { name: "Nuevo despliegue" });
    expect(await within(dialog).findByRole("link", { name: "Añadir un paquete al catálogo" })).toHaveAttribute("href", "/deploy/packages");
    await user.click(within(dialog).getByRole("combobox", { name: "Paquete" }));
    expect(await screen.findByText(packagePickerEmptyHint())).toBeInTheDocument();
    expect(packagePickerEmptyHint()).toContain("Añade uno en Software → Paquetes");
    expectNoEnglish(dialog);
  });
});

// ---------------------------------------------------------------- packages

describe("the packages page, in Spanish", () => {
  it("words the table, the row menu and the delete confirmation", async () => {
    const user = userEvent.setup();
    baseApi();
    const { container } = renderPage(<PackagesPage />, "/deploy/packages");
    expect(await screen.findByRole("heading", { name: "Paquetes" })).toBeInTheDocument();
    await screen.findByText("Notepad++");
    expect(screen.getByText("Instaladores que puedes desplegar. Cada archivo se verifica con una suma de comprobación al subirlo.")).toBeInTheDocument();
    for (const header of ["Nombre", "Versión", "Plataforma", "Instalador", "Subido"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(screen.getByText("Sin archivo subido")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Subir archivo" }).length).toBe(2);
    expect(screen.getByLabelText("Archivo instalador de Notepad++")).toBeInTheDocument();
    expectNoEnglish(container, ["Notepad++", "sha256"]);
    // The row menu and its confirmation.
    await user.click(screen.getAllByRole("button", { name: /Notepad\+\+ 8\.6/ })[0] as HTMLElement);
    await user.click(await screen.findByRole("menuitem", { name: "Eliminar" }));
    const confirm = await screen.findByRole("dialog", { name: "Eliminar paquete" });
    expect(confirm).toHaveTextContent("Lo quita del catálogo junto con su archivo subido.");
    expect(within(confirm).getByRole("button", { name: "Eliminar paquete" })).toBeInTheDocument();
    expectNoEnglish(confirm, ["Notepad++"]);
  });

  it("names the package a refused delete was about, and says 'ese paquete' when it cannot", async () => {
    const user = userEvent.setup();
    baseApi({ deletePackage: vi.fn().mockRejectedValue(new ApiError(403, "")) });
    renderPage(<PackagesPage />, "/deploy/packages");
    await screen.findByText("Notepad++");
    await user.click(screen.getAllByRole("button", { name: /Notepad\+\+ 8\.6/ })[0] as HTMLElement);
    await user.click(await screen.findByRole("menuitem", { name: "Eliminar" }));
    await user.click(within(await screen.findByRole("dialog", { name: "Eliminar paquete" })).getByRole("button", { name: "Eliminar paquete" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Notepad++ no se eliminó.");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent("Notepad++ was not deleted.");
  });

  it("words the empty page", async () => {
    baseApi({ listPackages: vi.fn().mockResolvedValue([]) });
    const { container } = renderPage(<PackagesPage />, "/deploy/packages");
    expect(await screen.findByText("No hay paquetes registrados.")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words the New package dialog and its refusal", async () => {
    const user = userEvent.setup();
    const api = baseApi();
    renderPage(<PackagesPage />, "/deploy/packages");
    await user.click(await screen.findByRole("button", { name: "Nuevo paquete" }));
    const dialog = await screen.findByRole("dialog", { name: "Nuevo paquete" });
    expect(within(dialog).getByLabelText("Argumentos de instalación (opcional)")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Crear" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("El campo Nombre es obligatorio");
    expect(alert).toHaveTextContent("El campo Versión es obligatorio");
    expect(api.createPackage).not.toHaveBeenCalled();
    expectNoEnglish(dialog);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent("Name is required");
  });
});

// ----------------------------------------------------------------- scripts

describe("the scripts page, in Spanish", () => {
  it("words the table, the schedules panel and the row actions", async () => {
    baseApi({ listScriptPolicies: vi.fn().mockResolvedValue([POLICY]) });
    const { container } = renderPage(<ScriptsPage />, "/deploy/scripts");
    expect(await screen.findByRole("heading", { name: "Scripts" })).toBeInTheDocument();
    await screen.findByText("limpiar-temporales", { selector: ".cell-title" });
    expect(screen.getByText("Scripts que puedes ejecutar en dispositivos, con derechos de administrador completos.")).toBeInTheDocument();
    for (const header of ["Nombre", "Plataforma", "Intérprete", "Versión"]) {
      expect(screen.getAllByRole("columnheader", { name: header }).length).toBeGreaterThan(0);
    }
    expect(screen.getByText("Bash")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Editar" }).length).toBe(2);
    expect(screen.getAllByRole("button", { name: "Ejecutar" }).length).toBe(2);
    // The schedules panel.
    expect(await screen.findByText("Limpieza nocturna")).toBeInTheDocument();
    expect(screen.getByText("Programaciones")).toBeInTheDocument();
    expect(screen.getByText("Falla en 2 equipos")).toBeInTheDocument();
    expect(screen.getByText("Cada día, durante la ventana de mantenimiento de cada equipo")).toBeInTheDocument();
    expect(screen.getByText("Avisa cuando falla")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Pausar Limpieza nocturna" })).toBeInTheDocument();
    expectNoEnglish(container, ["PowerShell"]);
  });

  it("words the empty page", async () => {
    baseApi({ listScripts: vi.fn().mockResolvedValue([]) });
    const { container } = renderPage(<ScriptsPage />, "/deploy/scripts");
    expect(await screen.findByText("Aún no se ha creado ningún script.")).toBeInTheDocument();
    expect(await screen.findByText("Todavía no hay ningún script programado.")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words the New script dialog and its refusal", async () => {
    const user = userEvent.setup();
    baseApi();
    renderPage(<ScriptsPage />, "/deploy/scripts");
    await user.click(await screen.findByRole("button", { name: "Nuevo script" }));
    const dialog = await screen.findByRole("dialog", { name: "Nuevo script" });
    await user.clear(within(dialog).getByLabelText("Intérprete"));
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("El campo Nombre es obligatorio");
    expect(alert).toHaveTextContent("El campo Intérprete es obligatorio");
    expect(alert).toHaveTextContent("El campo Contenido es obligatorio");
    expectNoEnglish(dialog);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent("Interpreter is required");
  });

  it("words the Edit dialog while the body loads, and when it cannot", async () => {
    const user = userEvent.setup();
    baseApi({ getScript: vi.fn().mockRejectedValue(new ApiError(404, "")) });
    renderPage(<ScriptsPage />, "/deploy/scripts");
    await user.click((await screen.findAllByRole("button", { name: "Editar" }))[0] as HTMLElement);
    const dialog = await screen.findByRole("dialog", { name: "Editar limpiar-temporales" });
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("No se pudo cargar el contenido actual del script.");
    expectNoEnglish(dialog);
  });

  it("asks for the target, then for confirmation, before running as SYSTEM", async () => {
    const user = userEvent.setup();
    const api = baseApi();
    renderPage(<ScriptsPage />, "/deploy/scripts");
    await user.click((await screen.findAllByRole("button", { name: "Ejecutar" }))[0] as HTMLElement);
    const dialog = await screen.findByRole("dialog", { name: "Ejecutar limpiar-temporales" });
    expect(within(dialog).getByRole("option", { name: "dispositivo" })).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "grupo" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Ejecutar script" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("El campo Destino es obligatorio");
    await pick(user, "Destino", /WKS-042/, dialog);
    await user.click(within(dialog).getByRole("button", { name: "Ejecutar script" }));
    const confirm = await screen.findByRole("dialog", { name: "Ejecutar script como SYSTEM" });
    expect(confirm).toHaveTextContent("limpiar-temporales en WKS-042");
    expect(confirm).toHaveTextContent("Ejecuta este script con derechos de administrador completos en todos los dispositivos del destino.");
    expectNoEnglish(confirm);
    expect(api.runScript).not.toHaveBeenCalled();
  });
});

describe("the schedules panel, in Spanish", () => {
  beforeEach(() => {
    baseApi({
      listScriptPolicies: vi.fn().mockResolvedValue([POLICY]),
      listScriptPolicyRuns: vi.fn().mockResolvedValue([
        { id: "r1", device_id: "d1", hostname: "LAPTOP-7", script_version: 2, state: "failed", exit_code: 3, output: "Access denied", created_at: "2026-09-23T01:00:00Z", finished_at: "2026-09-23T01:00:05Z" },
        { id: "r2", device_id: "d2", hostname: null, script_version: 2, state: "succeeded", exit_code: 0, output: null, created_at: "2026-09-22T01:00:00Z", finished_at: "2026-09-22T01:00:05Z" },
        { id: "r3", device_id: "d3", hostname: "LAPTOP-9", script_version: 2, state: "running", exit_code: null, output: null, created_at: "2026-09-21T01:00:00Z", finished_at: null },
      ]),
    });
  });

  it("words the runs dialog", async () => {
    const user = userEvent.setup();
    renderPage(<SchedulesPanel scripts={SCRIPTS} canRead canSchedule />);
    await screen.findByText("Limpieza nocturna");
    await user.click(screen.getByRole("button", { name: "Ejecuciones" }));
    const dialog = await screen.findByRole("dialog", { name: "Ejecuciones de Limpieza nocturna" });
    expect(await within(dialog).findByText("Fallida (salida 3)")).toBeInTheDocument();
    expect(within(dialog).getByText("Correcta")).toBeInTheDocument();
    expect(within(dialog).getByText("En ejecución")).toBeInTheDocument();
    expect(within(dialog).getByText(/Dispositivo sin nombre/)).toBeInTheDocument();
    expect(within(dialog).getByText("La salida ya no se conserva.")).toBeInTheDocument();
    expect(within(dialog).getByText(/23 sept 2026/)).toBeInTheDocument();
    // "Access denied" is the script output a run recorded; "Access" alone is the same recorded
    // word, and since 10b also a catalogue fragment (the report catalogue's "Access" column).
    expectNoEnglish(dialog, ["Access denied", "Access"]);
  });

  it("words the New schedule dialog, its interval choices and its refusal", async () => {
    const user = userEvent.setup();
    renderPage(<SchedulesPanel scripts={SCRIPTS} canRead canSchedule />);
    await user.click(await screen.findByRole("button", { name: "Nueva programación" }));
    const dialog = await screen.findByRole("dialog", { name: "Nueva programación" });
    for (const label of ["Cada hora", "Cada 4 horas", "Cada día", "Cada semana"]) {
      expect(within(dialog).getByRole("option", { name: label })).toBeInTheDocument();
    }
    await user.click(within(dialog).getByRole("switch", { name: "Generar una alerta cuando falle" }));
    expect(within(dialog).getByText(/La alerta pasa por el perfil de salud del equipo/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Crear programación" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("El campo Nombre es obligatorio");
    expect(alert).toHaveTextContent("El campo Script es obligatorio");
    expect(alert).toHaveTextContent("El campo Grupo de dispositivos es obligatorio");
    expectNoEnglish(dialog);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent("Device group is required");
    expect(screen.getByRole("option", { name: "Every 4 hours" })).toBeInTheDocument();
  });

  it("asks before deleting a schedule", async () => {
    const user = userEvent.setup();
    renderPage(<SchedulesPanel scripts={SCRIPTS} canRead canSchedule />);
    await screen.findByText("Limpieza nocturna");
    await user.click(screen.getByRole("button", { name: "Eliminar" }));
    const confirm = await screen.findByRole("dialog", { name: "Eliminar programación" });
    expect(confirm).toHaveTextContent("Deja de ejecutarse. Sus ejecuciones anteriores se quedan en el historial del script.");
    expectNoEnglish(confirm);
  });
});

// ------------------------------------------------------------ a device's tab

describe("a device's Deploy tab, in Spanish", () => {
  const HISTORY: DeviceDeploymentHistory[] = [
    { deployment_id: "dep-a", client_id: "o1", package_id: "pkg1", action: "install", state: "installed", exit_code: 0, log_ref: null, finished_at: "2026-09-23T01:00:00Z", created_at: "2026-09-23T00:00:00Z" },
    { deployment_id: "dep-b", client_id: "o1", package_id: "pkg1", action: "uninstall", state: "not_applicable", exit_code: null, log_ref: null, finished_at: null, created_at: "2026-09-22T00:00:00Z" },
    { deployment_id: "dep-c", client_id: "o1", package_id: "pkg1", action: "update", state: "failed", exit_code: 1603, log_ref: null, finished_at: "2026-09-21T01:00:00Z", created_at: "2026-09-21T00:00:00Z" },
  ];

  it("words the history table", async () => {
    baseApi({ getDeviceDeployments: vi.fn().mockResolvedValue(HISTORY) });
    const { container } = renderPage(<DeviceDeployTab deviceId="dev1" />);
    await screen.findByText("dep-a");
    for (const header of ["Despliegue", "Paquete", "Acción", "Estado", "Código de salida", "Finalizado"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(screen.getByText("Instalado")).toBeInTheDocument();
    expect(screen.getByText("No aplica")).toBeInTheDocument();
    expect(screen.getByText("Fallido")).toBeInTheDocument();
    expect(screen.getByText("desinstalar")).toBeInTheDocument();
    expect(screen.getByText("Sin código de salida registrado")).toBeInTheDocument();
    expectNoEnglish(container);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Not applicable")).toBeInTheDocument();
    expect(screen.getByText("uninstall")).toBeInTheDocument();
  });

  it("words the empty tab", async () => {
    baseApi({ getDeviceDeployments: vi.fn().mockResolvedValue([]) });
    const { container } = renderPage(<DeviceDeployTab deviceId="dev1" />);
    expect(await screen.findByText("No hay despliegues registrados")).toBeInTheDocument();
    expect(screen.getByText("Crea uno desde Despliegue de software para enviar un paquete a este dispositivo.")).toBeInTheDocument();
    expectNoEnglish(container);
  });
});

describe("the run-a-script dialog on a device, in Spanish", () => {
  it("offers the scripts that fit first, says which do not, and confirms before sending", async () => {
    const user = userEvent.setup();
    const api = baseApi();
    renderPage(
      <RunScriptOnDeviceDialog open onClose={() => {}} deviceId="dev1" hostname="WKS-042" osFamily="windows" onDispatched={() => {}} />,
    );
    const dialog = await screen.findByRole("dialog", { name: "Ejecutar un script en WKS-042" });
    await user.click(within(dialog).getByRole("button", { name: "Ejecutar script" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("El campo Script es obligatorio");
    await user.click(within(dialog).getByRole("combobox", { name: "Script" }));
    expect(await screen.findByRole("option", { name: /rotar-logs/ })).toHaveTextContent("para Linux, no para este dispositivo");
    await user.click(screen.getByRole("option", { name: /limpiar-temporales/ }));
    await user.click(within(dialog).getByRole("button", { name: "Ejecutar script" }));
    const confirm = await screen.findByRole("dialog", { name: "Ejecutar script como SYSTEM" });
    expect(confirm).toHaveTextContent("limpiar-temporales en WKS-042");
    expect(confirm).toHaveTextContent("Ejecuta este script en el dispositivo con derechos de administrador completos.");
    expectNoEnglish(confirm);
    expect(api.runScript).not.toHaveBeenCalled();
  });

  it("words an empty script library", async () => {
    const user = userEvent.setup();
    baseApi({ listScripts: vi.fn().mockResolvedValue([]) });
    renderPage(
      <RunScriptOnDeviceDialog open onClose={() => {}} deviceId="dev1" hostname="WKS-042" osFamily="windows" onDispatched={() => {}} />,
    );
    const dialog = await screen.findByRole("dialog", { name: "Ejecutar un script en WKS-042" });
    await user.click(within(dialog).getByRole("combobox", { name: "Script" }));
    expect(await screen.findByText("La biblioteca de scripts está vacía. Añade uno en Software y luego en Scripts.")).toBeInTheDocument();
  });
});

// --------------------------------------------------------------- readiness

describe("the readiness panel, in Spanish", () => {
  const fleet = (managed: number) => ({
    items: [],
    total: managed,
    state_counts: { all: 0, online: 0, offline: 0, assets: 0, certs: 0 },
    fleet: { total: managed, managed, online: managed, cert_attention: 0 },
  });

  it("names the packages that have no installer, agreeing with their number", async () => {
    baseApi({ listDevices: vi.fn().mockResolvedValue(fleet(0)) });
    const { container } = renderPage(<DeployReadiness />);
    expect(await screen.findByText("Antes de poder desplegar software")).toBeInTheDocument();
    expect(await screen.findByText("Todos los paquetes tienen su instalador subido")).toBeInTheDocument();
    expect(await screen.findByText(/Cliente VPN 3\.1 no tiene un instalador subido, así que se rechaza desplegarlo\./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Subir el instalador" })).toHaveAttribute("href", "/deploy/packages");
    expect(screen.getByText("Un despliegue se instala mediante el agente de cada equipo, y todavía ningún equipo lo ejecuta.")).toBeInTheDocument();
    expectNoEnglish(container);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText(/Cliente VPN 3\.1 has no installer uploaded, so deploying it is refused\./)).toBeInTheDocument();
  });

  it("uses the plural for several packages", async () => {
    baseApi({
      listPackages: vi.fn().mockResolvedValue([PACKAGES[1], { ...(PACKAGES[1] as Package), id: "pkg3", name: "Agente CRM" }]),
      listDevices: vi.fn().mockResolvedValue(fleet(2)),
    });
    renderPage(<DeployReadiness />);
    expect(await screen.findByText(/no tienen un instalador subido, así que se rechaza desplegarlos\./)).toBeInTheDocument();
  });
});

// ------------------------------------------- sentences built outside React

describe("the deploy sentences built outside React, in Spanish", () => {
  beforeEach(() => setActiveLanguage("es"));

  it("puts a maintenance window into words", () => {
    expect(windowLabel("0 2-4 * * 6")).toBe("Sáb, de 02:00 a 05:00");
    expect(windowLabel("0 22-23 * * 1-5")).toBe("Entre semana, de 22:00 a 24:00");
    expect(windowLabel("0 1-4 * * *")).toBe("Todos los días, de 01:00 a 05:00");
    expect(windowLabel("0 3 * * 0,6")).toBe("Fines de semana, de 03:00 a 04:00");
    expect(windowLabel("0 0-23 * * 1")).toBe("Lun, todo el día");
    expect(windowLabel("0 2-3,22-23 * * 1")).toBe("Lun, de 02:00 a 04:00 y de 22:00 a 24:00");
    expect(windowLabel("0 0 * * 1-3")).toBe("Lun a mié, de 00:00 a 01:00");
    expect(windowLabel("30 2 1 * *")).toBeNull();
  });

  it("words what is wrong with a window, with the example in the current language", () => {
    expect(cronProblem("0 2 * *")).toBe(
      "La ventana debe ser una expresión cron de 5 campos: minuto hora día-del-mes mes día-de-la-semana, p. ej. 0 2 * * 6",
    );
    expect(cronProblem("0 2 * * SAT")).toBe(
      "El campo de día de la semana de la ventana solo admite números: no se admiten nombres como «SAT». El domingo es 0 o 7 y el sábado, 6.",
    );
    expect(cronProblem("0 25 * * 6")).toBe("El campo de hora de la ventana debe estar entre 0 y 23.");
    expect(cronProblem("a 2 * * 6")).toBe("El campo de minuto de la ventana solo admite números.");
    expect(cronProblem("0,,5 2 * * 6")).toBe("El campo de minuto de la ventana tiene un valor vacío en su lista.");
    expect(cronProblem("0/x 2 * * 6")).toBe("El paso del campo de minuto de la ventana debe ser un número entero.");
    expect(cronPlaceholder()).toBe("p. ej. 0 2 * * 6");
    setActiveLanguage("en");
    expect(cronPlaceholder()).toBe("e.g. 0 2 * * 6");
  });

  it("words schedules and agrees their counts", () => {
    expect(intervalLabel(1)).toBe("Cada hora");
    expect(intervalLabel(48)).toBe("Cada 2 días");
    expect(intervalLabel(5)).toBe("Cada 5 horas");
    expect(whenLabel({ interval_hours: 168, in_maintenance_window: false })).toBe("Cada semana, en cualquier momento");
    expect(outcomeLabel({ devices_failing: 1, devices_succeeding: 0 }).label).toBe("Falla en 1 equipo");
    expect(outcomeLabel({ devices_failing: 0, devices_succeeding: 3 }).label).toBe("Correcta");
    expect(outcomeLabel({ devices_failing: 0, devices_succeeding: 0 }).label).toBe("Aún sin ejecutar");
  });

  it("words the picker options and hints", () => {
    expect(devicePickerOptions(DEVICES).map((o) => o.sublabel)).toEqual(["windows · en línea", "linux · sin conexión", "linux · sin agente"]);
    expect(groupPickerOptions([GROUP, SOLO_GROUP]).map((o) => o.sublabel)).toEqual(["3 dispositivos", "1 dispositivo"]);
    expect(packagePickerOptions(PACKAGES).map((o) => o.sublabel)).toEqual(["windows · msi", "linux · deb · sin archivo"]);
    expect(devicePickerEmptyHint()).toContain("Dispositivos → Añadir un equipo");
    expect(groupPickerEmptyHint()).toContain("Administración → Grupos de dispositivos");
    expect(scriptOptions(SCRIPTS, "windows").map((o) => o.sublabel)).toEqual([
      "PowerShell · Windows",
      "Bash · para Linux, no para este dispositivo",
    ]);
  });

  it("words the interpreters, the actions and the strip, and follows a switch", () => {
    expect(interpreterLabel("cmd")).toBe("Símbolo del sistema");
    expect(interpreterLabel("  PowerShell ")).toBe("PowerShell");
    expect(interpreterLabel("fish")).toBe("Fish");
    expect(deploymentActionLabel("uninstall")).toBe("Desinstalar");
    expect(DEPLOY_NAV_ITEMS.map((item) => item.label)).toEqual(["Despliegues", "Paquetes", "Scripts"]);
    setActiveLanguage("en");
    expect(interpreterLabel("cmd")).toBe("Command Prompt");
    expect(DEPLOY_NAV_ITEMS.map((item) => item.label)).toEqual(["Deployments", "Packages", "Scripts"]);
  });
});
