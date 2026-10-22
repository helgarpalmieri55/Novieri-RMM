/**
 * AI automation in Spanish: the actions queue, one action's confirmation screen
 * (the facts from the catalogue, the assistant's argument, the decision), the
 * autonomy dial with its confirmation, its prerequisites and the waiting
 * actions, and the label tables built outside React. Catalogue names,
 * descriptions, hostnames, parameters, script names and the assistant's own
 * rationale are recorded data and read as recorded.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import { ApiError } from "../../lib/api";
import type { ApiClient, Me } from "../../lib/api";
import { AiActionPage, AiActionsPage } from "./ActionsPage";
import { ActionPreview } from "./ActionPreview";
import { AutomationPrerequisites } from "./AutomationPrerequisites";
import { AutonomyDialPage } from "./AutonomyDialPage";
import { ConfirmDialog, explainFailure } from "./ConfirmDialog";
import {
  AI_CEILING_LEVELS,
  AI_PERSONAS,
  AI_REJECTION_REASONS,
  AI_RISK_CLASSES,
  autonomyLabel,
  ceilingLabel,
  personaLabel,
  rejectionReasonLabel,
  riskClassLabel,
} from "./types";
import type { AiActionOut, AiActionPreviewOut } from "./types";

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
  email: "a@b.c",
  display_name: "Alex Morgan",
  permissions: ["ai.act.propose", "ai.act.confirm", "ai.admin", "ai.use", "health.read"],
  timezone: "UTC",
};

const ACTION_ID = "11111111-1111-1111-1111-111111111111";

const WAITING: AiActionOut = {
  id: ACTION_ID,
  session_id: "44444444-4444-4444-4444-444444444444",
  persona: "technician",
  approved_action_key: "inventario-completo",
  job_type: "inventory.collect",
  target_device_id: "33333333-3333-3333-3333-333333333333",
  risk_class: "medium",
  destructive: false,
  effective_autonomy: "require_approval",
  tainted: false,
  state: "previewed",
  expires_at: new Date(Date.now() + 600_000).toISOString(),
  confirmed_by: null,
  rejected_reason: null,
  job_id: null,
  created_at: "2026-08-16T09:00:00Z",
};
const RAN: AiActionOut = { ...WAITING, id: "r1", state: "confirmed", persona: "admin", risk_class: "low", approved_action_key: "limpiar-disco" };
const DECLINED: AiActionOut = { ...WAITING, id: "r2", state: "rejected", destructive: true, risk_class: "high", approved_action_key: "reiniciar-servicio" };
const EXPIRED: AiActionOut = { ...WAITING, id: "r3", state: "expired", tainted: true, persona: "end_user", approved_action_key: "revisar-registro" };

function preview(overrides: Partial<AiActionPreviewOut> = {}): AiActionPreviewOut {
  return {
    ai_action_id: ACTION_ID,
    approved_action_id: "22222222-2222-2222-2222-222222222222",
    approved_action_key: "inventario-completo",
    name: "Recopilar inventario",
    description: "Actualizar el inventario de hardware y software.",
    job_type: "inventory.collect",
    rendered_params: {},
    redacted_param_paths: [],
    script_name: null,
    script_version: null,
    target_device_id: "33333333-3333-3333-3333-333333333333",
    target_hostname: "portatil-cfo-7",
    risk_class: "low",
    destructive: false,
    effective_autonomy: "require_approval",
    tainted: false,
    fuse_verdict: null,
    expires_at: new Date(Date.now() + 600_000).toISOString(),
    preview_digest: "a".repeat(64),
    state: "previewed",
    rationale: "el inventario tiene 40 días de antigüedad",
    ...overrides,
  };
}

function api(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    me: vi.fn().mockResolvedValue(ME),
    listAiActions: vi.fn().mockResolvedValue([WAITING, RAN, DECLINED, EXPIRED]),
    getAiAction: vi.fn().mockResolvedValue(preview()),
    confirmAiAction: vi.fn(),
    rejectAiAction: vi.fn(),
    listAiCeilings: vi.fn().mockResolvedValue([]),
    setAiCeiling: vi.fn().mockResolvedValue({}),
    getAiStatus: vi.fn().mockResolvedValue({ enabled: true, reason: "" }),
    listApprovedActions: vi.fn().mockResolvedValue([{ id: "a1" }]),
    ...overrides,
  } as unknown as ApiClient;
}

beforeEach(() => {
  mockUseApi.mockReturnValue(api());
});

describe("AiActionsPage in Spanish", () => {
  it("words the queue, the pills and the persona, and follows a live switch", async () => {
    const { container } = mount(<AiActionsPage />);
    expect(await screen.findByRole("link", { name: "inventario-completo" })).toHaveAttribute(
      "href",
      `/assistant/actions/${ACTION_ID}`,
    );
    expect(screen.getByRole("heading", { name: "Acciones del asistente" })).toBeInTheDocument();
    for (const header of ["Acción", "Qué se ejecuta", "Solicitada por", "Riesgo", "Estado"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(screen.getByText("Pendiente de aprobación")).toBeInTheDocument();
    expect(screen.getByText("Ejecutado")).toBeInTheDocument();
    expect(screen.getByText("Rechazado")).toBeInTheDocument();
    expect(screen.getByText("Caducado")).toBeInTheDocument();
    expect(screen.getByText("Destructiva")).toBeInTheDocument();
    expect(screen.getByText("Motivada por contenido no fiable")).toBeInTheDocument();
    // A risk class is a masculine word under "Riesgo".
    expect(screen.getAllByText("Medio").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Alto").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/^Propuesta el /).length).toBe(4);
    for (const persona of ["Técnico", "Administrador", "Usuario final"]) {
      expect(screen.getAllByText(persona).length).toBeGreaterThan(0);
    }
    expectNoEnglish(container);

    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("heading", { name: "Assistant actions" })).toBeInTheDocument();
    expect(screen.getByText("Awaiting approval")).toBeInTheDocument();
    expect(screen.getByText("Ran")).toBeInTheDocument();
    expect(screen.getByText("Destructive")).toBeInTheDocument();
    expect(screen.getAllByText("Technician").length).toBeGreaterThan(0);
  });

  it("words the empty queue", async () => {
    mockUseApi.mockReturnValue(api({ listAiActions: vi.fn().mockResolvedValue([]) }));
    const { container } = mount(<AiActionsPage />);
    expect(await screen.findByText("No hay nada a la espera de una decisión.")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("shows a state the console has not met in the shared status words", async () => {
    mockUseApi.mockReturnValue(api({ listAiActions: vi.fn().mockResolvedValue([{ ...WAITING, state: "paused" }]) }));
    mount(<AiActionsPage />);
    expect(await screen.findByText("En pausa")).toBeInTheDocument();
  });
});

describe("AiActionPage in Spanish", () => {
  function renderPage(route = `/assistant/actions/${ACTION_ID}`) {
    return mount(
      <Routes>
        <Route path="/assistant/actions/:actionId" element={<AiActionPage />} />
      </Routes>,
      route,
    );
  }

  it("words the facts, the two regions and the decision, and follows a switch", async () => {
    mockUseApi.mockReturnValue(
      api({
        getAiAction: vi.fn().mockResolvedValue(
          preview({
            rendered_params: { anillo: "piloto", maximo: 3 },
            redacted_param_paths: ["clave"],
            script_name: "limpiar.ps1",
            script_version: 4,
            fuse_verdict: "ok",
            tainted: true,
            risk_class: "high",
            destructive: true,
          }),
        ),
      }),
    );
    const { container } = renderPage();
    expect(await screen.findByRole("heading", { name: "Recopilar inventario" })).toBeInTheDocument();
    expect(screen.getByText("Preparada para portatil-cfo-7. No se ejecuta nada hasta que la confirmes.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Rechazar" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Confirmar y ejecutar" })).toBeEnabled();
    expect(screen.getByText(/^Al confirmar, la tarea se envía al dispositivo\./)).toBeInTheDocument();

    const what = screen.getByTestId("ai-action-what-will-happen");
    expect(what).toHaveAttribute("aria-label", "Qué ocurrirá");
    for (const label of ["Acción", "Qué se ejecutará", "Destino", "Riesgo", "Destructiva", "Caduca en"]) {
      if (label === "Qué se ejecutará") continue;
      expect(within(what).getAllByText(label).length).toBeGreaterThan(0);
    }
    expect(within(what).getByText("Qué se ejecuta")).toBeInTheDocument();
    expect(within(what).getByText("Clave del catálogo")).toBeInTheDocument();
    expect(within(what).getByText("Autonomía")).toBeInTheDocument();
    // The sentence form, with the stored token beside it.
    expect(within(what).getByText("A la espera de la confirmación de una persona")).toBeInTheDocument();
    expect(within(what).getByText("require_approval")).toBeInTheDocument();
    expect(within(what).getByText("Parámetros ocultos")).toBeInTheDocument();
    expect(within(what).getByText("Límite de frecuencia")).toBeInTheDocument();
    expect(within(what).getByText("Parámetros")).toBeInTheDocument();
    expect(within(what).getByText("2 valores resueltos, completos")).toBeInTheDocument();
    expect(within(what).getByText("Sí")).toBeInTheDocument();
    expect(within(what).getByText("Alto")).toBeInTheDocument();
    expect(screen.getByTestId("ai-action-taint-badge").textContent).toMatch(/^Esta sugerencia procede de contenido/);

    const why = screen.getByTestId("ai-action-why-suggested");
    expect(why).toHaveAttribute("aria-label", "Por qué se sugirió");
    expect(within(why).getByText("Por qué se propuso")).toBeInTheDocument();
    expect(within(why).getByText("el inventario tiene 40 días de antigüedad")).toBeInTheDocument();
    expectNoEnglish(container);

    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("button", { name: "Confirm and run" })).toBeInTheDocument();
    expect(screen.getByTestId("ai-action-what-will-happen")).toHaveAttribute("aria-label", "What will happen");
    expect(screen.getByText("2 resolved values, in full")).toBeInTheDocument();
  });

  it("words a missing rationale, no parameters, one parameter and a negative destructive flag", async () => {
    mockUseApi.mockReturnValue(api({ getAiAction: vi.fn().mockResolvedValue(preview({ rationale: "" })) }));
    const { container } = renderPage();
    expect(await screen.findByText("(no se indicó ningún motivo)")).toBeInTheDocument();
    expect(screen.getByText("Esta acción no tiene parámetros.")).toBeInTheDocument();
    expect(screen.getByText("No")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("agrees a single resolved value", async () => {
    mockUseApi.mockReturnValue(api({ getAiAction: vi.fn().mockResolvedValue(preview({ rendered_params: { anillo: "piloto" } })) }));
    renderPage();
    expect(await screen.findByText("1 valor resuelto, completo")).toBeInTheDocument();
  });

  it("asks why after Decline, in Spanish, and sends the chosen reason", async () => {
    const user = userEvent.setup();
    const rejectAiAction = vi.fn().mockResolvedValue({});
    mockUseApi.mockReturnValue(api({ rejectAiAction }));
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Rechazar" }));
    expect(screen.getByText("Rechazar esta acción")).toBeInTheDocument();
    expect(screen.getByText("Indica el motivo. Se guarda junto con la decisión.")).toBeInTheDocument();
    const group = await screen.findByRole("group", { name: "Motivo del rechazo" });
    for (const name of ["No hace falta", "Dispositivo equivocado", "Acción equivocada", "Necesita una solicitud de cambio", "No es seguro ahora", "Otro motivo"]) {
      expect(within(group).getByRole("button", { name })).toBeInTheDocument();
    }
    await user.click(within(group).getByRole("button", { name: "Dispositivo equivocado" }));
    await user.click(screen.getByRole("button", { name: "Enviar el rechazo" }));
    await waitFor(() => expect(rejectAiAction).toHaveBeenCalledWith(ACTION_ID, { reason: "wrong_device" }));
    expect(screen.getByRole("button", { name: "Dejarla en espera" })).toBeInTheDocument();
  });

  it("says a settled action has nothing left to decide, with the state agreeing with its noun", async () => {
    mockUseApi.mockReturnValue(api({ getAiAction: vi.fn().mockResolvedValue(preview({ state: "rejected" })) }));
    const { container } = renderPage();
    expect(await screen.findByText("Esta acción está rechazada; ya no queda nada por decidir.")).toBeInTheDocument();
    expectNoEnglish(container);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("This action is rejected; there is nothing left to decide.")).toBeInTheDocument();
  });

  it("words a dispatched action with the same word as everywhere else, agreeing with its noun", async () => {
    mockUseApi.mockReturnValue(api({ getAiAction: vi.fn().mockResolvedValue(preview({ state: "dispatched" })) }));
    renderPage();
    expect(await screen.findByText("Esta acción está despachada; ya no queda nada por decidir.")).toBeInTheDocument();
  });

  it("announces a failed preview load, re-worded on a switch from the held error", async () => {
    mockUseApi.mockReturnValue(api({ getAiAction: vi.fn().mockRejectedValue(new ApiError(503, "")) }));
    renderPage();
    expect((await screen.findByRole("alert")).textContent).toMatch(/no está disponible/i);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert").textContent).toMatch(/unavailable/i);
  });

  it("words the head while the preview loads", async () => {
    mockUseApi.mockReturnValue(api({ getAiAction: vi.fn().mockReturnValue(new Promise(() => {})) }));
    renderPage();
    expect(await screen.findByRole("heading", { name: "Confirmar una acción" })).toBeInTheDocument();
    expect(screen.getByText("Cargando…")).toBeInTheDocument();
  });

  it("announces a stale confirm, says nothing was run, and offers the reload; both re-word on a switch", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api({ confirmAiAction: vi.fn().mockRejectedValue(new ApiError(409, "")) }));
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Confirmar y ejecutar" }));
    const alert = await screen.findByTestId("ai-action-error");
    expect(alert.textContent).toContain("No se ejecutó nada. Recarga la vista previa y vuelve a revisarla.");
    expect(screen.getByRole("button", { name: "Mostrar la vista previa actual" })).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByTestId("ai-action-error").textContent).toContain("Nothing was run. Reload the preview and look again.");
  });
});

describe("ConfirmDialog failures in Spanish", () => {
  it("words a gone preview and a refusal, and nothing ran", () => {
    setActiveLanguage("es");
    expect(explainFailure(new ApiError(410, ""))).toBe("Esta vista previa caducó antes de confirmarse. Vuelve a pedírselo al asistente.");
    expect(explainFailure(new ApiError(403, ""))).toMatch(/No se ejecutó nada\.$/);
    expect(explainFailure(null)).toBe("");
    setActiveLanguage("en");
    expect(explainFailure(new ApiError(410, ""))).toMatch(/^This preview expired/);
  });

  it("announces a failed decline", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api({ rejectAiAction: vi.fn().mockRejectedValue(new ApiError(503, "")) }));
    mount(<ConfirmDialog preview={preview()} canConfirm onReload={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Rechazar" }));
    await user.click(await screen.findByRole("button", { name: "Enviar el rechazo" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/no está disponible/i);
  });
});

describe("ActionPreview in Spanish", () => {
  it("words the time left as expired or unknown", () => {
    const { container } = mount(<ActionPreview preview={preview({ expires_at: "2020-01-01T00:00:00Z" })} now={Date.parse("2026-01-01T00:00:00Z")} />);
    expect(within(container).getByText("caducada")).toBeInTheDocument();
    mount(<ActionPreview preview={preview({ expires_at: "no-es-una-fecha" })} />);
    expect(screen.getByText("desconocido")).toBeInTheDocument();
  });
});

describe("AutonomyDialPage in Spanish", () => {
  it("words the grid, the legend, the prerequisites and the waiting actions, and follows a switch", async () => {
    mockUseApi.mockReturnValue(
      api({
        listAiCeilings: vi.fn().mockResolvedValue([
          { persona: "technician", risk_class: "high", level: "suggest", updated_by: null, updated_at: null },
        ]),
        getAiAction: vi.fn().mockResolvedValue(preview()),
        listApprovedActions: vi.fn().mockResolvedValue([]),
        getAiStatus: vi.fn().mockResolvedValue({ enabled: false, reason: "No hay ningún proveedor de modelos configurado." }),
      }),
    );
    const { container } = mount(<AutonomyDialPage />);
    expect(await screen.findByRole("heading", { name: "Automatización con IA" })).toBeInTheDocument();
    expect(await screen.findByText("Límites de autonomía")).toBeInTheDocument();
    expect(screen.getByText("Qué puede preparar cada tipo de usuario para cada clase de riesgo")).toBeInTheDocument();
    expect(screen.getByText("Una celda vacía significa Desactivado. No se guarda nada hasta que pulses Guardar cambios.")).toBeInTheDocument();
    for (const header of ["Tipo de usuario", "Riesgo bajo", "Riesgo medio", "Riesgo alto"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(screen.getByText("El asistente nunca puede actuar en nombre de un usuario final, sea cual sea la configuración de aquí.")).toBeInTheDocument();
    expect(screen.getAllByText("Fijo").length).toBe(3);
    const cell = within(screen.getByTestId("dial-technician-high"));
    expect(cell.getByRole("button", { name: "Solo sugerir" })).toHaveAttribute("aria-pressed", "true");
    expect(cell.getByRole("button", { name: "Requiere aprobación" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Técnico, riesgo alto" })).toBeInTheDocument();
    // The prerequisites, with the server's reason as the server wrote it.
    expect(await screen.findByText("Antes de que el asistente pueda actuar")).toBeInTheDocument();
    expect(screen.getByText(/^El asistente está desactivado en este servidor, así que no propone nada/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Abrir la configuración del asistente" })).toBeInTheDocument();
    // The waiting actions panel names the action by the catalogue's sentence.
    expect(await screen.findByText("Acciones pendientes de aprobación")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Recopilar inventario" })).toBeInTheDocument();
    expect(screen.getByText(/· Técnico · propuesta el /)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Revisar" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Todas las acciones" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Guardar cambios" })).toBeDisabled();
    // "user": the stored token `end_user` the grid prints under the persona's name on purpose (the identifier an
    // operator searches the docs for); the rest of the page is checked in full.
    expectNoEnglish(container, ["user"]);

    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Autonomy ceilings")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Technician, high risk" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "low risk" })).toBeInTheDocument();
  });

  it("words the legend: each position, its meaning and its stored token", async () => {
    const { container } = mount(<AutonomyDialPage />);
    expect(await screen.findByText("Límites de autonomía")).toBeInTheDocument();
    expect(screen.getByText(/^El asistente no ofrecerá en absoluto acciones de este riesgo/)).toBeInTheDocument();
    expect(screen.getByText(/^El asistente puede describir una acción en su respuesta\./)).toBeInTheDocument();
    expect(screen.getByText(/^El asistente puede preparar la acción y dejarla en espera\./)).toBeInTheDocument();
    expect(screen.getByText("La clase de riesgo la fija cada acción aprobada del catálogo de tu organización, no esta página.")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("states an unsaved edit and the confirmation, and re-words both on a switch", async () => {
    const user = userEvent.setup();
    mount(<AutonomyDialPage />);
    await user.click(within(await screen.findByTestId("dial-technician-high")).getByRole("button", { name: "Requiere aprobación" }));
    expect(screen.getByText("Sin guardar. Guardado: Desactivado")).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "" }).textContent).toContain("1 cambio sin guardar: Técnico / riesgo alto: Desactivado → Requiere aprobación");
    await user.click(within(screen.getByTestId("dial-admin-low")).getByRole("button", { name: "Solo sugerir" }));
    expect(screen.getByText(/^2 cambios sin guardar: /)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Guardar cambios" }));
    const dialog = await screen.findByRole("dialog", { name: "Subir el límite de autonomía del asistente" });
    expect(dialog.textContent).toContain("Técnico, con riesgo alto. El asistente puede preparar la acción y dejarla en espera.");
    expect(dialog.textContent).toContain("Administrador, con riesgo bajo. El asistente puede describir una acción en su respuesta.");
    expect(dialog.textContent).toContain("las acciones destructivas siguen fuera de alcance en cualquier posición");
    expect(within(dialog).getByRole("button", { name: "Subir el límite" })).toBeInTheDocument();
    expectNoEnglish(dialog);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("dialog", { name: "Raise the assistant's autonomy ceiling" })).toBeInTheDocument();
    expect(screen.getByRole("dialog").textContent).toContain("Technician at high risk The assistant may prepare the action and hold it.");
  });

  it("applies the change, says it saved, and names the cell the server refused", async () => {
    const user = userEvent.setup();
    const setAiCeiling = vi.fn().mockResolvedValue({});
    mockUseApi.mockReturnValue(api({ setAiCeiling }));
    mount(<AutonomyDialPage />);
    await user.click(within(await screen.findByTestId("dial-technician-low")).getByRole("button", { name: "Solo sugerir" }));
    await user.click(screen.getByRole("button", { name: "Guardar cambios" }));
    await user.click(await screen.findByRole("button", { name: "Subir el límite" }));
    await waitFor(() => expect(setAiCeiling).toHaveBeenCalledWith({ persona: "technician", risk_class: "low", level: "suggest" }));
    expect(await screen.findByText("Guardado. La cuadrícula muestra lo que devolvió el servidor.")).toBeInTheDocument();
  });

  it("names the refused cell, and the sentence is worded from the held error", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api({ setAiCeiling: vi.fn().mockRejectedValue(new ApiError(503, "")) }));
    mount(<AutonomyDialPage />);
    await user.click(within(await screen.findByTestId("dial-admin-medium")).getByRole("button", { name: "Requiere aprobación" }));
    await user.click(screen.getByRole("button", { name: "Guardar cambios" }));
    await user.click(await screen.findByRole("button", { name: "Subir el límite" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/^Administrador, con riesgo medio: el límite no se modificó\. .*no está disponible.* La cuadrícula sigue mostrando lo que no se ha aplicado\.$/i);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert").textContent).toMatch(/^Administrator \/ medium risk was not changed\. /);
  });

  it("discards an edit", async () => {
    const user = userEvent.setup();
    mount(<AutonomyDialPage />);
    await user.click(within(await screen.findByTestId("dial-technician-low")).getByRole("button", { name: "Solo sugerir" }));
    await user.click(screen.getByRole("button", { name: "Descartar cambios" }));
    expect(screen.queryByText(/cambio sin guardar/)).not.toBeInTheDocument();
  });
});

describe("AutomationPrerequisites in Spanish", () => {
  it("sends an organization with no approved action to them", async () => {
    mockUseApi.mockReturnValue(api({ listApprovedActions: vi.fn().mockResolvedValue([]) }));
    const { container } = mount(<AutomationPrerequisites />);
    expect(await screen.findByText(/^El asistente solo propone acciones aprobadas y no existe ninguna/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Abrir las acciones aprobadas" })).toHaveAttribute("href", "/health#approved-actions");
    expectNoEnglish(container);
  });

  it("agrees the count of approved actions with the reason it is shown", async () => {
    mockUseApi.mockReturnValue(
      api({
        listApprovedActions: vi.fn().mockResolvedValue([{ id: "a1" }, { id: "a2" }]),
        getAiStatus: vi.fn().mockResolvedValue({ enabled: false, reason: "El asistente no está disponible." }),
      }),
    );
    mount(<AutomationPrerequisites />);
    expect(await screen.findByText(/^El asistente está desactivado en este servidor/)).toBeInTheDocument();
    expect(screen.getByText(/El asistente no está disponible\.$/)).toBeInTheDocument();
  });
});

describe("label tables built outside React", () => {
  it("words every dial position, reason, persona and risk class, and follows a switch", () => {
    setActiveLanguage("es");
    expect(AI_CEILING_LEVELS.map(ceilingLabel)).toEqual(["Desactivado", "Solo sugerir", "Requiere aprobación"]);
    expect(AI_CEILING_LEVELS.map(autonomyLabel)).toEqual([
      "No permitido",
      "Solo puede mencionarse en texto",
      "A la espera de la confirmación de una persona",
    ]);
    expect(AI_REJECTION_REASONS.map(rejectionReasonLabel)).toEqual([
      "No hace falta",
      "Dispositivo equivocado",
      "Acción equivocada",
      "Necesita una solicitud de cambio",
      "No es seguro ahora",
      "Otro motivo",
    ]);
    expect(AI_PERSONAS.map(personaLabel)).toEqual(["Usuario final", "Técnico", "Administrador"]);
    expect(AI_RISK_CLASSES.map(riskClassLabel)).toEqual(["Bajo", "Medio", "Alto"]);
    // A value the tables do not name reads in the shared status words.
    expect(ceilingLabel("paused")).toBe("En pausa");
    setActiveLanguage("en");
    expect(AI_CEILING_LEVELS.map(ceilingLabel)).toEqual(["Off", "Suggest only", "Needs approval"]);
    expect(personaLabel("end_user")).toBe("End user");
    expect(riskClassLabel("high")).toBe("High");
  });
});
