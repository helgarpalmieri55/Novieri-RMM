/**
 * Change management in Spanish: the list with its tiles, filters and gates, the
 * request form, one change (facts, lifecycle, approvals, linked actions), the
 * approval queue, the calendar, the readiness and gate prerequisites, and the
 * label tables built outside React. Change titles, descriptions, rollback plans,
 * comments, group names and ids are recorded data and read as recorded.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Prerequisites } from "../../components/prerequisites";
import { translate } from "../../i18n/core";
import { setActiveLanguage } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import { ApiError } from "../../lib/api";
import type { ApiClient, Me } from "../../lib/api";
import { ApprovalQueue } from "./ApprovalQueue";
import { ChangeCalendar } from "./ChangeCalendar";
import { availableChangeActions, hiddenActionsReason, noActionsReason } from "./changeActions";
import { ChangeDetail } from "./ChangeDetail";
import { ChangeReadiness } from "./ChangeReadiness";
import { ChangeRequestForm } from "./ChangeRequestForm";
import { changeStateLabel, changeTypeLabel, decisionLabel, linkActionLabel, riskLabel } from "./changeState";
import { ChangesPage } from "./ChangesPage";
import { GatesPanel, gateCoverage } from "./GatesPanel";
import { gatedWhere, useChangeGatePrerequisite } from "./gatePrerequisite";
import {
  deploymentPickerEmptyHint,
  deploymentPickerOptions,
  provisionRunPickerEmptyHint,
  provisionRunPickerOptions,
} from "./pickerOptions";
import type { ChangeGate, ChangeRequest } from "./types";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function mount(ui: ReactElement, route = "/") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  permissions: ["change.read", "change.request", "change.approve", "change.manage", "change.gate.manage"],
  timezone: "UTC",
};

const CHANGE_ID = "06a85c19-55e2-7f3c-8000-831c7bdba357";

const DRAFT: ChangeRequest = {
  id: "change1",
  type: "standard",
  title: "Actualizar firmware del conmutador",
  description: "Desplegar el firmware 2.1 en los conmutadores troncales.",
  risk: "medium",
  affected: [],
  window_start: "2026-02-01T00:00:00.000Z",
  window_end: "2026-02-01T04:00:00.000Z",
  rollback_plan: "Volver a la imagen de firmware 2.0.",
  state: "draft",
  created_by: "u2",
  created_at: "2026-01-01T00:00:00.000Z",
};
const SUBMITTED: ChangeRequest = { ...DRAFT, id: "change2", title: "Parchear la base de datos de producción", state: "submitted", risk: "high" };
const EMERGENCY: ChangeRequest = {
  ...DRAFT,
  id: "change4",
  title: "Reiniciar el nodo caído del clúster",
  type: "emergency",
  state: "implementing",
  risk: "low",
  origin: "auto",
};
const COMPLETED: ChangeRequest = { ...DRAFT, id: "change3", title: "Retirar el cortafuegos antiguo", state: "completed", type: "normal" };
const ALL = [DRAFT, SUBMITTED, COMPLETED, EMERGENCY];

function api(overrides: Partial<ApiClient> = {}): ApiClient {
  return {
    me: vi.fn().mockResolvedValue(ME),
    listChanges: vi.fn().mockResolvedValue(ALL),
    listChangeGates: vi.fn().mockResolvedValue([]),
    listDeviceGroups: vi.fn().mockResolvedValue({ items: [{ id: "grp", name: "Servidores" }], total: 1 }),
    getChange: vi.fn().mockResolvedValue({ ...DRAFT, id: CHANGE_ID }),
    listChangeLinks: vi.fn().mockResolvedValue([]),
    listChangeApprovals: vi.fn().mockResolvedValue([]),
    createChange: vi.fn().mockResolvedValue(DRAFT),
    getChangeCalendar: vi.fn().mockResolvedValue({ entries: [], conflicts: [] }),
    ...overrides,
  } as unknown as ApiClient;
}

beforeEach(() => {
  mockUseApi.mockReturnValue(api());
});

describe("ChangesPage in Spanish", () => {
  it("words the list, the tiles, the pills and the gates, and follows a live switch", async () => {
    const { container } = mount(<ChangesPage />);
    expect(await screen.findByText("Actualizar firmware del conmutador")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Gestión de cambios" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Cola de aprobación" })).toHaveAttribute("href", "/changes/approvals");
    expect(screen.getByRole("link", { name: "Calendario de cambios" })).toHaveAttribute("href", "/changes/calendar");
    expect(screen.getByRole("button", { name: "Nuevo cambio" })).toBeInTheDocument();
    for (const label of ["Cambios abiertos", "Pendientes de aprobación", "Cambios de emergencia"]) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
    // A change's state and risk are masculine words under "Estado" and "Riesgo".
    expect(screen.getByText("Borrador")).toBeInTheDocument();
    expect(screen.getByText("Enviado")).toBeInTheDocument();
    expect(screen.getByText("En implementación")).toBeInTheDocument();
    expect(screen.getByText("Completado")).toBeInTheDocument();
    expect(screen.getAllByText("Alto").length).toBe(1);
    expect(screen.getAllByText("Medio").length).toBe(2);
    expect(screen.getAllByText("Bajo").length).toBe(1);
    expect(screen.getByText(/Registrado automáticamente/)).toBeInTheDocument();
    expect(screen.getByText("Exigir un cambio aprobado")).toBeInTheDocument();
    expect(screen.getByText("Todavía nada exige un cambio aprobado.")).toBeInTheDocument();
    expectNoEnglish(container);

    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("heading", { name: "Change management" })).toBeInTheDocument();
    expect(screen.getByText("Draft")).toBeInTheDocument();
    expect(screen.getByText("Submitted")).toBeInTheDocument();
    expect(screen.getAllByText("High").length).toBe(1);
    expect(screen.getByText(/Recorded automatically/)).toBeInTheDocument();
    expect(screen.getByText("Nothing requires an approved change yet.")).toBeInTheDocument();
  });

  it("names the subset a tile opened, and a switch re-words it", async () => {
    const { container } = mount(<ChangesPage />, "/changes?type=emergency");
    await screen.findByText("Reiniciar el nodo caído del clúster");
    expect(screen.getByText(/^Mostrando 1 de 4: cambios de tipo emergencia\./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Mostrar todos los cambios" })).toHaveAttribute("href", "/changes");
    expect(screen.queryByText("Actualizar firmware del conmutador")).not.toBeInTheDocument();
    expectNoEnglish(container);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText(/^Showing 1 of 4: emergency changes\./)).toBeInTheDocument();
  });

  it("words the open view, a state view and an empty view", async () => {
    mount(<ChangesPage />, "/changes?state=open");
    await screen.findByText("Actualizar firmware del conmutador");
    expect(screen.getByText(/^Mostrando 3 de 4: cambios abiertos\./)).toBeInTheDocument();
  });

  it("words a state view with the state as the page words it, and the empty view", async () => {
    const { container } = mount(<ChangesPage />, "/changes?state=submitted&type=emergency");
    expect(await screen.findByText(/Ningún cambio coincide con esta vista: cambios en estado enviado, cambios de tipo emergencia\./)).toBeInTheDocument();
    expectNoEnglish(container);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText(/No change matches this view: changes in state submitted, emergency changes\./)).toBeInTheDocument();
  });

  it("words the empty list", async () => {
    mockUseApi.mockReturnValue(api({ listChanges: vi.fn().mockResolvedValue([]) }));
    const { container } = mount(<ChangesPage />);
    expect(await screen.findByText("Aún no hay cambios.")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("shows a state the console has not met in the shared status words, not as a wire word", async () => {
    mockUseApi.mockReturnValue(api({ listChanges: vi.fn().mockResolvedValue([{ ...DRAFT, state: "paused" }]) }));
    mount(<ChangesPage />);
    await screen.findByText("Actualizar firmware del conmutador");
    expect(screen.getByText("En pausa", { exact: false })).toBeInTheDocument();
  });

  it("opens the request form, words it, and a refused save re-words on a switch", async () => {
    const user = userEvent.setup();
    mount(<ChangesPage />);
    await user.click(await screen.findByRole("button", { name: "Nuevo cambio" }));
    const dialog = await screen.findByRole("dialog", { name: "Nuevo cambio" });
    expect(within(dialog).getByText("Los campos marcados con * son obligatorios.")).toBeInTheDocument();
    for (const label of ["Tipo", "Título *", "Descripción *", "Riesgo", "Inicio de la ventana", "Fin de la ventana", "Plan de reversión"]) {
      expect(within(dialog).getByText(label)).toBeInTheDocument();
    }
    expect(within(dialog).getByRole("option", { name: "Estándar (preaprobado, rutinario)" })).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "Emergencia (se aprueba después)" })).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "Alto" })).toBeInTheDocument();
    expect(within(dialog).getByText(/^Las horas de la ventana están en /)).toBeInTheDocument();
    expectNoEnglish(dialog);

    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    const refused = (await within(dialog).findByRole("alert")).textContent ?? "";
    expect(refused).toContain("El campo Título es obligatorio");
    expect(refused).toContain("El campo Descripción es obligatorio");
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByRole("alert").textContent).toContain("Title is required");
    act(() => setActiveLanguage("es"));
    expect(within(dialog).getByRole("alert").textContent).toContain("El campo Título es obligatorio");
  });
});

describe("ChangeRequestForm in Spanish", () => {
  it("refuses a window that ends before it opens, in Spanish, and re-words it on a switch", async () => {
    const user = userEvent.setup();
    mount(<ChangeRequestForm change="new" onClose={() => {}} />);
    await user.type(screen.getByLabelText("Título *"), "Cambio de prueba");
    await user.type(screen.getByLabelText("Descripción *"), "Una descripción.");
    await user.type(screen.getByLabelText("Inicio de la ventana"), "2026-08-25T13:00");
    await user.type(screen.getByLabelText("Fin de la ventana"), "2026-08-25T12:00");
    await user.click(screen.getByRole("button", { name: "Guardar" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/^El fin de la ventana debe ser posterior al inicio \(/);
    expect(alert.textContent).toContain("una ventana de mantenimiento no puede terminar antes de abrirse");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert").textContent).toMatch(/^Window end must be after Window start \(/);
  });

  it("words a failed save from the held error", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api({ createChange: vi.fn().mockRejectedValue(new ApiError(503, "")) }));
    mount(<ChangeRequestForm change="new" onClose={() => {}} />);
    await user.type(screen.getByLabelText("Título *"), "Cambio de prueba");
    await user.type(screen.getByLabelText("Descripción *"), "Una descripción.");
    await user.click(screen.getByRole("button", { name: "Guardar" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/no está disponible/i);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert").textContent).toMatch(/unavailable/i);
  });

  it("titles the dialog for an edit", async () => {
    mount(<ChangeRequestForm change={DRAFT} onClose={() => {}} />);
    expect(await screen.findByRole("dialog", { name: "Editar cambio" })).toBeInTheDocument();
  });
});

describe("GatesPanel in Spanish", () => {
  const GATE: ChangeGate = {
    id: "g1",
    device_group_id: "grp",
    device_group_name: "Servidores",
    applies_to_patches: true,
    applies_to_deployments: false,
    created_at: "2026-09-23T00:00:00Z",
  };

  it("words the table, the form and the coverage, and follows a switch", async () => {
    mockUseApi.mockReturnValue(api({ listChangeGates: vi.fn().mockResolvedValue([GATE, { ...GATE, id: "g2", device_group_id: null, device_group_name: null, applies_to_deployments: true }]) }));
    const { container } = mount(<GatesPanel canManage />);
    expect((await screen.findAllByText("Servidores")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("Todos los dispositivos").length).toBeGreaterThan(0);
    expect(screen.getByText("Aprobaciones de parches")).toBeInTheDocument();
    expect(screen.getByText("Aprobaciones de parches y despliegues")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Exigir un cambio" })).toBeInTheDocument();
    expect(screen.getByText("Las aprobaciones de parches necesitan un cambio")).toBeInTheDocument();
    expect(screen.getByText("Los despliegues necesitan un cambio")).toBeInTheDocument();
    expectNoEnglish(container);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Patch approvals and deployments")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Require a change" })).toBeInTheDocument();
  });

  it("words a refused gate from the held error", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api({ setChangeGate: vi.fn().mockRejectedValue(new ApiError(503, "")) }));
    mount(<GatesPanel canManage />);
    await user.click(await screen.findByRole("button", { name: "Exigir un cambio" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/no está disponible/i);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert").textContent).toMatch(/unavailable/i);
  });

  it("names what a gate holds back in either language", () => {
    setActiveLanguage("es");
    expect(gateCoverage({ applies_to_patches: false, applies_to_deployments: true })).toBe("Despliegues");
    setActiveLanguage("en");
    expect(gateCoverage({ applies_to_patches: false, applies_to_deployments: true })).toBe("Deployments");
  });
});

describe("ApprovalQueue in Spanish", () => {
  it("words the queue and the decision controls, and follows a switch", async () => {
    mockUseApi.mockReturnValue(api({ listChanges: vi.fn().mockResolvedValue([SUBMITTED, { ...SUBMITTED, id: "mine", title: "Retirar el cortafuegos antiguo", created_by: "u1" }]) }));
    const { container } = mount(<ApprovalQueue />);
    expect(await screen.findByText("Parchear la base de datos de producción")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Cola de aprobación" })).toBeInTheDocument();
    expect(screen.getByText("Solicitudes de cambio a la espera de una decisión.")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Decisión" })).toBeInTheDocument();
    expect(screen.getAllByText(/^Estándar · ventana /).length).toBe(2);
    expect(screen.getByLabelText("Comentario de la decisión sobre Parchear la base de datos de producción")).toBeInTheDocument();
    expect(screen.getAllByPlaceholderText("Comentario (opcional)").length).toBe(2);
    // The caller's own change cannot be approved by them; the reason is on the control.
    const own = screen.getAllByRole("button", { name: "Aprobar" })[1] as HTMLElement;
    expect(own).toBeDisabled();
    expect(own).toHaveAttribute("title", "No puedes aprobar un cambio que solicitaste");
    expectNoEnglish(container);

    act(() => setActiveLanguage("en"));
    expect(screen.getAllByRole("button", { name: "Approve" })[1]).toHaveAttribute("title", "Cannot approve your own change");
    expect(screen.getByLabelText("Decision comment for Parchear la base de datos de producción")).toBeInTheDocument();
  });

  it("words an empty queue", async () => {
    mockUseApi.mockReturnValue(api({ listChanges: vi.fn().mockResolvedValue([DRAFT]) }));
    const { container } = mount(<ApprovalQueue />);
    expect(await screen.findByText("No hay cambios pendientes de aprobación.")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words a refused decision from the held error", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(
      api({ listChanges: vi.fn().mockResolvedValue([SUBMITTED]), approveChange: vi.fn().mockRejectedValue(new ApiError(503, "")) }),
    );
    mount(<ApprovalQueue />);
    await user.click(await screen.findByRole("button", { name: "Aprobar" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/no está disponible/i);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert").textContent).toMatch(/unavailable/i);
  });
});

describe("ChangeCalendar in Spanish", () => {
  const ENTRIES = [
    { change_id: "c1", title: "Actualizar firmware del conmutador", type: "standard", risk: "high", state: "approved", window_start: "2026-02-01T00:00:00.000Z", window_end: "2026-02-01T04:00:00.000Z" },
    { change_id: "c2", title: "Parchear la base de datos de producción", type: "emergency", risk: "low", state: "implementing", window_start: "2026-02-01T02:00:00.000Z", window_end: "2026-02-01T03:00:00.000Z" },
  ];

  it("words the range, the conflicts, the table and the badge, and follows a switch", async () => {
    mockUseApi.mockReturnValue(
      api({ getChangeCalendar: vi.fn().mockResolvedValue({ entries: ENTRIES, conflicts: [{ change_a: "c1", change_b: "c2" }] }) }),
    );
    const { container } = mount(<ChangeCalendar />);
    expect(await screen.findByText("Actualizar firmware del conmutador")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Calendario de cambios" })).toBeInTheDocument();
    expect(screen.getByText("Periodo")).toBeInTheDocument();
    expect(screen.getByText(/^Desde el .* hasta el .*, en tu zona horaria\.$/)).toBeInTheDocument();
    expect(screen.getByText("1 conflicto de programación en esta ventana.")).toBeInTheDocument();
    expect(screen.getAllByText("conflicto").length).toBe(2);
    expect(screen.getByText("Aprobado")).toBeInTheDocument();
    expect(screen.getByText("Emergencia")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Todos los cambios" })).toHaveAttribute("href", "/changes");
    expectNoEnglish(container);

    act(() => setActiveLanguage("en"));
    expect(screen.getByText("1 scheduling conflict in this window.")).toBeInTheDocument();
    expect(screen.getByText(/^From .* to .*, in your timezone\.$/)).toBeInTheDocument();
    expect(screen.getAllByText("conflict").length).toBe(2);
  });

  it("pluralises the conflicts and words the empty window", async () => {
    mockUseApi.mockReturnValue(
      api({
        getChangeCalendar: vi.fn().mockResolvedValue({
          entries: ENTRIES,
          conflicts: [
            { change_a: "c1", change_b: "c2" },
            { change_a: "c2", change_b: "c1" },
          ],
        }),
      }),
    );
    mount(<ChangeCalendar />);
    expect(await screen.findByText("2 conflictos de programación en esta ventana.")).toBeInTheDocument();
  });

  it("words the empty window", async () => {
    const { container } = mount(<ChangeCalendar />);
    expect(await screen.findByText("No hay cambios programados en esta ventana.")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words the range when only one end is set", async () => {
    const user = userEvent.setup();
    mount(<ChangeCalendar />);
    await screen.findByText("No hay cambios programados en esta ventana.");
    await user.clear(screen.getByLabelText("Desde"));
    expect(await screen.findByText(/^Desde el primer cambio hasta el .*, en tu zona horaria\.$/)).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Hasta"));
    expect(screen.queryByText(/en tu zona horaria/)).not.toBeInTheDocument();
  });

  it("words the range with no end", async () => {
    const user = userEvent.setup();
    mount(<ChangeCalendar />);
    await screen.findByText("No hay cambios programados en esta ventana.");
    await user.clear(screen.getByLabelText("Hasta"));
    expect(await screen.findByText(/^Desde el .* en adelante, en tu zona horaria\.$/)).toBeInTheDocument();
  });
});

describe("ChangeDetail in Spanish", () => {
  function renderDetail() {
    return mount(
      <Routes>
        <Route path="/changes/:changeId" element={<ChangeDetail />} />
      </Routes>,
      `/changes/${CHANGE_ID}`,
    );
  }

  it("words the facts, the lifecycle and the empty tables, and follows a switch", async () => {
    const { container } = renderDetail();
    expect(await screen.findByRole("heading", { name: "Actualizar firmware del conmutador" })).toBeInTheDocument();
    expect(screen.getByText("Todo lo registrado sobre una solicitud de cambio.")).toBeInTheDocument();
    for (const label of ["Estado", "Tipo", "Riesgo", "Ventana", "Registrado"]) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
    expect(screen.getByText("Borrador")).toBeInTheDocument();
    expect(screen.getByText("Estándar")).toBeInTheDocument();
    expect(screen.getByText("Plan de reversión")).toBeInTheDocument();
    expect(screen.getByText("Ciclo de vida")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Enviar a aprobación" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancelar cambio" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Exportar evidencias" })).toBeInTheDocument();
    expect(await screen.findByText("Aún no hay decisiones de aprobación.")).toBeInTheDocument();
    expect(await screen.findByText("No hay acciones vinculadas.")).toBeInTheDocument();
    expect(document.title).toBe("Actualizar firmware del conmutador · Cambio · ITMS");
    expectNoEnglish(container);

    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("button", { name: "Submit for approval" })).toBeInTheDocument();
    expect(screen.getByText("Standard")).toBeInTheDocument();
    expect(document.title).toBe("Actualizar firmware del conmutador · Change · ITMS");
  });

  it("words the approval history, with the decision agreeing with its noun", async () => {
    mockUseApi.mockReturnValue(
      api({
        getChange: vi.fn().mockResolvedValue({ ...SUBMITTED, id: CHANGE_ID }),
        listChangeApprovals: vi.fn().mockResolvedValue([
          { id: "a1", approver_id: "u9", decision: "approved", comment: "Adelante", is_retrospective: true, decided_at: "2026-02-01T00:00:00.000Z" },
          { id: "a2", approver_id: "u8", decision: "rejected", comment: null, is_retrospective: false, decided_at: "2026-02-02T00:00:00.000Z" },
        ]),
      }),
    );
    const { container } = renderDetail();
    expect(await screen.findByText("Aprobada")).toBeInTheDocument();
    expect(screen.getByText("Rechazada")).toBeInTheDocument();
    expect(screen.getByText("sí")).toBeInTheDocument();
    expect(screen.getByText("no")).toBeInTheDocument();
    for (const header of ["Aprobador", "Decisión", "Comentario", "Retroactiva", "Decidida"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    // "Adelante" is a comment a person wrote, kept as recorded.
    expectNoEnglish(container);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Approved")).toBeInTheDocument();
    expect(screen.getByText("yes")).toBeInTheDocument();
  });

  it("asks before approving, in Spanish, and states why the caller cannot decide their own change", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(
      api({ getChange: vi.fn().mockResolvedValue({ ...SUBMITTED, id: CHANGE_ID, created_by: "u1" }) }),
    );
    renderDetail();
    expect(await screen.findByText(/^Solicitaste este cambio, así que no puedes decidir sobre él\./)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Aprobar" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancelar cambio" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Cancelar cambio" }));
    const dialog = await screen.findByRole("dialog", { name: "Cancelar este cambio" });
    expect(dialog).toHaveTextContent("Parchear la base de datos de producción");
    expect(dialog).toHaveTextContent("El cambio se cierra sin llevarse a cabo.");
    expectNoEnglish(dialog);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("dialog", { name: "Cancel this change" })).toBeInTheDocument();
  });

  it("confirms an approval with the decision comment field and the confirm label in Spanish", async () => {
    const user = userEvent.setup();
    const approveChange = vi.fn().mockResolvedValue({ id: "a1", approver_id: "u1", decision: "approved", comment: null, is_retrospective: false, decided_at: "2026-01-02T00:00:00.000Z" });
    mockUseApi.mockReturnValue(api({ getChange: vi.fn().mockResolvedValue({ ...SUBMITTED, id: CHANGE_ID }), approveChange }));
    renderDetail();
    expect(await screen.findByText("Comentario de la decisión (opcional)")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Aprobar" }));
    const dialog = await screen.findByRole("dialog", { name: "Aprobar este cambio" });
    await user.click(within(dialog).getByRole("button", { name: "Aprobar cambio" }));
    await waitFor(() => expect(approveChange).toHaveBeenCalledWith(CHANGE_ID, { comment: null }));
  });

  it("says why the header is empty for a closed change", async () => {
    mockUseApi.mockReturnValue(api({ getChange: vi.fn().mockResolvedValue({ ...COMPLETED, id: CHANGE_ID }) }));
    const { container } = renderDetail();
    expect(await screen.findByText("Este cambio está completado. Los cambios completados están cerrados de forma definitiva y no se pueden reabrir.")).toBeInTheDocument();
    expectNoEnglish(container);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText(/^This change is completed\./)).toBeInTheDocument();
  });

  it("says which permission hides a control, in words, and re-words on a switch", async () => {
    mockUseApi.mockReturnValue(
      api({
        me: vi.fn().mockResolvedValue({ ...ME, permissions: ["change.read"] }),
        getChange: vi.fn().mockResolvedValue({ ...SUBMITTED, id: CHANGE_ID }),
      }),
    );
    const { container } = renderDetail();
    const note = await screen.findByText(/están disponibles desde este estado, pero ocultas/);
    expect(note.textContent).toBe(
      "Las acciones Aprobar, Rechazar, Cancelar cambio están disponibles desde este estado, pero ocultas: tu cuenta no tiene permiso para aprobar cambios o solicitar cambios.",
    );
    expectNoEnglish(container);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText(/does not hold change\.approve or change\.request/)).toBeInTheDocument();
  });

  it("words the failed export from the held error", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api({ getChangeEvidence: vi.fn().mockRejectedValue(new ApiError(503, "")) }));
    renderDetail();
    await user.click(await screen.findByRole("button", { name: "Exportar evidencias" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/no está disponible/i);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert").textContent).toMatch(/unavailable/i);
  });

  it("words the linked actions and the link form: the type options, the notes and a refused link", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(
      api({
        listChangeLinks: vi.fn().mockResolvedValue([
          { id: "l1", action_type: "patch_run", action_id: "run-1", linked_at: "2026-02-01T00:00:00.000Z", linked_by: "u1" },
          { id: "l2", action_type: "provision_run", action_id: "run-2", linked_at: "2026-02-02T00:00:00.000Z", linked_by: "u1" },
        ]),
      }),
    );
    const { container } = renderDetail();
    const table = (await screen.findByText("run-1")).closest("table") as HTMLElement;
    const select = screen.getByLabelText("Tipo de acción");
    expect(within(table).getByText("Ejecución de parches")).toBeInTheDocument();
    expect(within(table).getByText("Ejecución de aprovisionamiento")).toBeInTheDocument();
    for (const name of ["Ejecución de parches", "Despliegue", "Ejecución de aprovisionamiento", "Aprobación de parche"]) {
      expect(within(select).getByRole("option", { name })).toBeInTheDocument();
    }
    expect(screen.getByText(/^Las ejecuciones de parches figuran en la pestaña Parches de cada equipo/)).toBeInTheDocument();
    expectNoEnglish(container);

    await user.type(screen.getByLabelText("ID de la acción *"), "no-es-un-uuid");
    await user.click(screen.getByRole("button", { name: "Vincular" }));
    expect((await screen.findByRole("alert")).textContent).toContain(
      "El ID de la acción debe ser un UUID como 06a85c19-55e2-7f3c-8000-831c7bdba357: «no-es-un-uuid» no lo es",
    );
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert").textContent).toContain("Action ID must be a UUID like");
    act(() => setActiveLanguage("es"));

    await user.selectOptions(select, "patch_approval");
    expect(screen.getByText(/^Las aprobaciones de parches figuran en la página Parches/)).toBeInTheDocument();
  });

  it("names the unchosen deployment, and the picker hint says where deployments come from", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api({ listDeployments: vi.fn().mockResolvedValue([]), listPackages: vi.fn().mockResolvedValue([]) }));
    const { container } = renderDetail();
    await user.selectOptions(await screen.findByLabelText("Tipo de acción"), "deployment");
    await user.click(screen.getByRole("combobox", { name: "Despliegue" }));
    expect(await screen.findByText(/Flota → Despliegue de software/)).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Buscar despliegues…")).toBeInTheDocument();
    expectNoEnglish(container);
    await user.click(screen.getByRole("button", { name: "Vincular" }));
    expect((await screen.findByRole("alert")).textContent).toContain("El campo Despliegue es obligatorio");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert").textContent).toContain("Deployment is required");
  });

  it("re-words picker rows that are on screen when the language switches", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(
      api({
        listDeployments: vi.fn().mockResolvedValue([
          { id: "dep1", package_id: "pkg1", action: "install", target_type: "device", target_id: "d1", window_cron: null, requested_by: "u1", state: "complete", created_at: "2026-02-01T00:00:00.000Z" },
        ]),
        listPackages: vi.fn().mockResolvedValue([
          { id: "pkg1", name: "Firmware del conmutador", version: "2.1", os_family: "linux", installer_type: "bin", artifact_ref: null, artifact_sha256: "abc", detection_rule: {}, uninstall_rule: {}, install_args: null, created_at: "2026-01-01T00:00:00.000Z" },
        ]),
      }),
    );
    renderDetail();
    await user.selectOptions(await screen.findByLabelText("Tipo de acción"), "deployment");
    await user.click(screen.getByRole("combobox", { name: "Despliegue" }));
    const option = await screen.findByRole("option", { name: /Firmware del conmutador 2\.1/ });
    expect(option.textContent).toContain("Firmware del conmutador 2.1 · instalar");
    expect(option.textContent).toContain("completado · ");
    act(() => setActiveLanguage("en"));
    const reworded = screen.getByRole("option", { name: /Firmware del conmutador 2\.1/ });
    expect(reworded.textContent).toContain("Firmware del conmutador 2.1 · install");
    expect(reworded.textContent).toContain("complete · ");
  });

  it("words the provisioning run picker", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api({ listProvisionRuns: vi.fn().mockResolvedValue([]), listProvisionProfiles: vi.fn().mockResolvedValue([]) }));
    renderDetail();
    await user.selectOptions(await screen.findByLabelText("Tipo de acción"), "provision_run");
    expect(screen.getByPlaceholderText("Buscar ejecuciones de aprovisionamiento…")).toBeInTheDocument();
  });
});

describe("ChangeReadiness in Spanish", () => {
  function role(codes: string[], assigned: number, id: string) {
    return { id, name: `Role ${id}`, description: "", is_global: false, assigned_users: assigned, codes };
  }
  const ROLE_ME = { ...ME, permissions: ["change.read", "roles.read", "devices.read"] };

  it("words a missing approver and a missing group, and follows a switch", async () => {
    mockUseApi.mockReturnValue(
      api({
        me: vi.fn().mockResolvedValue(ROLE_ME),
        listRoles: vi.fn().mockResolvedValue({ roles: [role(["change.approve"], 1, "r1")] }),
        listDeviceGroups: vi.fn().mockResolvedValue({ items: [], total: 0, ungrouped_device_count: 0, evaluation: null }),
      }),
    );
    const { container } = mount(<ChangeReadiness />);
    expect(await screen.findByText("Antes de poder aprobar cambios y exigir un cambio aprobado")).toBeInTheDocument();
    expect(await screen.findByText(/^Solo una asignación de rol permite aprobar un cambio/)).toBeInTheDocument();
    expect(screen.getByText(/Esto cuenta asignaciones de rol, no personas/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Dar a alguien permiso para aprobar cambios" })).toBeInTheDocument();
    expect(screen.getByText("Existe un grupo de dispositivos para exigir cambios en una parte del parque")).toBeInTheDocument();
    expectNoEnglish(container);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText(/^Only one role assignment allows approving a change/)).toBeInTheDocument();
  });

  it("words the case of nobody able to approve", async () => {
    mockUseApi.mockReturnValue(
      api({ me: vi.fn().mockResolvedValue(ROLE_ME), listRoles: vi.fn().mockResolvedValue({ roles: [] }) }),
    );
    mount(<ChangeReadiness />);
    expect(await screen.findByText(/^Nadie puede aprobar un cambio, así que un cambio enviado nunca podrá aprobarse\./)).toBeInTheDocument();
  });
});

describe("the change gate prerequisite in Spanish", () => {
  function Probe({ kind }: { kind: "patches" | "deployments" }) {
    const item = useChangeGatePrerequisite(kind, { label: "Sonda", ctaLabel: "Abrir cambios" });
    // A second item that is missing keeps the panel on screen: a panel whose only item is done is hidden.
    return <Prerequisites title="Prueba" items={[item, { label: "Otra", state: "missing", detail: "Falta" }]} />;
  }
  const GATE: ChangeGate = {
    id: "g1",
    device_group_id: "grp",
    device_group_name: "Servidores",
    applies_to_patches: true,
    applies_to_deployments: true,
    created_at: "2026-09-23T00:00:00Z",
  };

  it("words a gate with nothing approved, and re-words it on a switch", async () => {
    mockUseApi.mockReturnValue(api({ listChangeGates: vi.fn().mockResolvedValue([GATE]), listChanges: vi.fn().mockResolvedValue([DRAFT]) }));
    const { container } = mount(<Probe kind="patches" />);
    expect(
      await screen.findByText(
        "Las actualizaciones para los dispositivos de Servidores necesitan un cambio aprobado y no hay ninguno, así que se rechazan hasta que lo haya.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Abrir cambios" })).toHaveAttribute("href", "/changes");
    expectNoEnglish(container, ["Sonda", "Prueba"]);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText(/^Updates to devices in Servidores need an approved change, and none is approved/)).toBeInTheDocument();
  });

  it("agrees the count of ready changes", async () => {
    mockUseApi.mockReturnValue(
      api({
        listChangeGates: vi.fn().mockResolvedValue([{ ...GATE, device_group_id: null, device_group_name: null }]),
        listChanges: vi.fn().mockResolvedValue([
          { ...DRAFT, state: "approved" },
          { ...DRAFT, id: "c9", state: "implementing" },
        ]),
      }),
    );
    mount(<Probe kind="deployments" />);
    expect(
      await screen.findByText("Los despliegues para todos los dispositivos necesitan un cambio aprobado; 2 están listos para vincular."),
    ).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Deployments to every device need an approved change; 2 are ready to link.")).toBeInTheDocument();
  });

  it("words the gated location in both languages", () => {
    setActiveLanguage("es");
    expect(gatedWhere([{ ...GATE, device_group_name: null }])).toBe("los dispositivos de un grupo de dispositivos");
    setActiveLanguage("en");
    expect(gatedWhere([{ ...GATE, device_group_name: null }])).toBe("devices in a device group");
  });
});

describe("label tables built outside React", () => {
  it("words the state, risk, type, decision and link labels, and a state the console has not met", () => {
    setActiveLanguage("es");
    expect(["draft", "submitted", "approved", "implementing", "completed", "rejected", "cancelled"].map(changeStateLabel)).toEqual([
      "Borrador",
      "Enviado",
      "Aprobado",
      "En implementación",
      "Completado",
      "Rechazado",
      "Cancelado",
    ]);
    expect(["low", "medium", "high"].map(riskLabel)).toEqual(["Bajo", "Medio", "Alto"]);
    expect(["standard", "normal", "emergency"].map(changeTypeLabel)).toEqual(["Estándar", "Normal", "Emergencia"]);
    expect(["approved", "rejected"].map(decisionLabel)).toEqual(["Aprobada", "Rechazada"]);
    expect(linkActionLabel("patch_approval")).toBe("Aprobación de parche");
    expect(changeStateLabel("paused")).toBe("En pausa");
    setActiveLanguage("en");
    expect(changeStateLabel("submitted")).toBe("Submitted");
    expect(decisionLabel("approved")).toBe("Approved");
  });

  it("words every action's button and confirmation, re-read on a switch", () => {
    const specs = [
      ...availableChangeActions("draft", "standard"),
      ...availableChangeActions("submitted", "standard"),
      ...availableChangeActions("approved", "standard"),
      ...availableChangeActions("implementing", "emergency"),
      ...availableChangeActions("rejected", "standard"),
    ];
    const first = specs[0];
    expect(first?.label).toBe("Submit for approval");
    setActiveLanguage("es");
    expect(first?.label).toBe("Enviar a aprobación");
    const labels = specs.map((spec) => spec.label);
    expect(labels).toEqual(
      expect.arrayContaining(["Aprobar", "Rechazar", "Aprobar retroactivamente", "Rechazar retroactivamente", "Iniciar implementación", "Marcar como completado", "Cancelar cambio", "Corregir (volver a borrador)"]),
    );
    for (const spec of specs) {
      if (!spec.confirm) continue;
      for (const text of [spec.confirm.title, spec.confirm.body, spec.confirm.confirmLabel]) {
        expect(text).not.toMatch(/\b(change|approve|reject|emergency)\b/i);
      }
    }
    const cancel = specs.find((spec) => spec.action === "cancel");
    expect(cancel?.confirm?.destructive).toBe(true);
    setActiveLanguage("en");
    expect(first?.label).toBe("Submit for approval");
  });

  it("words the reason for no transition and for a hidden one", () => {
    setActiveLanguage("es");
    expect(noActionsReason("cancelled")).toMatch(/^Este cambio se canceló\./);
    expect(noActionsReason("paused")).toBe("No hay ninguna transición disponible desde el estado «En pausa».");
    const hidden = availableChangeActions("implementing", "emergency").filter((spec) => spec.action === "complete");
    expect(hiddenActionsReason(hidden)).toBe(
      "La acción Marcar como completado está disponible desde este estado, pero oculta: tu cuenta no tiene permiso para solicitar cambios.",
    );
    expect(hiddenActionsReason([])).toBeNull();
    setActiveLanguage("en");
    expect(noActionsReason("cancelled")).toMatch(/^This change was cancelled\./);
  });

  it("words the picker hints and the option sublabels", () => {
    const deployment = {
      id: "dep1",
      package_id: "pkg1",
      action: "install",
      target_type: "device",
      target_id: "dev1",
      window_cron: null,
      requested_by: "u1",
      state: "complete",
      created_at: "2026-02-01T00:00:00.000Z",
    };
    const pkg = {
      id: "pkg1",
      name: "CoreSwitch firmware",
      version: "2.1",
      os_family: "linux",
      installer_type: "bin",
      artifact_ref: null,
      artifact_sha256: "abc",
      detection_rule: {},
      uninstall_rule: {},
      install_args: null,
      created_at: "2026-01-01T00:00:00.000Z",
    };
    setActiveLanguage("es");
    const [option] = deploymentPickerOptions([deployment], [pkg], "UTC");
    expect(option?.label).toBe("CoreSwitch firmware 2.1 · instalar");
    expect(option?.sublabel).toMatch(/^completado · /);
    const [run] = provisionRunPickerOptions(
      [{ id: "run1", profile_id: "prof1", device_id: "WKS-042", target_ref: null, state: "complete", answer_ref: null, started_at: "2026-02-01T00:00:00.000Z", finished_at: null, error: null }],
      [],
      "UTC",
    );
    expect(run?.label).toBe("prof1");
    expect(run?.sublabel).toMatch(/^completada · /);
    expect(deploymentPickerEmptyHint()).toMatch(/Flota → Despliegue de software/);
    expect(provisionRunPickerEmptyHint()).toMatch(/Flota → Aprovisionamiento/);
    setActiveLanguage("en");
    expect(deploymentPickerEmptyHint()).toBe(translate("change.picker.deploymentEmptyHint"));
    expect(deploymentPickerOptions([deployment], [pkg], "UTC")[0]?.sublabel).toMatch(/^complete · /);
  });
});
