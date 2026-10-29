/**
 * People in Spanish: the person page (what someone holds, the starter and
 * leaver checklist with its dialogs), the "Needs a person" list, the picker
 * other pages open to link a seat, licence or device, and the labels built
 * outside React. Names, emails, hostnames, product and service names are
 * recorded data; so are a checklist step's title, help and evidence, which the
 * server words in the requested language (the fixtures word them in Spanish).
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { en } from "../../i18n/en";
import { es } from "../../i18n/es";
import { setActiveLanguage, translate } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import { ApiError } from "../../lib/api";
import type { ApiClient, Me } from "../../lib/api";
import {
  CHECKLIST_LABEL,
  STATUS_LABEL,
  accountStatusLabel,
  assignedSourceNote,
  lifecycleLabel,
  linkSourceLabel,
} from "./labels";
import { DeviceAssignedTo, PersonLinkForUser, PersonPickerDialog } from "./links";
import { PersonPage } from "./PersonPage";
import { UnmatchedPage } from "./UnmatchedPage";
import type {
  ChecklistOut,
  DevicePersonOut,
  PersonDetailOut,
  PersonListRow,
  UnmatchedOut,
} from "./types";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function mount(ui: ReactElement, path: string, route: string, spanish = true) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const tree = (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path={path} element={ui} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  return spanish ? renderInSpanish(tree) : render(tree);
}

function me(...permissions: string[]): Me {
  return {
    user_id: "u1",
    org_id: "o1",
    email: "it@corp.example",
    display_name: "IT",
    permissions,
    timezone: "UTC",
  };
}

const MARIA: PersonListRow = {
  id: "p1",
  client_id: "o1",
  display_name: "Maria Garcia",
  email: "maria@corp.example",
  user_id: "u9",
  status: "active",
  department: "Finanzas",
  job_title: "Contable",
  start_date: "2024-01-08",
  end_date: null,
  source: "account",
  created_at: "2026-09-01T09:00:00Z",
  device_count: 1,
};

/** Recorded data the pages print as it is. */
const RECORDED = ["Maria Garcia", "maria@corp.example", "FIN-LT-042", "Slack", "Zoom", "Pat Doe", "pat@corp.example"];

const NOTHING_UNMATCHED: UnmatchedOut = { seats: [], devices: [], allocations: [] };

function leaverChecklist(): ChecklistOut {
  return {
    id: "c1",
    kind: "leaver",
    status: "open",
    started_at: "2026-09-20T09:00:00Z",
    closed_at: null,
    can_complete: false,
    steps: [
      {
        key: "access",
        position: 0,
        title: "Desactivar su acceso",
        help: "Ya no puede usar la consola ni el portal.",
        link: "/settings",
        destructive: true,
        checked: false,
        evidence: "Su acceso sigue activo",
        marked: "pending",
        state: "pending",
        note: "",
        updated_at: null,
      },
      {
        key: "tickets",
        position: 1,
        title: "Cerrar sus tickets abiertos",
        help: "Resuelve o cierra lo que haya creado.",
        link: "/service-desk",
        destructive: false,
        checked: true,
        evidence: "Sin tickets abiertos",
        marked: "pending",
        state: "done",
        note: "",
        updated_at: null,
      },
      {
        key: "seats",
        position: 2,
        title: "Recuperar sus suscripciones",
        help: "",
        link: "/subscriptions",
        destructive: false,
        checked: null,
        evidence: "Sigue teniendo una",
        marked: "skipped",
        state: "skipped",
        note: "Lo hará otra persona",
        updated_at: null,
      },
      {
        key: "devices",
        position: 3,
        title: "Recoger sus equipos",
        help: "",
        link: "/devices",
        destructive: false,
        checked: true,
        evidence: "Ninguno a su nombre",
        marked: "done",
        state: "done",
        note: "Entregado en recepción",
        updated_at: null,
      },
    ],
  };
}

