/**
 * Business continuity and DR tests in Spanish: the plans list (tiles, filters,
 * table), a plan's detail with its confirmations and the New test dialog, the
 * plan editor with its scope, procedure references and refusals, the test
 * recorder (outcome, evidence snapshot, steps and findings), both reports, the
 * readiness panel and the sentences the module builds outside React. Plan,
 * device, group and article names, ids and the server's own text read as
 * recorded; the words around them are Spanish.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import { ApiError } from "../../lib/api";
import type { ApiClient, Device, Me } from "../../lib/api";
import { BcpReadiness } from "./BcpReadiness";
import { BcpReport } from "./BcpReport";
import {
  outcomeLabel,
  planStateLabel,
  remediationLabel,
  severityLabel,
  stepResultLabel,
  testTypeLabel,
} from "./bcpState";
import { PlanDetail } from "./PlanDetail";
import { PlanEditor } from "./PlanEditor";
import { PlansPage } from "./PlansPage";
import {
  procedurePickerEmptyHint,
  procedurePickerOptions,
  scopeDevicePickerEmptyHint,
  scopeDevicePickerOptions,
  scopeGroupPickerEmptyHint,
  scopeGroupPickerOptions,
} from "./pickerOptions";
import { reviewStanding, reviewStandingLabel } from "./reviewState";
import { TestRecorder } from "./TestRecorder";
import type { FindingOut, PlanOut, PlanStatusOut, TestOut } from "./types";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function renderPage(ui: ReactElement, path = "/bcp") {
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
  timezone: "UTC",
  permissions: ["bcp.read", "bcp.manage", "bcp.approve", "backup.read", "roles.read"],
};

// ------------------------------------------------------------------ fixtures

const DEVICE_ID = "9f1f4a1e-0000-4000-8000-000000000001";
const GROUP_ID = "06a85c19-55e2-7f3c-8000-831c7bdba357";
const ARTICLE_ID = "3c2b1a09-0000-4000-8000-000000000002";

function plan(over: Partial<PlanOut> = {}): PlanOut {
  return {
    id: "plan1",
    name: "Conmutación del centro de datos",
    description: "Pasar los servicios centrales al sitio de recuperación.",
    scope: [
      { kind: "device", id: DEVICE_ID },
      { kind: "group", id: GROUP_ID },
    ],
    rto_minutes: 60,
    rpo_minutes: 15,
    procedure_refs: [ARTICLE_ID],
    dependencies: [],
    review_interval_days: 365,
    last_reviewed_at: null,
    approved_by: null,
    state: "active",
    created_by: "u2",
    created_at: "2026-01-01T00:00:00.000Z",
    ...over,
  };
}

const PLANS: PlanOut[] = [
  plan(),
  plan({ id: "plan2", name: "Oficina de Oporto", rto_minutes: null, rpo_minutes: null, last_reviewed_at: "2026-09-01T00:00:00.000Z", scope: [], procedure_refs: [], review_interval_days: 1 }),
  plan({ id: "plan3", name: "Plan antiguo", state: "archived", last_reviewed_at: "2025-01-01T00:00:00.000Z" }),
];

function status(over: Partial<PlanStatusOut> & Pick<PlanStatusOut, "id">): PlanStatusOut {
  return {
    name: "x",
    state: "active",
    review_overdue: false,
    last_reviewed_at: null,
    last_test_outcome: null,
    open_findings: 0,
    restore_proven_coverage: { total: 0, restore_proven: 0 },
    ...over,
  };
}
const STATUS: PlanStatusOut[] = [
  status({ id: "plan1", last_test_outcome: "partial", open_findings: 2 }),
  status({ id: "plan2", last_reviewed_at: "2026-09-01T00:00:00.000Z", review_overdue: false, last_test_outcome: null, open_findings: 0 }),
  status({ id: "plan3", state: "archived", last_reviewed_at: "2025-01-01T00:00:00.000Z", review_overdue: true, last_test_outcome: "pass", open_findings: 1 }),
];

const DEVICE = {
  id: DEVICE_ID,
  hostname: "WKS-042",
  fqdn: null,
  os_family: "windows",
  os_version: "11",
  architecture: "x86_64",
  device_group_id: null,
  status: "active",
  agent_status: "online",
  last_seen_at: null,
  cert_not_after: null,
  cert_state: null,
} as unknown as Device;
const DEVICE_OFFLINE = { ...DEVICE, id: "dev-2", hostname: "SRV-02", os_family: "linux", agent_status: "offline" } as Device;
const DEVICE_NO_AGENT = { ...DEVICE, id: "dev-3", hostname: "SW-CORE-1", os_family: null, agent_status: null } as unknown as Device;

const GROUP = {
  id: GROUP_ID,
  name: "Centro de datos principal",
  parent_group_id: null,
  description: "",
  dynamic: false,
  device_count: 12,
  precedence: 0,
  archived_at: null,
};
const GROUP_ONE = { ...GROUP, id: "grp-2", name: "Un solo equipo", device_count: 1 };

const ARTICLE = {
  id: ARTICLE_ID,
  title: "Manual de conmutación",
  category: "manual",
  archived: false,
  updated_at: "2026-02-01T00:00:00.000Z",
  link_count: 0,
};
const ARTICLE_ARCHIVED = { ...ARTICLE, id: "art-2", title: "Manual antiguo", archived: true };

const TEST_PENDING: TestOut = {
  id: "test1",
  plan_id: "plan1",
  type: "restore_drill",
  scheduled_for: "2026-01-15T19:00:00.000Z",
  executed_at: null,
  executed_by: null,
  outcome: "pending",
  rto_met: null,
  rpo_met: null,
  evidence: {},
  notes: "Simulacro anual",
  created_at: "2026-01-01T00:00:00.000Z",
};
const EVIDENCE = {
  captured_at: "2026-01-16T10:00:00.000Z",
  devices: [
    { device_id: "d1", hostname: "FS-01", covered: true, last_backup_taken_at: "2026-01-15T02:00:00.000Z", verified_at: "2026-01-15T03:00:00.000Z", restore_tested_at: "2026-01-15T04:00:00.000Z", restore_proven: true },
    { device_id: "d2", hostname: "WEB-02", covered: false, last_backup_taken_at: null, verified_at: null, restore_tested_at: null, restore_proven: false },
  ],
  summary: { total_n: 2, covered_n: 1, restore_proven_n: 1 },
};
const TEST_DONE: TestOut = {
  ...TEST_PENDING,
  id: "test2",
  type: "failover",
  executed_at: "2026-01-16T10:00:00.000Z",
  outcome: "partial",
  rto_met: true,
  rpo_met: false,
  evidence: EVIDENCE,
};

const FINDINGS: FindingOut[] = [
  { id: "f1", test_id: "test1", description: "La restauración del servidor de archivos superó el RTO", severity: "high", remediation_state: "open", change_ref: null, created_at: "2026-01-15T19:00:00.000Z", resolved_at: null },
  { id: "f2", test_id: "test1", description: "Falta el procedimiento de red", severity: "critical", remediation_state: "in_progress", change_ref: null, created_at: "2026-01-15T19:00:00.000Z", resolved_at: null },
  { id: "f3", test_id: "test1", description: "Contraseña caducada", severity: "low", remediation_state: "resolved", change_ref: null, created_at: "2026-01-15T19:00:00.000Z", resolved_at: "2026-01-16T00:00:00.000Z" },
];

const REPORT = {
  test: TEST_DONE,
  plan: PLANS[0],
  steps: [
    { id: "s1", test_id: "test1", ordinal: 1, description: "Apagar el servidor principal", result: "pass", evidence_ref: "EVID-1" },
    { id: "s2", test_id: "test1", ordinal: 2, description: "Levantar la réplica", result: "fail", evidence_ref: null },
    { id: "s3", test_id: "test1", ordinal: 3, description: "Comprobar el servicio", result: "pending", evidence_ref: null },
  ],
  findings: FINDINGS,
  evidence: EVIDENCE,
};

function baseApi(over: Record<string, unknown> = {}) {
  const api = {
    me: vi.fn().mockResolvedValue(ME),
    listPlans: vi.fn().mockResolvedValue(PLANS),
    planStatus: vi.fn().mockResolvedValue(STATUS),
    getPlan: vi.fn().mockResolvedValue(PLANS[0]),
    listPlanTests: vi.fn().mockResolvedValue([TEST_PENDING, TEST_DONE]),
    listDevices: vi.fn().mockResolvedValue({ items: [DEVICE, DEVICE_OFFLINE, DEVICE_NO_AGENT], total: 3 }),
    listDeviceGroups: vi.fn().mockResolvedValue({ items: [GROUP, GROUP_ONE], total: 2, ungrouped_device_count: 0, evaluation: null }),
    listArticles: vi.fn().mockResolvedValue({ items: [ARTICLE, ARTICLE_ARCHIVED], total: 2 }),
    createPlan: vi.fn().mockResolvedValue(PLANS[0]),
    updatePlan: vi.fn().mockResolvedValue(PLANS[0]),
    reviewPlan: vi.fn().mockResolvedValue(PLANS[0]),
    createTest: vi.fn().mockResolvedValue(TEST_PENDING),
    getTest: vi.fn().mockResolvedValue(TEST_DONE),
    recordTest: vi.fn().mockResolvedValue(TEST_DONE),
    listFindings: vi.fn().mockResolvedValue(FINDINGS),
    createFinding: vi.fn().mockResolvedValue(FINDINGS[0]),
    updateFinding: vi.fn().mockResolvedValue(FINDINGS[0]),
    upsertStep: vi.fn().mockResolvedValue(REPORT.steps[0]),
    testReport: vi.fn().mockResolvedValue(REPORT),
    periodReport: vi.fn().mockResolvedValue({
      outcome_counts: { pass: 1, partial: 3, fail: 0 },
      plans_tested: ["plan1", "plan2"],
      open_findings_by_severity: { high: 1, critical: 2 },
      review_overdue_plans: ["plan3"],
    }),
    listBackupPolicies: vi.fn().mockResolvedValue([]),
    listRoles: vi.fn().mockResolvedValue({ roles: [] }),
    ...over,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

beforeEach(() => {
  baseApi();
});

// -------------------------------------------------------------------- plans

describe("the plans page, in Spanish", () => {
  it("words the head, the tiles, the table, the pills and the readiness panel", async () => {
    const { container } = renderPage(<PlansPage />);
    expect(await screen.findByRole("heading", { name: "Pruebas de continuidad y recuperación (BCP / DR)" })).toBeInTheDocument();
    await screen.findByText("Conmutación del centro de datos");
    expect(screen.getByText("Planes de continuidad y recuperación, sus objetivos y sus pruebas.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Nuevo plan" })).toBeInTheDocument();
    // The tiles and their second lines.
    expect(screen.getByText("Planes activos")).toBeInTheDocument();
    expect(screen.getByText("de 3 registrados")).toBeInTheDocument();
    expect(screen.getByText("Revisiones vencidas")).toBeInTheDocument();
    expect(screen.getByText("1 nunca revisado, 1 fuera del intervalo")).toBeInTheDocument();
    expect(screen.getByText("Hallazgos abiertos")).toBeInTheDocument();
    expect(screen.getByText("en 2 planes")).toBeInTheDocument();
    // The table.
    for (const header of ["Nombre", "Estado", "Revisión", "Última prueba", "Hallazgos", "Creado"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    const row = (name: string) => screen.getByText(name).closest("tr") as HTMLElement;
    expect(within(row("Conmutación del centro de datos")).getByText("RTO 60 min · RPO 15 min")).toBeInTheDocument();
    expect(within(row("Conmutación del centro de datos")).getByText("Activo")).toBeInTheDocument();
    expect(within(row("Conmutación del centro de datos")).getByText("Nunca revisado")).toBeInTheDocument();
    expect(within(row("Conmutación del centro de datos")).getByText("Parcial")).toBeInTheDocument();
    expect(within(row("Conmutación del centro de datos")).getByRole("link", { name: "Pruebas" })).toBeInTheDocument();
    expect(within(row("Oficina de Oporto")).getByText("RTO n/d · RPO n/d")).toBeInTheDocument();
    expect(within(row("Oficina de Oporto")).getByText("Al día")).toBeInTheDocument();
    expect(within(row("Oficina de Oporto")).getByText("Sin probar")).toBeInTheDocument();
    expect(within(row("Plan antiguo")).getByText("Archivado")).toBeInTheDocument();
    expect(within(row("Plan antiguo")).getByText("Revisión vencida")).toBeInTheDocument();
    expect(within(row("Plan antiguo")).getByText("Superada")).toBeInTheDocument();
    // The readiness panel.
    expect(await screen.findByText("Antes de que las pruebas de continuidad demuestren algo")).toBeInTheDocument();
    expectNoEnglish(container, ["RTO", "RPO"]);
  });

  it("says why there are no plans, and what an unmeasured zero means", async () => {
    baseApi({ listPlans: vi.fn().mockResolvedValue([]), planStatus: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    const { container } = renderPage(<PlansPage />);
    expect(await screen.findByText("Todavía no hay planes BCP/DR.")).toBeInTheDocument();
    expect(screen.getByText("Ningún plan está activo")).toBeInTheDocument();
    expect(screen.getByText("No se comprobaron los intervalos de revisión: no se cargó el estado de los planes")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words the filter note, and agrees the counts of one", async () => {
    baseApi({ listPlans: vi.fn().mockResolvedValue([PLANS[0]]), planStatus: vi.fn().mockResolvedValue([STATUS[0]!]) });
    const first = renderPage(<PlansPage />, "/bcp?review=overdue");
    await screen.findByText("Conmutación del centro de datos");
    expect(screen.getByText(/Mostrando 1 de 1: planes cuya revisión está vencida o nunca se ha hecho\./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Mostrar todos los planes" })).toBeInTheDocument();
    expect(screen.getByText("en 1 plan")).toBeInTheDocument();
    expect(screen.getByText("1 nunca revisado, 0 fuera del intervalo")).toBeInTheDocument();
    expect(screen.getByText("de 1 registrado")).toBeInTheDocument();
    expectNoEnglish(first.container, ["RTO", "RPO"]);
    first.unmount();
    renderPage(<PlansPage />, "/bcp?state=archived");
    expect(await screen.findByText("Ningún plan coincide con esta vista: planes archivados.")).toBeInTheDocument();
  });

  it("says every review is current when none is overdue", async () => {
    baseApi({ listPlans: vi.fn().mockResolvedValue([PLANS[1]]), planStatus: vi.fn().mockResolvedValue([STATUS[1]!]) });
    const { container } = renderPage(<PlansPage />);
    await screen.findByText("Oficina de Oporto");
    expect(screen.getByText("La revisión de todos los planes está al día")).toBeInTheDocument();
    expect(screen.getByText("Ningún hallazgo está abierto")).toBeInTheDocument();
    expectNoEnglish(container, ["RTO", "RPO"]);
  });

  it("re-words the page when the language is switched", async () => {
    renderPage(<PlansPage />);
    await screen.findByText("Conmutación del centro de datos");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("heading", { name: "BCP / DR tests" })).toBeInTheDocument();
    const row = screen.getByText("Plan antiguo").closest("tr") as HTMLElement;
    expect(within(row).getByText("Archived")).toBeInTheDocument();
    expect(within(row).getByText("Review overdue")).toBeInTheDocument();
    expect(within(row).getByText("Pass")).toBeInTheDocument();
    expect(screen.getByText("1 never reviewed, 1 past the interval")).toBeInTheDocument();
    expect(screen.getByText("Before continuity tests prove anything")).toBeInTheDocument();
  });
});

describe("the readiness panel, in Spanish", () => {
  it("links each gap and counts what is there", async () => {
    const first = renderPage(<BcpReadiness />);
    expect(await screen.findByText("Hay copias de seguridad configuradas, así que una prueba de restauración tiene evidencia")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Configurar copias de seguridad" })).toHaveAttribute("href", "/backup/policies");
    expect(screen.getByRole("link", { name: "Dar a alguien la revisión de planes" })).toBeInTheDocument();
    expect(screen.getByText(/Nadie puede firmar la revisión de un plan/)).toBeInTheDocument();
    expectNoEnglish(first.container);
    first.unmount();
    const enabled = { id: "p", device_group_id: null, repository_id: "r", sources: {}, window_cron: null, retention_gfs: {}, enabled: true };
    baseApi({
      listBackupPolicies: vi.fn().mockResolvedValue([enabled]),
      listRoles: vi.fn().mockResolvedValue({ roles: [{ id: "r1", name: "R", description: "", is_global: false, assigned_users: 0, codes: ["bcp.read"] }] }),
    });
    const second = renderPage(<BcpReadiness />);
    expect(await screen.findByText(/Hay 1 política de copia de seguridad habilitada\./)).toBeInTheDocument();
    expectNoEnglish(second.container);
  });

  it("agrees the plural forms", async () => {
    const enabled = { id: "p", device_group_id: null, repository_id: "r", sources: {}, window_cron: null, retention_gfs: {}, enabled: true };
    baseApi({
      listBackupPolicies: vi.fn().mockResolvedValue([enabled, { ...enabled, id: "p2" }]),
      listRoles: vi.fn().mockResolvedValue({ roles: [] }),
    });
    renderPage(<BcpReadiness />);
    expect(await screen.findByText(/Hay 2 políticas de copia de seguridad habilitadas\./)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- plan detail

describe("a plan's detail, in Spanish", () => {
  function renderDetail() {
    return renderPage(
      <Routes>
        <Route path="/bcp/plans/:planId" element={<PlanDetail />} />
      </Routes>,
      "/bcp/plans/plan1",
    );
  }

  it("words the head, the facts, the plan panel and the tests table", async () => {
    const { container } = renderDetail();
    expect(await screen.findByRole("heading", { name: "Conmutación del centro de datos" })).toBeInTheDocument();
    expect(await screen.findByText("WKS-042, Centro de datos principal")).toBeInTheDocument();
    expect(screen.getByText("Objetivos, ámbito, procedimientos, pruebas y hallazgos de un plan.")).toBeInTheDocument();
    for (const name of ["Registrar revisión", "Editar plan", "Archivar"]) {
      expect(screen.getByRole("button", { name })).toBeInTheDocument();
    }
    for (const label of ["Estado", "RTO", "RPO", "Última revisión", "Intervalo de revisión", "Revisión"]) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
    expect(screen.getAllByText("60 min").length).toBeGreaterThan(0);
    expect(screen.getByText("nunca")).toBeInTheDocument();
    expect(screen.getByText("365 días")).toBeInTheDocument();
    expect(screen.getByText("Nunca revisado")).toBeInTheDocument();
    expect(screen.getByText("Manual de conmutación")).toBeInTheDocument();
    expect(screen.getByText("Pruebas de recuperación")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Nueva prueba" })).toBeInTheDocument();
    for (const header of ["Prueba", "Programada", "Ejecutada", "Resultado"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(screen.getByText("Simulacro de restauración")).toBeInTheDocument();
    expect(screen.getByText("Conmutación por error")).toBeInTheDocument();
    expect(screen.getByText("Pendiente")).toBeInTheDocument();
    expect(screen.getAllByText("Parcial").length).toBeGreaterThan(0);
    expect(screen.getByText("Aún sin ejecutar")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Registrar / ver" })).toHaveLength(2);
    expectNoEnglish(container, ["WKS-042"]);
  });

  it("says nothing is in scope, no procedure, no tests and a day of review", async () => {
    baseApi({
      getPlan: vi.fn().mockResolvedValue(PLANS[1]),
      listPlanTests: vi.fn().mockResolvedValue([]),
    });
    const { container } = renderDetail();
    expect(await screen.findByText("Todavía no hay nada en el ámbito")).toBeInTheDocument();
    expect(screen.getByText("Todavía no se hace referencia a ningún procedimiento")).toBeInTheDocument();
    expect(screen.getByText("Todavía no hay pruebas de recuperación.")).toBeInTheDocument();
    expect(screen.getByText("1 día")).toBeInTheDocument();
    expect(screen.getAllByText("n/d").length).toBeGreaterThan(0);
    expectNoEnglish(container);
  });

  it("asks before recording a review, and before archiving", async () => {
    const user = userEvent.setup();
    const api = baseApi();
    renderDetail();
    await screen.findByRole("heading", { name: "Conmutación del centro de datos" });
    await user.click(screen.getByRole("button", { name: "Registrar revisión" }));
    const review = await screen.findByRole("dialog", { name: "Registrar una revisión de este plan" });
    expect(review).toHaveTextContent(
      "Anota la fecha de hoy y tu cuenta en el plan como su última revisión. Se escribe en el registro de auditoría y no se puede retirar.",
    );
    expect(review).toHaveTextContent("Conmutación del centro de datos");
    expectNoEnglish(review, ["Conmutación del centro de datos"]);
    await user.click(within(review).getByRole("button", { name: "Registrar revisión" }));
    await waitFor(() => expect(api.reviewPlan).toHaveBeenCalledWith("plan1"));
    await user.click(screen.getByRole("button", { name: "Archivar" }));
    const archive = await screen.findByRole("dialog", { name: "Archivar este plan de continuidad" });
    expect(archive).toHaveTextContent(
      "El plan deja de formar parte de la cobertura de recuperación activa y de la situación de continuidad.",
    );
    expect(within(archive).getByRole("button", { name: "Archivar plan" })).toBeInTheDocument();
    expectNoEnglish(archive, ["Conmutación del centro de datos"]);
  });

  it("says Desarchivar for an archived plan, and words a refused review", async () => {
    const user = userEvent.setup();
    baseApi({
      getPlan: vi.fn().mockResolvedValue({ ...PLANS[0], state: "archived" }),
      reviewPlan: vi.fn().mockRejectedValue(new ApiError(403, "")),
    });
    renderDetail();
    expect(await screen.findByRole("button", { name: "Desarchivar" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Registrar revisión" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Registrar revisión" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expectNoEnglish(screen.getByRole("alert"));
  });

  it("words the New test dialog", async () => {
    const user = userEvent.setup();
    renderDetail();
    await screen.findByRole("heading", { name: "Conmutación del centro de datos" });
    await user.click(screen.getByRole("button", { name: "Nueva prueba" }));
    const dialog = await screen.findByRole("dialog", { name: "Nueva prueba de recuperación" });
    const type = within(dialog).getByRole("combobox", { name: "Tipo" });
    for (const option of ["Simulacro de restauración", "Conmutación por error", "Ejercicio de mesa", "Otra"]) {
      expect(within(type).getByRole("option", { name: option })).toBeInTheDocument();
    }
    expect(within(dialog).getByText("Programada para")).toBeInTheDocument();
    expect(within(dialog).getByText(/Las horas están en UTC\./)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Crear" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Cancelar" })).toBeInTheDocument();
    expectNoEnglish(dialog);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("dialog", { name: "New DR test" })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- plan editor

describe("the plan editor, in Spanish", () => {
  function renderEditor(planArg: PlanOut | "new" = "new") {
    return renderPage(<PlanEditor plan={planArg} onClose={() => {}} />);
  }

  it("words the new-plan form and its notes", async () => {
    const user = userEvent.setup();
    const { container } = renderEditor();
    const dialog = await screen.findByRole("dialog", { name: "Nuevo plan" });
    expect(dialog).toHaveTextContent("Los campos marcados con * son obligatorios.");
    for (const label of ["Nombre *", "Descripción", "Ámbito", "RTO (minutos)", "RPO (minutos)", "Referencias a procedimientos", "Intervalo de revisión (días) *"]) {
      expect(within(dialog).getByText(label)).toBeInTheDocument();
    }
    expect(within(dialog).getByText("RTO: cuánto puede tardar la recuperación antes de que vuelva el servicio.")).toBeInTheDocument();
    expect(within(dialog).getByText("RPO: cuántos datos recientes se pueden perder, medidos hacia atrás desde la interrupción.")).toBeInTheDocument();
    expect(within(dialog).getByText("Todavía no se hace referencia a ningún procedimiento.")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Añadir una entrada al ámbito" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Guardar" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Añadir una entrada al ámbito" }));
    expect(within(dialog).getByText("Tipo del ámbito 1")).toBeInTheDocument();
    const kind = within(dialog).getByRole("combobox", { name: "Tipo del ámbito 1" });
    expect(within(kind).getByRole("option", { name: "dispositivo" })).toBeInTheDocument();
    expect(within(kind).getByRole("option", { name: "grupo" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Quitar" })).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("names the missing name and the bad interval, and re-words them on a switch", async () => {
    const user = userEvent.setup();
    renderEditor();
    const dialog = await screen.findByRole("dialog", { name: "Nuevo plan" });
    const interval = within(dialog).getByLabelText("Intervalo de revisión (días) *");
    await user.clear(interval);
    await user.type(interval, "0");
    await user.type(within(dialog).getByLabelText("RTO (minutos)"), "-90");
    await user.type(within(dialog).getByLabelText("RPO (minutos)"), "1.5");
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("El campo Nombre es obligatorio");
    expect(alert).toHaveTextContent("El intervalo de revisión (días) debe ser al menos 1");
    expect(alert).toHaveTextContent("RTO (minutos) no puede ser negativo");
    expect(alert).toHaveTextContent("RPO (minutos) debe ser un número entero de minutos");
    expectNoEnglish(dialog, ["RTO", "RPO"]);
    act(() => setActiveLanguage("en"));
    const again = within(screen.getByRole("dialog", { name: "New plan" })).getByRole("alert");
    expect(again).toHaveTextContent("Name is required");
    expect(again).toHaveTextContent("Review interval (days) must be at least 1");
    expect(again).toHaveTextContent("RTO (minutes) cannot be negative");
    expect(again).toHaveTextContent("RPO (minutes) must be a whole number of minutes");
  });

  it("words a scope row that is not an id", async () => {
    const user = userEvent.setup();
    renderEditor(plan({ scope: [{ kind: "group", id: "core" }, { kind: "device", id: "" }] }));
    const dialog = await screen.findByRole("dialog", { name: "Editar plan" });
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("El ámbito 1 debe ser el id de un dispositivo o de un grupo: «core» no lo es");
    expect(alert).toHaveTextContent("El campo Ámbito 2 (dispositivo) es obligatorio");
    expectNoEnglish(dialog, ["Conmutación del centro de datos"]);
    act(() => setActiveLanguage("en"));
    const again = within(screen.getByRole("dialog", { name: "Edit plan" })).getByRole("alert");
    expect(again).toHaveTextContent("Scope 1 must be a device or group id");
    expect(again).toHaveTextContent("Scope 2 (device) is required");
  });

  it("warns when the recovery time is lower than the data-loss window", async () => {
    const user = userEvent.setup();
    renderEditor();
    const dialog = await screen.findByRole("dialog", { name: "Nuevo plan" });
    await user.type(within(dialog).getByLabelText("RTO (minutos)"), "5");
    await user.type(within(dialog).getByLabelText("RPO (minutos)"), "30");
    expect(within(dialog).getByRole("status")).toHaveTextContent(
      "El RTO es menor que el RPO: este plan dice que el servicio vuelve antes que los datos que perdió.",
    );
    expectNoEnglish(dialog, ["RTO", "RPO"]);
  });

  it("words the pickers: sublabels, empty hints and the links beside them", async () => {
    const user = userEvent.setup();
    renderEditor();
    const dialog = await screen.findByRole("dialog", { name: "Nuevo plan" });
    await user.click(within(dialog).getByRole("button", { name: "Añadir una entrada al ámbito" }));
    await user.click(within(dialog).getByRole("combobox", { name: "Dispositivo del ámbito 1" }));
    expect(await screen.findByRole("option", { name: /WKS-042.*windows · en línea/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /SRV-02.*linux · sin conexión/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /SW-CORE-1.*sin agente/ })).toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: /WKS-042/ }));
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Tipo del ámbito 1" }), "group");
    await user.click(within(dialog).getByRole("combobox", { name: "Grupo del ámbito 1" }));
    expect(await screen.findByRole("option", { name: /Centro de datos principal.*12 dispositivos/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Un solo equipo.*1 dispositivo$/ })).toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: /Un solo equipo/ }));
    await user.click(within(dialog).getByRole("combobox", { name: "Añadir un procedimiento" }));
    expect(await screen.findByRole("option", { name: /Manual de conmutación.*manual/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Manual antiguo.*archivado/ })).toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: /Manual de conmutación/ }));
    expect(within(dialog).getByText("Manual de conmutación")).toBeInTheDocument();
    expectNoEnglish(dialog, ["WKS-042", "SRV-02", "SW-CORE-1"]);
  });

  it("explains each dead end when the lists are empty", async () => {
    const user = userEvent.setup();
    baseApi({
      listDevices: vi.fn().mockResolvedValue({ items: [], total: 0 }),
      listDeviceGroups: vi.fn().mockResolvedValue({ items: [], total: 0, ungrouped_device_count: 0, evaluation: null }),
      listArticles: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    });
    renderEditor();
    const dialog = await screen.findByRole("dialog", { name: "Nuevo plan" });
    await user.click(within(dialog).getByRole("button", { name: "Añadir una entrada al ámbito" }));
    expect(await within(dialog).findByRole("link", { name: "Añadir un equipo →" })).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: "Escribe el procedimiento de recuperación en la documentación de TI →" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("combobox", { name: "Dispositivo del ámbito 1" }));
    expect(await within(dialog).findByText(scopeDevicePickerEmptyHint())).toBeInTheDocument();
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Tipo del ámbito 1" }), "group");
    expect(await within(dialog).findByRole("link", { name: "Crear un grupo de dispositivos →" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("combobox", { name: "Grupo del ámbito 1" }));
    expect(await within(dialog).findByText(scopeGroupPickerEmptyHint())).toBeInTheDocument();
    expectNoEnglish(dialog);
  });

  it("opens an existing plan in Spanish", async () => {
    renderEditor(PLANS[0]);
    const dialog = await screen.findByRole("dialog", { name: "Editar plan" });
    expect(within(dialog).getByLabelText("Nombre *")).toHaveValue("Conmutación del centro de datos");
    expect(await within(dialog).findByText("Manual de conmutación")).toBeInTheDocument();
    expectNoEnglish(dialog, ["Conmutación del centro de datos", "WKS-042"]);
  });
});

// ---------------------------------------------------------------- test recorder

describe("the test recorder, in Spanish", () => {
  function renderRecorder() {
    return renderPage(
      <Routes>
        <Route path="/bcp/tests/:testId" element={<TestRecorder />} />
      </Routes>,
      "/bcp/tests/test1",
    );
  }

  it("words the head, the facts, the outcome form and the evidence snapshot", async () => {
    const { container } = renderRecorder();
    expect(await screen.findByRole("heading", { name: "Prueba de recuperación" })).toBeInTheDocument();
    expect(await screen.findByText("Instantánea de evidencia de copias de seguridad")).toBeInTheDocument();
    expect(screen.getByText("Registra el resultado de una prueba frente a los objetivos de recuperación de su plan.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Ver informe" })).toBeInTheDocument();
    for (const label of ["Tipo", "Resultado", "Programada", "Ejecutada"]) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
    expect(screen.getAllByText("Conmutación por error").length).toBeGreaterThan(0);
    expect(screen.getByText("Registrar resultado")).toBeInTheDocument();
    expect(screen.getByLabelText("RTO cumplido")).toBeInTheDocument();
    const rto = screen.getByLabelText("RTO cumplido");
    for (const option of ["Desconocido", "Sí", "No"]) {
      expect(within(rto).getByRole("option", { name: option })).toBeInTheDocument();
    }
    const outcome = screen.getAllByRole("combobox", { name: "Resultado" })[0]!;
    for (const option of ["Superada", "Parcial", "Fallida"]) {
      expect(within(outcome).getByRole("option", { name: option })).toBeInTheDocument();
    }
    expect(screen.getByRole("button", { name: "Registrar prueba" })).toBeInTheDocument();
    // The snapshot.
    expect(screen.getByText(/Congelada el/)).toBeInTheDocument();
    for (const header of ["Nombre de host", "Cubierto", "Última copia", "Verificado", "Restauración probada", "Restauración demostrada"]) {
      expect(screen.getAllByRole("columnheader", { name: header }).length).toBeGreaterThan(0);
    }
    const row = (host: string) => screen.getByText(host).closest("tr") as HTMLElement;
    expect(within(row("FS-01")).getByText("Cubierto")).toBeInTheDocument();
    expect(within(row("FS-01")).getByText("Demostrada")).toBeInTheDocument();
    expect(within(row("WEB-02")).getByText("No cubierto")).toBeInTheDocument();
    expect(within(row("WEB-02")).getByText("No demostrada")).toBeInTheDocument();
    expectNoEnglish(container, ["FS-01", "WEB-02", "RTO", "RPO"]);
  });

  it("words the steps form and the findings, and their refusals on a switch", async () => {
    const user = userEvent.setup();
    const { container } = renderRecorder();
    await screen.findByText("Registrar paso de la prueba");
    for (const label of ["Número de paso", "Descripción del paso", "Referencia de evidencia"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    await user.click(screen.getByRole("button", { name: "Guardar paso" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("El campo Descripción del paso es obligatorio");
    expectNoEnglish(container, ["FS-01", "WEB-02", "RTO", "RPO"]);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent("Step description is required");
    act(() => setActiveLanguage("es"));
    // Findings.
    await screen.findByText("La restauración del servidor de archivos superó el RTO");
    const findings = screen.getByText("Hallazgos").closest("section") ?? container;
    for (const header of ["Descripción", "Gravedad", "Corrección"]) {
      expect(within(findings as HTMLElement).getAllByRole("columnheader", { name: header }).length).toBeGreaterThan(0);
    }
    const select = screen.getByRole("combobox", { name: "Estado de corrección de La restauración del servidor de archivos superó el RTO" });
    for (const option of ["Abierto", "En curso", "Resuelto"]) {
      expect(within(select).getByRole("option", { name: option })).toBeInTheDocument();
    }
    expect(screen.getAllByText("Alto").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Crítico").length).toBeGreaterThan(0);
    await user.click(screen.getByRole("button", { name: "Añadir hallazgo" }));
    expect(await screen.findAllByRole("alert")).not.toHaveLength(0);
    expect(screen.getAllByRole("alert").some((el) => /Descripción del hallazgo es obligatorio/.test(el.textContent ?? ""))).toBe(true);
  });

  it("names a step number that is not a whole number", async () => {
    const user = userEvent.setup();
    renderRecorder();
    await screen.findByText("Registrar paso de la prueba");
    const number = screen.getByLabelText("Número de paso");
    await user.clear(number);
    await user.type(number, "1.5");
    await user.type(screen.getByLabelText("Descripción del paso"), "Probar el servicio");
    await user.click(screen.getByRole("button", { name: "Guardar paso" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("El número de paso debe ser un número entero");
  });

  it("asks before replacing the frozen evidence", async () => {
    const user = userEvent.setup();
    const api = baseApi();
    renderRecorder();
    await screen.findByText("Instantánea de evidencia de copias de seguridad");
    await user.click(screen.getByRole("button", { name: "Registrar prueba" }));
    const dialog = await screen.findByRole("dialog", { name: "Volver a registrar esta prueba de recuperación" });
    expect(dialog).toHaveTextContent(
      "La instantánea de evidencia de copias de seguridad congelada al registrar esta prueba por primera vez se sustituye por una tomada ahora. La original no se puede recuperar.",
    );
    expect(dialog).toHaveTextContent("Prueba de tipo Conmutación por error, registrada el");
    expect(within(dialog).getByRole("button", { name: "Sustituir la evidencia" })).toBeInTheDocument();
    expectNoEnglish(dialog);
    await user.click(within(dialog).getByRole("button", { name: "Sustituir la evidencia" }));
    await waitFor(() => expect(api.recordTest).toHaveBeenCalled());
  });

  it("words the re-record subject so nothing has to agree with a masculine type", async () => {
    const user = userEvent.setup();
    baseApi({ getTest: vi.fn().mockResolvedValue({ ...TEST_DONE, type: "restore_drill", executed_at: "2026-01-16T10:00:00.000Z" }) });
    renderRecorder();
    await screen.findByText("Instantánea de evidencia de copias de seguridad");
    await user.click(screen.getByRole("button", { name: "Registrar prueba" }));
    const dialog = await screen.findByRole("dialog", { name: "Volver a registrar esta prueba de recuperación" });
    expect(dialog).toHaveTextContent(/Prueba de tipo Simulacro de restauración, registrada el .*2026/);
    expect(dialog).not.toHaveTextContent("Simulacro de restauración registrada");
    expectNoEnglish(dialog);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("dialog", { name: "Re-record this DR test" })).toHaveTextContent(/Restore drill recorded 16 Jan 2026/);
  });

  it("echoes the moment typed, and words a failed recording", async () => {
    baseApi({ getTest: vi.fn().mockResolvedValue(TEST_PENDING), recordTest: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    const user = userEvent.setup();
    renderRecorder();
    await screen.findByText("Registrar resultado");
    fireEvent.change(screen.getByLabelText("Ejecutada el"), { target: { value: "2026-01-16T10:00" } });
    expect(await screen.findByText(/^Ejecutada el .*2026/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Registrar prueba" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expectNoEnglish(screen.getByRole("alert"));
  });

  it("re-words a mounted recorder when the language is switched", async () => {
    renderRecorder();
    await screen.findByText("Instantánea de evidencia de copias de seguridad");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("heading", { name: "DR test" })).toBeInTheDocument();
    expect(screen.getByText("Backup-evidence snapshot")).toBeInTheDocument();
    const findings = screen.getByText("La restauración del servidor de archivos superó el RTO").closest("tr") as HTMLElement;
    expect(within(findings).getByText("High")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------- reports

describe("the reports, in Spanish", () => {
  it("words one test's report", async () => {
    const { container } = renderPage(
      <Routes>
        <Route path="/bcp/tests/:testId/report" element={<BcpReport />} />
      </Routes>,
      "/bcp/tests/test1/report",
    );
    expect(await screen.findByRole("heading", { name: "Informe de la prueba de recuperación" })).toBeInTheDocument();
    expect(await screen.findByText("Apagar el servidor principal")).toBeInTheDocument();
    expect(screen.getByText("Los pasos, hallazgos y resultado de una prueba, como evidencia SOC 2.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Volver a la prueba" })).toBeInTheDocument();
    for (const label of ["Plan", "Tipo", "Resultado", "Ejecutada", "RTO cumplido", "RPO cumplido"]) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
    expect(screen.getByText("Conmutación por error")).toBeInTheDocument();
    expect(screen.getByText("sí")).toBeInTheDocument();
    expect(screen.getByText("no")).toBeInTheDocument();
    for (const header of ["Paso", "Descripción", "Resultado", "Referencia de evidencia", "Gravedad", "Corrección"]) {
      expect(screen.getAllByRole("columnheader", { name: header }).length).toBeGreaterThan(0);
    }
    const step = (text: string) => screen.getByText(text).closest("tr") as HTMLElement;
    expect(within(step("Apagar el servidor principal")).getByText("Superado")).toBeInTheDocument();
    expect(within(step("Levantar la réplica")).getByText("Fallido")).toBeInTheDocument();
    expect(within(step("Levantar la réplica")).getByText("Sin referencia de evidencia")).toBeInTheDocument();
    expect(within(step("Comprobar el servicio")).getByText("Pendiente")).toBeInTheDocument();
    expect(within(step("Contraseña caducada")).getByText("Resuelto")).toBeInTheDocument();
    expect(within(step("Falta el procedimiento de red")).getByText("En curso")).toBeInTheDocument();
    expectNoEnglish(container, ["EVID-1", "RTO", "RPO"]);
  });

  it("says there are no steps or findings, and words a failed load", async () => {
    baseApi({ testReport: vi.fn().mockResolvedValue({ ...REPORT, steps: [], findings: [], test: { ...TEST_DONE, rto_met: null, rpo_met: null }, plan: null }) });
    const first = renderPage(
      <Routes>
        <Route path="/bcp/tests/:testId/report" element={<BcpReport />} />
      </Routes>,
      "/bcp/tests/test1/report",
    );
    expect(await screen.findByText("No hay pasos registrados.")).toBeInTheDocument();
    expect(screen.getByText("No hay hallazgos registrados.")).toBeInTheDocument();
    expect(screen.getAllByText("n/d").length).toBeGreaterThan(0);
    expectNoEnglish(first.container, ["RTO", "RPO"]);
    first.unmount();
    baseApi({ testReport: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    renderPage(
      <Routes>
        <Route path="/bcp/tests/:testId/report" element={<BcpReport />} />
      </Routes>,
      "/bcp/tests/test1/report",
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo cargar el informe.");
  });

  it("words the period report: the empty state, the echo, the tiles and the units", async () => {
    const { container } = renderPage(<BcpReport />, "/bcp/report");
    expect(await screen.findByRole("heading", { name: "Informe de pruebas de recuperación por periodo" })).toBeInTheDocument();
    expect(screen.getByText("Resultados de las pruebas y hallazgos abiertos en un intervalo de fechas.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Todos los planes" })).toBeInTheDocument();
    expect(screen.getByText("No se ha elegido ningún periodo")).toBeInTheDocument();
    expect(screen.getByText("Indica los dos extremos del periodo arriba.")).toBeInTheDocument();
    expect(screen.getByText(/Las horas están en UTC\. Cada ejecución queda registrada en el registro de auditoría como una exportación\./)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Desde"), { target: { value: "2026-01-01T00:00" } });
    expect(await screen.findByText(/^Desde .*2026.*, sin fecha de fin\.$/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Hasta"), { target: { value: "2026-01-31T23:59" } });
    expect(await screen.findByText(/^Desde .*2026.* hasta .*2026.*\.$/)).toBeInTheDocument();
    await screen.findByText("Hallazgos abiertos por gravedad");
    expect(await screen.findByText("Superada")).toBeInTheDocument();
    expect(screen.getByText("Parcial")).toBeInTheDocument();
    expect(screen.getByText("Alto")).toBeInTheDocument();
    expect(screen.getByText("Crítico")).toBeInTheDocument();
    expect(screen.getByText("Probados en este periodo")).toBeInTheDocument();
    expect(screen.getByText("Revisión vencida")).toBeInTheDocument();
    expect(container.textContent).toMatch(/1\s*prueba(?!s)/);
    expect(container.textContent).toMatch(/3\s*pruebas/);
    expect(container.textContent).toMatch(/1\s*hallazgo(?!s)/);
    expect(container.textContent).toMatch(/2\s*hallazgos/);
    expectNoEnglish(container);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Open findings by severity")).toBeInTheDocument();
    expect(container.textContent).toMatch(/1\s*test(?!s)/);
  });

  it("says nothing was executed, and the start-less echo", async () => {
    baseApi({ periodReport: vi.fn().mockResolvedValue({ outcome_counts: {}, plans_tested: [], open_findings_by_severity: {}, review_overdue_plans: [] }) });
    const { container } = renderPage(<BcpReport />, "/bcp/report");
    fireEvent.change(await screen.findByLabelText("Hasta"), { target: { value: "2026-01-31T23:59" } });
    expect(await screen.findByText(/^Hasta .*2026.*, sin fecha de inicio\.$/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Desde"), { target: { value: "2026-01-01T00:00" } });
    expect(await screen.findByText("No se ejecutó ninguna prueba en este periodo")).toBeInTheDocument();
    expect(screen.getByText("Ningún hallazgo de estas pruebas sigue abierto.")).toBeInTheDocument();
    expectNoEnglish(container);
  });
});

// ------------------------------------------------ built outside React

describe("the labels the module builds outside React, in Spanish", () => {
  it("words the review standings and the enum labels, and follows a switch", () => {
    setActiveLanguage("es");
    expect(reviewStandingLabel(reviewStanding(null, undefined))).toBe("Nunca revisado");
    expect(reviewStandingLabel(reviewStanding("2026-01-01", true))).toBe("Revisión vencida");
    expect(reviewStandingLabel(reviewStanding("2026-01-01", false))).toBe("Al día");
    expect(reviewStandingLabel(reviewStanding("2026-01-01", undefined))).toBe("Revisión desconocida");
    expect(outcomeLabel("pass")).toBe("Superada");
    expect(outcomeLabel("pendiente-raro")).toBe("Pendiente raro");
    expect(remediationLabel("in_progress")).toBe("En curso");
    expect(severityLabel("medium")).toBe("Medio");
    expect(stepResultLabel("fail")).toBe("Fallido");
    expect(planStateLabel("archived")).toBe("Archivado");
    expect(testTypeLabel("tabletop")).toBe("Ejercicio de mesa");
    // A value nobody catalogued reads through the shared status words, not raw.
    expect(severityLabel("urgent")).toBe("Urgente");
    setActiveLanguage("en");
    expect(reviewStandingLabel(reviewStanding(null, undefined))).toBe("Never reviewed");
    expect(outcomeLabel("pass")).toBe("Pass");
    expect(remediationLabel("in_progress")).toBe("In progress");
    expect(testTypeLabel("restore_drill")).toBe("Restore drill");
  });

  it("words the picker options and the empty hints, and follows a switch", () => {
    setActiveLanguage("es");
    expect(scopeDevicePickerOptions([DEVICE, DEVICE_OFFLINE, DEVICE_NO_AGENT]).map((o) => o.sublabel)).toEqual([
      "windows · en línea",
      "linux · sin conexión",
      "sin agente",
    ]);
    expect(scopeGroupPickerOptions([GROUP, GROUP_ONE]).map((o) => o.sublabel)).toEqual(["12 dispositivos", "1 dispositivo"]);
    expect(procedurePickerOptions([ARTICLE_ARCHIVED], "UTC")[0]?.sublabel).toMatch(/^manual · archivado · /);
    expect(scopeDevicePickerEmptyHint()).toMatch(/Todavía no hay equipos/);
    expect(scopeGroupPickerEmptyHint()).toMatch(/grupos de dispositivos/);
    expect(procedurePickerEmptyHint()).toMatch(/documentación/);
    setActiveLanguage("en");
    expect(scopeDevicePickerOptions([DEVICE, DEVICE_OFFLINE, DEVICE_NO_AGENT]).map((o) => o.sublabel)).toEqual([
      "windows · online",
      "linux · offline",
      "no agent",
    ]);
    expect(procedurePickerOptions([ARTICLE_ARCHIVED], "UTC")[0]?.sublabel).toMatch(/^manual · archived · /);
    expect(scopeDevicePickerEmptyHint()).toBe(
      "No computers to scope this plan to yet; you may also not have permission to list them.",
    );
  });
});