function detail(overrides: Partial<PersonDetailOut> = {}): PersonDetailOut {
  const { device_count: _count, ...person } = MARIA;
  return {
    person,
    logins: [{ id: "l1", login: "corp\\maria", created_at: "2026-09-01T09:00:00Z" }],
    account: {
      user_id: "u9",
      kind: "staff",
      email: "maria@corp.example",
      status: "active",
      last_login_at: "2026-09-22T08:00:00Z",
      console: false,
      roles: ["requester"],
    },
    devices: [
      {
        device_id: "d1",
        hostname: "FIN-LT-042",
        os_family: "windows",
        lifecycle: "in_service",
        device_group_id: null,
        group_name: null,
        last_seen_at: null,
        source: "inventory",
        observed_login: "CORP\\maria",
        observed_at: null,
      },
    ],
    licences: [
      {
        allocation_id: "a1",
        license_id: "lic1",
        product_name: "Zoom",
        through_device_id: "d1",
        allocated_at: "2026-03-01T09:00:00Z",
      },
    ],
    seats: [
      {
        seat_id: "s1",
        subscription_id: "sub1",
        service_name: "Slack",
        plan: "Pro",
        assigned_at: "2026-02-01T09:00:00Z",
      },
    ],
    tickets: [
      {
        ticket_id: "t1",
        number: "TKT-7",
        subject: "Portátil lento",
        status: "pending",
        priority: "high",
        created_at: "2026-09-10T09:00:00Z",
      },
    ],
    checklist: null,
    past_checklists: [],
    ...overrides,
  };
}

function api(overrides: Partial<Record<keyof ApiClient, unknown>>, perms: string[]): ApiClient {
  return {
    me: vi.fn().mockResolvedValue(me(...perms)),
    listUnmatched: vi.fn().mockResolvedValue(NOTHING_UNMATCHED),
    ...overrides,
  } as unknown as ApiClient;
}

function devicePerson(overrides: Partial<DevicePersonOut> = {}): DevicePersonOut {
  return {
    device_id: "d1",
    person: MARIA,
    source: "operator",
    observed_login: "CORP\\maria",
    observed_at: null,
    ...overrides,
  };
}

beforeEach(() => {
  mockUseApi.mockReset();
});

describe("the person page in Spanish", () => {
  it("shows the details, the sign-in and everything the person holds", async () => {
    mockUseApi.mockReturnValue(api({ getPerson: vi.fn().mockResolvedValue(detail()) }, ["people.read"]));
    const { container } = mount(<PersonPage />, "/people/:personId", "/people/p1");
    expect(await screen.findByRole("heading", { name: "Maria Garcia" })).toBeInTheDocument();
    expect(screen.getByText("Trabaja aquí")).toBeInTheDocument();
    for (const title of ["Detalles", "Inicio de sesión", "Equipos", "Licencias", "Suscripciones", "Tickets abiertos"]) {
      expect(screen.getByText(title, { selector: ".panel-title, .panel-head *, h2, h3" })).toBeInTheDocument();
    }
    expect(screen.getByText("Correo electrónico")).toBeInTheDocument();
    expect(screen.getByText("Inicia sesión en un equipo como")).toBeInTheDocument();
    expect(screen.getByText("Puede iniciar sesión")).toBeInTheDocument();
    expect(screen.getByText("Solo el portal")).toBeInTheDocument();
    expect(screen.getByText("Según quién inicia sesión")).toBeInTheDocument();
    // The account's role is the seeded `requester`: it reads in Spanish.
    expect(screen.getByText("Solicitante")).toBeInTheDocument();
    expect(screen.getByText("En uso")).toBeInTheDocument();
    expect(screen.getByText("A través de su equipo")).toBeInTheDocument();
    expect(screen.getByText("Pendiente")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Licencias" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Asignada" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Creado" })).toBeInTheDocument();
    // Read-only: no checklist or release controls.
    expect(screen.queryByRole("button", { name: /Empezar/ })).toBeNull();
    // The department and job title are recorded; the lines around them are Spanish.
    expectNoEnglish(container, [...RECORDED, "Pro"]);
  });

  it("says a hidden section is hidden, an empty one empty, and what has no sign-in", async () => {
    mockUseApi.mockReturnValue(
      api(
        {
          getPerson: vi.fn().mockResolvedValue(
            detail({ account: null, devices: [], licences: null, seats: [], tickets: null, logins: [] }),
          ),
        },
        ["people.read"],
      ),
    );
    const { container } = mount(<PersonPage />, "/people/:personId", "/people/p1");
    await screen.findByRole("heading", { name: "Maria Garcia" });
    expect(screen.getByText(/^No tiene inicio de sesión\./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Crea una cuenta en Configuración" })).toHaveAttribute("href", "/settings");
    expect(screen.getByText(/^No hay ningún equipo vinculado a la persona\./)).toBeInTheDocument();
    expect(screen.getByText("Oculto: no tienes permiso para ver las licencias.")).toBeInTheDocument();
    expect(screen.getByText("Sin suscripciones.")).toBeInTheDocument();
    expect(screen.getByText("Oculto: no tienes permiso para ver los tickets.")).toBeInTheDocument();
    expect(screen.getByText("Se aprende cuando alguien vincula un equipo a la persona")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });

  it("says when the person is not found", async () => {
    mockUseApi.mockReturnValue(api({ getPerson: vi.fn().mockResolvedValue(null) }, ["people.read"]));
    const { container } = mount(<PersonPage />, "/people/:personId", "/people/p1");
    expect(await screen.findByText("No se encontró a la persona.")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("offers the checklists, and asks before starting one", async () => {
    const user = userEvent.setup();
    const startChecklist = vi.fn().mockResolvedValue(leaverChecklist());
    mockUseApi.mockReturnValue(
      api({ getPerson: vi.fn().mockResolvedValue(detail()), startChecklist }, ["people.read", "people.manage", "people.offboard"]),
    );
    mount(<PersonPage />, "/people/:personId", "/people/p1");
    expect(await screen.findByRole("button", { name: "Editar" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Empezar nueva incorporación" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Empezar salida" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Empezar la lista de verificación de salida" })).toBeInTheDocument();
    expect(within(dialog).getByText(/^Aparecerá como persona que se va\./)).toBeInTheDocument();
    expectNoEnglish(dialog, RECORDED);
    await user.click(within(dialog).getByRole("button", { name: "Empezar" }));
    expect(startChecklist).toHaveBeenCalledWith("p1", "leaver");
  });

  it("shows the open checklist, its steps and its confirmations", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(
      api(
        {
          getPerson: vi.fn().mockResolvedValue(
            detail({ checklist: leaverChecklist(), person: { ...detail().person, status: "leaving" } }),
          ),
        },
        ["people.read", "people.manage", "people.offboard"],
      ),
    );
    const { container } = mount(<PersonPage />, "/people/:personId", "/people/p1");
    expect(await screen.findByText("Lista de verificación de salida")).toBeInTheDocument();
    expect(screen.getByText("Se va")).toBeInTheDocument();
    expect(screen.getByText("Pasos pendientes: 1 de 4.")).toBeInTheDocument();
    expect(screen.getByText("Omitido: Lo hará otra persona", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("Marcado como hecho: Entregado en recepción", { exact: false })).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Ir a la página" })).toHaveLength(4);
    expect(screen.getByRole("button", { name: "Terminar" })).toHaveAttribute("title", "Termina u omite antes todos los pasos");
    expect(screen.getByRole("button", { name: "Cancelar la lista" })).toBeInTheDocument();
    expect(screen.getByText("Hecho")).toBeInTheDocument();
    // The skipped step and the step marked by hand can each be undone.
    expect(screen.getAllByRole("button", { name: "Deshacer" })).toHaveLength(2);
    expectNoEnglish(container, RECORDED);

    await user.click(screen.getByRole("button", { name: "Cancelar la lista" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Cancelar la lista de verificación de salida" })).toBeInTheDocument();
    expect(within(dialog).getByText(/^Las marcas registradas hasta ahora/)).toBeInTheDocument();
    expectNoEnglish(dialog, RECORDED);
  });

  it("asks why before marking done what the data disagrees with, or skipping", async () => {
    const user = userEvent.setup();
    const updateChecklistStep = vi.fn().mockResolvedValue(leaverChecklist());
    mockUseApi.mockReturnValue(
      api(
        {
          getPerson: vi.fn().mockResolvedValue(detail({ checklist: leaverChecklist() })),
          updateChecklistStep,
        },
        ["people.read", "people.manage", "people.offboard"],
      ),
    );
    mount(<PersonPage />, "/people/:personId", "/people/p1");
    await user.click(await screen.findByRole("button", { name: "Marcar como hecho" }));
    let dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Marcar como hecho: Desactivar su acceso" })).toBeInTheDocument();
    expect(within(dialog).getByText("ITMS sigue detectando: Su acceso sigue activo. Indica por qué se da por hecho de todos modos.")).toBeInTheDocument();
    expect(within(dialog).getByText("Motivo")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Atrás" })).toBeInTheDocument();
    expectNoEnglish(dialog, RECORDED);
    await user.click(within(dialog).getByRole("button", { name: "Atrás" }));

    await user.click(screen.getByRole("button", { name: "Omitir" }));
    dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Omitir: Desactivar su acceso" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Omitir paso" })).toBeDisabled();
    await user.type(within(dialog).getByRole("textbox"), "No aplica");
    await user.click(within(dialog).getByRole("button", { name: "Omitir paso" }));
    expect(updateChecklistStep).toHaveBeenCalledWith("p1", "c1", "access", { state: "skipped", note: "No aplica" });
  });

  it("asks to confirm a destructive step, and finishes a starter checklist", async () => {
    const user = userEvent.setup();
    const starter: ChecklistOut = {
      ...leaverChecklist(),
      kind: "starter",
      can_complete: true,
      steps: [{ ...leaverChecklist().steps[0]!, checked: true, evidence: "Ya tiene acceso" }],
    };
    mockUseApi.mockReturnValue(
      api({ getPerson: vi.fn().mockResolvedValue(detail({ checklist: starter })) }, ["people.read", "people.manage"]),
    );
    const { container } = mount(<PersonPage />, "/people/:personId", "/people/p1");
    expect(await screen.findByText("Lista de verificación de nueva incorporación")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Marcar como hecho" }));
    const mark = await screen.findByRole("dialog");
    expect(within(mark).getByText("Confirma que esto se ha hecho. Tu nombre queda registrado junto a la acción.")).toBeInTheDocument();
    expect(within(mark).getByText("Nota (opcional)")).toBeInTheDocument();
    await user.click(within(mark).getByRole("button", { name: "Atrás" }));
    await user.click(screen.getByRole("button", { name: "Terminar" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Terminar la lista de verificación de nueva incorporación" })).toBeInTheDocument();
    expect(within(dialog).getByText("Aparecerá como persona que trabaja aquí.")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });

  it("says every step is done once none is left", async () => {
    const finished: ChecklistOut = {
      ...leaverChecklist(),
      can_complete: true,
      steps: leaverChecklist().steps.map((step) => ({ ...step, state: "done" as const })),
    };
    mockUseApi.mockReturnValue(
      api({ getPerson: vi.fn().mockResolvedValue(detail({ checklist: finished })) }, ["people.read", "people.manage", "people.offboard"]),
    );
    const { container } = mount(<PersonPage />, "/people/:personId", "/people/p1");
    expect(
      await screen.findByText("Todos los pasos están hechos. Termina la lista de verificación para registrarla."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Terminar" })).toBeEnabled();
    expectNoEnglish(container, RECORDED);
  });

  it("lists the past checklists with their outcome and date", async () => {
    mockUseApi.mockReturnValue(
      api(
        {
          getPerson: vi.fn().mockResolvedValue(
            detail({
              past_checklists: [
                { id: "x1", kind: "starter", status: "completed", closed_at: "2026-01-10T09:00:00Z" },
                { id: "x2", kind: "leaver", status: "cancelled", closed_at: "2026-02-10T09:00:00Z" },
                { id: "x3", kind: "mover", status: "completed", closed_at: "2026-03-10T09:00:00Z" },
              ],
            }),
          ),
        },
        ["people.read"],
      ),
    );
    const { container } = mount(<PersonPage />, "/people/:personId", "/people/p1");
    expect(await screen.findByText("Listas de verificación anteriores")).toBeInTheDocument();
    const items = [...container.querySelectorAll("li")].map((li) => li.textContent ?? "");
    expect(items[0]).toMatch(/^Nueva incorporación: terminada el /);
    expect(items[1]).toMatch(/^Salida: cancelada el /);
    // A kind the console has not met is humanised, never printed as the wire word.
    expect(items[2]).toMatch(/^Mover: terminada el /);
    expectNoEnglish(container, [...RECORDED, "Mover"]);
  });

  it("confirms before releasing a seat, a licence or a sign-in", async () => {
    const user = userEvent.setup();
    const releaseSeat = vi.fn().mockResolvedValue({});
    mockUseApi.mockReturnValue(
      api(
        { getPerson: vi.fn().mockResolvedValue(detail()), releaseSeat },
        ["people.read", "subscriptions.manage", "licenses.manage", "users.manage"],
      ),
    );
    mount(<PersonPage />, "/people/:personId", "/people/p1");
    const buttons = await screen.findAllByRole("button", { name: "Liberar" });
    expect(buttons).toHaveLength(2);
    expect(screen.getAllByRole("columnheader", { name: "Recuperar" })).toHaveLength(2);

    await user.click(buttons[1]!);
    let dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Liberar este puesto de suscripción" })).toBeInTheDocument();
    expect(within(dialog).getByText(/^Vuelve al conjunto para que lo use otra persona\./)).toBeInTheDocument();
    expectNoEnglish(dialog, RECORDED);
    await user.click(within(dialog).getByRole("button", { name: "Cancelar" }));

    await user.click(buttons[0]!);
    dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Liberar esta licencia" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Cancelar" }));

    await user.click(screen.getByRole("button", { name: "Desactivar el inicio de sesión" }));
    dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Desactivar su inicio de sesión" })).toBeInTheDocument();
    expect(within(dialog).getByText(/^La persona ya no podrá usar la consola ni el portal\./)).toBeInTheDocument();
    expectNoEnglish(dialog, RECORDED);
  });

  it("tells how a client's contact is turned back on, and not Settings", async () => {
    const user = userEvent.setup();
    const account = detail().account;
    if (account === null) throw new Error("the fixture has an account");
    mockUseApi.mockReturnValue(
      api(
        { getPerson: vi.fn().mockResolvedValue(detail({ account: { ...account, kind: "contact" } })) },
        ["people.read", "clients.manage"],
      ),
    );
    mount(<PersonPage />, "/people/:personId", "/people/p1");
    await user.click(await screen.findByRole("button", { name: "Desactivar el inicio de sesión" }));
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByText(
        "La persona ya no podrá usar el portal. Hasta que la consola pueda editar los contactos de un cliente, se vuelve a activar mediante la API o una conexión de IA (MCP).",
      ),
    ).toBeInTheDocument();
    expect(within(dialog).queryByText(/Configuración/)).toBeNull();
    expectNoEnglish(dialog, RECORDED);
  });

  it("words a failed release from the held error, and re-words it on a switch", async () => {
    const user = userEvent.setup();
    const releaseSeat = vi.fn().mockRejectedValue(new ApiError(503, ""));
    mockUseApi.mockReturnValue(
      api({ getPerson: vi.fn().mockResolvedValue(detail({ licences: [] })), releaseSeat }, ["people.read", "subscriptions.manage"]),
    );
    mount(<PersonPage />, "/people/:personId", "/people/p1");
    await user.click(await screen.findByRole("button", { name: "Liberar" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Liberar" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/no está disponible/i);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert").textContent).toMatch(/unavailable/i);
    act(() => setActiveLanguage("es"));
    expect(screen.getByRole("alert").textContent).toMatch(/no está disponible/i);
  });

  it("edits the person in Spanish", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api({ getPerson: vi.fn().mockResolvedValue(detail()) }, ["people.read", "people.manage"]));
    mount(<PersonPage />, "/people/:personId", "/people/p1");
    await user.click(await screen.findByRole("button", { name: "Editar" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Editar persona" })).toBeInTheDocument();
    for (const label of ["Nombre", "Correo electrónico", "Departamento", "Cargo", "Fecha de incorporación", "Fecha de salida"]) {
      expect(within(dialog).getByLabelText(label)).toBeInTheDocument();
    }
    expect(within(dialog).getByRole("button", { name: "Guardar" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Cancelar" })).toBeInTheDocument();
    expectNoEnglish(dialog, RECORDED);
  });

  it("re-words the page on a live switch", async () => {
    mockUseApi.mockReturnValue(api({ getPerson: vi.fn().mockResolvedValue(detail({ checklist: leaverChecklist() })) }, ["people.read", "people.manage"]));
    mount(<PersonPage />, "/people/:personId", "/people/p1");
    expect(await screen.findByText("Pasos pendientes: 1 de 4.")).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("1 of 4 steps left.")).toBeInTheDocument();
    expect(screen.getByText("Leaver checklist")).toBeInTheDocument();
    expect(screen.getByText("Working here")).toBeInTheDocument();
    expect(screen.getByText("The portal only")).toBeInTheDocument();
    // English shows the role as stored; Spanish words the seeded ones (and re-words on a switch).
    expect(screen.getByText("requester")).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(screen.getByText("Solicitante")).toBeInTheDocument();
    expect(screen.getByText("Lista de verificación de salida")).toBeInTheDocument();
    expect(screen.getByText("Trabaja aquí")).toBeInTheDocument();
  });
});

describe("the unmatched list in Spanish", () => {
  const FILLED: UnmatchedOut = {
    seats: [
      {
        seat_id: "s9",
        subscription_id: "sub1",
        service_name: "Zoom",
        assignee_name: "Pat Doe",
        assignee_email: "pat@corp.example",
      },
    ],
    devices: [
      { device_id: "d1", hostname: "FIN-LT-042", owner: null, observed_login: null },
      { device_id: "d2", hostname: null, owner: "Ana", observed_login: "CORP\\ana" },
    ],
    allocations: [{ allocation_id: "al1", license_id: "lic1", product_name: "Zoom", user_id: "u5" }],
  };

  it("renders the three groups", async () => {
    mockUseApi.mockReturnValue(api({ listUnmatched: vi.fn().mockResolvedValue(FILLED) }, ["people.read", "people.manage"]));
    const { container } = mount(<UnmatchedPage />, "/people/unmatched", "/people/unmatched");
    expect(await screen.findByRole("heading", { name: "Necesita una persona" })).toBeInTheDocument();
    expect(screen.getByText(/^Estos elementos nombran a alguien/)).toBeInTheDocument();
    for (const title of ["Puestos de suscripción", "Equipos", "Licencias asignadas a una cuenta"]) {
      expect(await screen.findByRole("table", { name: title })).toBeInTheDocument();
    }
    expect(screen.getByRole("button", { name: "Vincular a…" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Añadir como persona nueva" })).toBeInTheDocument();
    expect(screen.getByText("No se ha visto a nadie")).toBeInTheDocument();
    expect(screen.getByText("Equipo sin nombre")).toBeInTheDocument();
    expect(screen.getByText("Vacío")).toBeInTheDocument();
    expect(screen.getByText("Registrado como")).toBeInTheDocument();
    expect(screen.getByText("Sesión iniciada como")).toBeInTheDocument();
    expectNoEnglish(container, [...RECORDED, "Ana", "CORP\\ana"]);
  });

  it("says everything is linked, and the picker for a seat is Spanish", async () => {
    const user = userEvent.setup();
    const listPeople = vi.fn().mockResolvedValue([MARIA]);
    mockUseApi.mockReturnValue(
      api({ listUnmatched: vi.fn().mockResolvedValue(FILLED), listPeople }, ["people.read", "people.manage"]),
    );
    mount(<UnmatchedPage />, "/people/unmatched", "/people/unmatched");
    await user.click(await screen.findByRole("button", { name: "Vincular a…" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Vincular este puesto a" })).toBeInTheDocument();
    expect(within(dialog).getByRole("combobox", { name: "Persona" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Vincular" })).toBeDisabled();
    expectNoEnglish(dialog, RECORDED);
  });

  it("says when there is nothing to link", async () => {
    mockUseApi.mockReturnValue(api({}, ["people.read"]));
    const { container } = mount(<UnmatchedPage />, "/people/unmatched", "/people/unmatched");
    expect(await screen.findByText("Todo está vinculado a una persona.")).toBeInTheDocument();
    expectNoEnglish(container);
  });
});

describe("the pieces other pages use, in Spanish", () => {
  async function openPicker(overrides: Partial<Record<keyof ApiClient, unknown>>) {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(
      api(
        {
          getDevicePerson: vi.fn().mockResolvedValue(devicePerson({ person: null, source: null })),
          listPeople: vi.fn().mockResolvedValue([MARIA]),
          ...overrides,
        },
        ["people.read", "people.manage"],
      ),
    );
    const view = mount(<DeviceAssignedTo deviceId="d1" clientId="c-x" />, "/devices/:id", "/devices/d1");
    await user.click(await screen.findByRole("button", { name: "Cambiar" }));
    return { user, dialog: await screen.findByRole("dialog"), ...view };
  }

  it("reads the assigned holder as a field of the device", async () => {
    mockUseApi.mockReturnValue(api({ getDevicePerson: vi.fn().mockResolvedValue(devicePerson()) }, ["people.read", "people.manage"]));
    const { container } = mount(<DeviceAssignedTo deviceId="d1" clientId="c-x" />, "/devices/:id", "/devices/d1");
    const field = await screen.findByTestId("device-assigned-to");
    expect(field).toHaveTextContent("Asignado a: Maria Garcia · indicado a mano");
    expect(screen.getByRole("button", { name: "Cambiar" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Volver a automático" })).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });

  it("says nobody is assigned yet, and who last signed in", async () => {
    mockUseApi.mockReturnValue(
      api(
        { getDevicePerson: vi.fn().mockResolvedValue(devicePerson({ person: null, source: "inventory", observed_login: "TOESHEE\\aruiz" })) },
        ["people.read"],
      ),
    );
    const { container } = mount(<DeviceAssignedTo deviceId="d1" clientId="c-x" />, "/devices/:id", "/devices/d1");
    expect(await screen.findByTestId("device-assigned-to")).toHaveTextContent(
      "Asignado a: nadie todavía · último inicio de sesión como TOESHEE\\aruiz",
    );
    expectNoEnglish(container, ["TOESHEE\\aruiz"]);
  });

  it("opens the picker in Spanish, with Nobody among the options", async () => {
    const { user, dialog } = await openPicker({});
    expect(within(dialog).getByRole("heading", { name: "Asignar este dispositivo a" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("combobox", { name: "Persona" }));
    expect(await within(dialog).findByRole("option", { name: "Nadie" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Persona nueva…" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Guardar" })).toBeInTheDocument();
    expectNoEnglish(dialog, RECORDED);
  });

  it("explains an empty list of people, with and without the way to add one", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api({ listPeople: vi.fn().mockResolvedValue([]) }, ["people.read"]));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    renderInSpanish(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <PersonPickerDialog
            open
            title="Asignar a"
            confirmLabel="Guardar"
            allowNobody={false}
            pending={false}
            error={null}
            onCancel={() => undefined}
            onConfirm={() => undefined}
            onConfirmNew={() => undefined}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("combobox", { name: "Persona" }));
    expect(await within(dialog).findByText("Aún no hay nadie. Elige «Persona nueva» para añadirla.")).toBeInTheDocument();
    expectNoEnglish(dialog);
  });

  it("explains an empty list of people when no one can be added", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api({ listUnmatched: vi.fn().mockResolvedValue({ ...NOTHING_UNMATCHED, seats: [{ seat_id: "s9", subscription_id: "sub1", service_name: "Zoom", assignee_name: "Pat Doe", assignee_email: "pat@corp.example" }] }), listPeople: vi.fn().mockResolvedValue([]) }, ["people.read", "people.manage"]));
    mount(<UnmatchedPage />, "/people/unmatched", "/people/unmatched");
    await user.click(await screen.findByRole("button", { name: "Vincular a…" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("combobox", { name: "Persona" }));
    expect(await within(dialog).findByText("Aún no hay nadie.")).toBeInTheDocument();
    expectNoEnglish(dialog, RECORDED);
  });

  it("holds a refused new person as data and re-words it on a live switch", async () => {
    const { user, dialog } = await openPicker({});
    await user.click(within(dialog).getByRole("button", { name: "Persona nueva…" }));
    expect(within(dialog).getByText("Opcional. Los puestos y los inicios de sesión con esta dirección se vinculan a la persona.")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Elegir a alguien que ya está" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect(await within(dialog).findByText(/^El campo Nombre es obligatorio/)).toBeInTheDocument();
    const english = en.validation.required.replace("{label}", "Name");
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByText(new RegExp(`^${english}`))).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(within(dialog).queryByText(new RegExp(`^${english}`))).toBeNull();

    await user.type(within(dialog).getByLabelText("Nombre"), "Pat");
    await user.type(within(dialog).getByLabelText("Correo electrónico"), "no-es-un-correo");
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect(await within(dialog).findByText(/^Eso no es una dirección de correo electrónico/)).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByText(/^That isn't an email address/)).toBeInTheDocument();
  });

  it("words a failed assignment from the held error", async () => {
    const createPerson = vi.fn().mockRejectedValue(new ApiError(503, ""));
    const { user, dialog } = await openPicker({ createPerson });
    await user.click(within(dialog).getByRole("button", { name: "Persona nueva…" }));
    await user.type(within(dialog).getByLabelText("Nombre"), "Pat");
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect((await within(dialog).findByRole("alert")).textContent).toMatch(/no está disponible/i);
  });

  it("links a ticket requester to their person", async () => {
    mockUseApi.mockReturnValue(api({ personForUser: vi.fn().mockResolvedValue(MARIA) }, ["people.read"]));
    mount(<PersonLinkForUser userId="u9" />, "/t", "/t");
    expect(await screen.findByRole("link", { name: "Ver persona" })).toHaveAttribute("href", "/people/p1");
  });
});

describe("the labels built outside React", () => {
  it("name every wire value in both languages", () => {
    setActiveLanguage("es");
    expect(Object.values(STATUS_LABEL)).toEqual(["Se incorpora", "Trabaja aquí", "Se va", "Se fue"]);
    expect(Object.values(CHECKLIST_LABEL)).toEqual(["Nueva incorporación", "Salida"]);
    expect(["operator", "inventory", null].map(linkSourceLabel)).toEqual([
      "Indicado por una persona",
      "Según quién inicia sesión",
      "Se desconoce",
    ]);
    expect([assignedSourceNote("operator", true), assignedSourceNote("inventory", true), assignedSourceNote("inventory", false)]).toEqual([
      "indicado a mano",
      "según quién inicia sesión",
      null,
    ]);
    expect(["active", "disabled", "deleted", "other"].map(accountStatusLabel)).toEqual([
      "Puede iniciar sesión",
      "Inicio de sesión desactivado",
      "Cuenta eliminada",
      "No puede iniciar sesión",
    ]);
    expect(["in_service", "stock", "unmanageable", "retired", "other"].map(lifecycleLabel)).toEqual([
      "En uso",
      "En almacén",
      "No puede ejecutar un agente",
      "Retirado",
      "En uso",
    ]);
    setActiveLanguage("en");
    expect(Object.values(STATUS_LABEL)).toEqual(Object.values(en.people.status));
    expect(Object.values(CHECKLIST_LABEL)).toEqual(Object.values(en.people.checklistKind));
    expect(lifecycleLabel("stock")).toBe("In stock");
  });

  it("agrees the steps-left note, and keeps both catalogues the same shape", () => {
    setActiveLanguage("es");
    expect(translate("people.checklist.stepsLeft", { remaining: 1, count: 1 })).toBe("Pasos pendientes: 1 de 1.");
    expect(translate("people.checklist.stepsLeft", { remaining: 2, count: 5 })).toBe("Pasos pendientes: 2 de 5.");
    expect(Object.keys(es.people.holdings)).toEqual(Object.keys(en.people.holdings));
  });
});
