/**
 * Endpoint health and alerts in Spanish: the Health page (tiles, readiness,
 * alert list and its drawer), the report, the profiles and their checks, the
 * approved-action registry with its exemptions, the settings panel, a device's
 * own health panel, and the sentences the module builds outside React. Hostnames,
 * profile and action names, selectors and observed values read as recorded; the
 * words around them are Spanish.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import { ApiError } from "../../lib/api";
import type { ApiClient, Me } from "../../lib/api";
import { ADD_COMPUTER_CTA } from "../../lib/detectors";
import { AlertConsole } from "./AlertConsole";
import { AlertDetail, describeApprovalSubject } from "./AlertDetail";
import { AlertReadiness, ASSIGN_PROFILE_CTA } from "./AlertReadiness";
import {
  ApprovedActionList,
  decisionBody,
  decisionConfirmLabel,
  decisionSubject,
  decisionTitle,
} from "./ApprovedActionList";
import { DeviceHealthPanel } from "./DeviceHealthPanel";
import { NUMERIC_FIELDS } from "./fields";
import { HealthPage } from "./HealthPage";
import { HealthReportPanel } from "./HealthReportPanel";
import { HealthSettingsPanel } from "./HealthSettingsPanel";
import {
  ACTION_KIND_LABEL,
  ACTION_STATE_PRESENTATION,
  ALERT_STATE_PRESENTATION,
  AUTONOMY_PRESENTATION,
  CHECK_STATE_PRESENTATION,
  CLEARED_REASON_PRESENTATION,
  SEVERITY_PRESENTATION,
  checkLabel,
  describeAlertStatus,
  describeDeviceHealth,
  describeDuration,
  describeObservation,
  describeRepeatPause,
  describeThreshold,
  formatSeconds,
} from "./healthState";
import { ProfileEditor, CheckTypeMatrix } from "./ProfileEditor";
import { ProfileList, describeAssignments, describeDeleteImpact } from "./ProfileList";
import type {
  AlertActionOut,
  AlertDetailOut,
  AlertOut,
  ApprovedActionOut,
  CheckOut,
  CheckTypeOut,
  DeviceHealthOut,
} from "./types";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

// ------------------------------------------------------------------ fixtures

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "tech@example.com",
  display_name: "Tech",
  permissions: ["health.read", "health.alert.manage", "health.remediate", "health.admin", "health.action.admin", "devices.read", "notification.read"],
  timezone: "UTC",
};

function alert(overrides: Partial<AlertOut> = {}): AlertOut {
  return {
    id: "a1",
    device_id: "d1",
    device_hostname: "WS-01",
    device_group_id: null,
    check_id: "c1",
    profile_id: "p1",
    check_type: "cpu.busy_pct",
    selector: "",
    severity: "warning",
    state: "active",
    observed_value: "97.4",
    threshold_display: "gt 95",
    opened_at: "2026-03-01T09:00:00.000Z",
    opened_policy_version: 1,
    last_seen_at: "2026-03-01T09:05:00.000Z",
    occurrence_count: 2,
    flapping: false,
    flap_count: 0,
    escalated_at: null,
    acknowledged_at: null,
    acknowledged_by: null,
    muted_until: null,
    muted_by: null,
    cleared_at: null,
    cleared_reason: null,
    cleared_by: null,
    ticket_id: null,
    previous_alert_id: null,
    ...overrides,
  };
}

function action(overrides: Partial<AlertActionOut> = {}): AlertActionOut {
  return {
    id: "ar1",
    alert_id: "a1",
    kind: "remediate",
    state: "awaiting_approval",
    approved_action_id: "act1",
    approved_action_key: "restart-spooler",
    approved_action_name: "Reiniciar la cola",
    script_id: "s1",
    script_name: "Restart Spooler",
    script_version: 4,
    autonomy: "require_approval",
    risk_class: "low",
    destructive: false,
    job_id: null,
    ticket_id: null,
    attempts: 0,
    next_attempt_at: null,
    approved_by: null,
    approved_at: null,
    expires_at: "2026-03-02T09:00:00.000Z",
    last_error: null,
    created_at: "2026-03-01T09:00:00.000Z",
    updated_at: "2026-03-01T09:00:00.000Z",
    ...overrides,
  };
}

function detail(overrides: Partial<AlertDetailOut> = {}): AlertDetailOut {
  return {
    ...alert(),
    profile_name: "Base de oficina",
    ticket_number: "INC-9",
    previous_alert_ticket_number: null,
    previous_alert_opened_at: null,
    monitoring_deep_link: null,
    actions: [],
    ...overrides,
  };
}

const PROFILE = {
  id: "p1",
  name: "Base de oficina",
  description: "Para los puestos",
  tick_interval_s: 300,
  posture_interval_s: 3600,
  summary_interval_s: 1800,
  active: true,
  policy_version: 3,
  check_count: 2,
  assignment_count: 2,
  created_at: "2026-01-01T00:00:00.000Z",
  updated_at: "2026-01-01T00:00:00.000Z",
};

function deviceHealth(overrides: Partial<DeviceHealthOut> = {}): DeviceHealthOut {
  return {
    device_id: "d1",
    hostname: "WS-01",
    os_family: "windows",
    has_profile: true,
    profile: PROFILE,
    assignment: {
      id: "as1",
      profile_id: "p1",
      profile_name: "Base de oficina",
      device_id: null,
      device_group_id: "grp-1",
      level: "group",
      created_by: null,
      created_at: "2026-01-01T00:00:00.000Z",
    },
    resolution_level: "group",
    acknowledged_policy_version: 2,
    policy_drift: true,
    checks: [
      { check_id: "c1", check_type: "disk.free_pct", selector: "C:", comparator: "lt", threshold_display: "10", severity: "critical", for_ticks: 1, for_seconds: 300, evaluated_by: "agent", state: "breaching", last_value: "3", policy_version: 3, reported_at: "2026-03-01T09:00:00.000Z" },
      { check_id: "c2", check_type: "posture.firewall_enabled", selector: "", comparator: "is", threshold_display: "true", severity: "warning", for_ticks: 1, for_seconds: 300, evaluated_by: "agent", state: "unknown", last_value: "", policy_version: 3, reported_at: null },
    ],
    open_alerts: [alert({ id: "a9", check_type: "disk.free_pct", selector: "C:", severity: "critical" })],
    agent_last_seen_at: "2026-03-01T09:05:00.000Z",
    agent_online: true,
    offline_after_s: 900,
    ...overrides,
  };
}

function check(overrides: Partial<CheckOut> = {}): CheckOut {
  return {
    id: "c1",
    profile_id: "p1",
    check_type: "cpu.busy_pct",
    selector: "",
    comparator: "gt",
    threshold_num: 95,
    threshold_bool: null,
    threshold_display: "95",
    for_ticks: 3,
    for_seconds: 900,
    tick_interval_s: 300,
    severity: "warning",
    os_family: null,
    enabled: true,
    declares_notify: true,
    declares_open_ticket: false,
    remediate_approved_action_id: null,
    value_kind: "numeric",
    cadence: "tick",
    evaluated_by: "agent",
    available_os_families: ["linux", "windows"],
    selector_meaning: "",
    created_at: "2026-01-01T00:00:00.000Z",
    updated_at: "2026-01-01T00:00:00.000Z",
    ...overrides,
  };
}

const CHECK_TYPES: CheckTypeOut[] = [
  { name: "cpu.busy_pct", value_kind: "numeric", cadence: "tick", evaluated_by: "agent", os_families: ["linux", "windows"], legal_comparators: ["gt"], selector_meaning: "", enum_values: [] },
  { name: "backup.success_age_h", value_kind: "numeric", cadence: "posture", evaluated_by: "server", os_families: ["windows", "linux", "macos"], legal_comparators: ["gt"], selector_meaning: "", enum_values: [] },
  { name: "posture.antivirus_healthy", value_kind: "boolean", cadence: "posture", evaluated_by: "agent", os_families: ["windows"], legal_comparators: ["is"], selector_meaning: "el nombre del producto", enum_values: [] },
];

function approved(overrides: Partial<ApprovedActionOut> = {}): ApprovedActionOut {
  return {
    id: "act1",
    key: "restart-spooler",
    name: "Reiniciar la cola",
    description: "",
    job_type: "script.run",
    params_template: {},
    params_template_redacted_keys: [],
    script_id: "s1",
    script_name: "Restart Spooler",
    script_version: 4,
    script_live_version: 4,
    risk_class: "medium",
    destructive: false,
    max_target_scope: "device",
    autonomy: "require_approval",
    personas: ["health"],
    cooldown_s: 3600,
    max_per_device_per_day: 3,
    max_per_org_per_hour: 20,
    active: true,
    script_exemption: null,
    created_by: "u1",
    created_at: "2026-01-01T00:00:00.000Z",
    updated_at: "2026-01-01T00:00:00.000Z",
    ...overrides,
  };
}

const SETTINGS = {
  flap_window_s: 3600,
  flap_threshold: 5,
  flap_cooldown_s: 3600,
  unavailable_clear_after_s: 86400,
  alert_retention_days: 90,
  escalate_after_s: 3600,
  offline_after_s: 900,
  approval_ttl_s: 3600,
  default_alert_queue_id: "q1",
  default_ticket_category_id: "c1",
  monitoring_deep_link_template: null,
  auto_resolve_alert_tickets: true,
  configured: true,
  alert_tickets_enabled: true,
  updated_at: "2026-03-01T09:00:00.000Z",
  updated_by: "ops",
};

const fleet = (managed: number) => ({
  items: [],
  total: managed,
  state_counts: { all: 0, online: 0, offline: 0, assets: 0, certs: 0 },
  fleet: { total: managed, managed, online: managed, cert_attention: 0 },
});

function stubApi(overrides: Record<string, unknown> = {}) {
  const api = {
    me: vi.fn().mockResolvedValue(ME),
    listHealthAlerts: vi.fn().mockResolvedValue([]),
    getHealthAlert: vi.fn().mockResolvedValue(detail()),
    getHealthAlertSummary: vi.fn().mockResolvedValue({ open: 0, by_state: { active: 0, acknowledged: 0, muted: 0 }, by_severity: { critical: 0, warning: 0, info: 0 }, worst_severity: null }),
    getDeviceHealth: vi.fn().mockResolvedValue(deviceHealth()),
    getHealthProfileChecks: vi.fn().mockResolvedValue({ profile_id: "p1", policy_version: 3, tick_interval_s: 300, checks: [check()] }),
    listHealthCheckTypes: vi.fn().mockResolvedValue(CHECK_TYPES),
    listApprovedActions: vi.fn().mockResolvedValue([]),
    listHealthProfiles: vi.fn().mockResolvedValue([PROFILE]),
    listHealthAssignments: vi.fn().mockResolvedValue([]),
    getHealthSettings: vi.fn().mockResolvedValue(SETTINGS),
    updateHealthSettings: vi.fn().mockResolvedValue(SETTINGS),
    listDevices: vi.fn().mockResolvedValue(fleet(2)),
    listNotificationRules: vi.fn().mockResolvedValue([]),
    listChangeGates: vi.fn().mockResolvedValue([]),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

function renderUi(node: ReactNode, at = "/health") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[at]}>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Text recorded by the system or typed by a person, as it reads in any language. */
const STORED = ["WS-01", "C:", "restart-spooler", "Restart Spooler", "Restart", "act1", "INC-9", "windows", "linux", "macos", "grp-1"];

beforeEach(() => {
  mockUseApi.mockReset();
});

// -------------------------------------------------------------- the console

describe("the alert console, in Spanish", () => {
  const rows = () => [
    alert({ id: "act", device_hostname: "ACT-HOST", state: "active", severity: "critical" }),
    alert({ id: "ack", device_hostname: "ACK-HOST", state: "acknowledged" }),
    alert({ id: "mut", device_hostname: "MUT-HOST", state: "muted", muted_until: "2026-03-02T09:00:00.000Z", flapping: true, flap_count: 4 }),
    alert({ id: "rec", device_hostname: "REC-HOST", state: "cleared", cleared_at: "2026-03-01T10:00:00.000Z", cleared_reason: "recovery", severity: "info" }),
    alert({ id: "gone", device_hostname: "GONE-HOST", state: "cleared", cleared_at: "2026-03-01T10:00:00.000Z", cleared_reason: "unavailable_timeout", observed_value: "", threshold_display: "is true", check_type: "posture.antivirus_healthy" }),
  ];

  it("words the filters, the columns, the states, the reasons and the observations", async () => {
    stubApi({ listHealthAlerts: vi.fn().mockResolvedValue(rows()) });
    const { container } = renderUi(<AlertConsole onSelect={() => {}} />);
    await screen.findByText("ACT-HOST");
    expect(screen.getByText("Abiertas: 3 de 5 mostradas")).toBeInTheDocument();
    for (const header of ["Dispositivo", "Estado", "Gravedad", "Comprobación", "Cierre", "Observado", "Visto"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    const state = screen.getByRole("combobox", { name: "Filtrar por estado" });
    for (const option of ["Cualquier estado", "Abiertas (cualquier estado)", "Activa", "Reconocida", "Silenciada", "Cerrada"]) {
      expect(within(state).getByRole("option", { name: option })).toBeInTheDocument();
    }
    const severity = screen.getByRole("combobox", { name: "Filtrar por gravedad" });
    for (const option of ["Cualquier gravedad", "Crítica", "Advertencia", "Información"]) {
      expect(within(severity).getByRole("option", { name: option })).toBeInTheDocument();
    }
    const resolution = screen.getByRole("combobox", { name: "Filtrar por resolución" });
    for (const option of ["Cualquier resolución", "Contadas como resueltas", "Resueltas por la máquina", "Cerradas por una persona", "Cerradas, pero sin corrección"]) {
      expect(within(resolution).getByRole("option", { name: option })).toBeInTheDocument();
    }
    const row = (host: string) => screen.getByText(host).closest("tr") as HTMLElement;
    expect(within(row("ACK-HOST")).getByText("Reconocida")).toBeInTheDocument();
    expect(within(row("ACK-HOST")).getByText("Sigue abierta, así que nada la ha cerrado")).toBeInTheDocument();
    expect(within(row("MUT-HOST")).getByText("Silenciada")).toBeInTheDocument();
    expect(within(row("MUT-HOST")).getByText(/^hasta/)).toBeInTheDocument();
    expect(within(row("MUT-HOST")).getByText("se repite ×4")).toBeInTheDocument();
    expect(within(row("REC-HOST")).getByText("Cerrada — Recuperada")).toBeInTheDocument();
    expect(within(row("GONE-HOST")).getByText("Cerrada — El sensor desapareció")).toBeInTheDocument();
    expect(within(row("GONE-HOST")).getByText("Estado del antivirus")).toBeInTheDocument();
    expect(within(row("GONE-HOST")).getByText("No se observó nada, se esperaba true")).toBeInTheDocument();
    expect(within(row("ACT-HOST")).getByText("CPU ocupada")).toBeInTheDocument();
    expect(within(row("ACT-HOST")).getByText("97.4 por encima de 95")).toBeInTheDocument();
    expect(within(row("ACT-HOST")).getByRole("button", { name: "Abrir" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ordenar primero las que se repiten" })).toBeInTheDocument();
    expectNoEnglish(container, [...STORED, "ACT-HOST", "ACK-HOST", "MUT-HOST", "REC-HOST", "GONE-HOST", "true"]);
  });

  it("words the empty answer, the single-device narrowing and the sort toggle", async () => {
    const user = userEvent.setup();
    stubApi({ listHealthAlerts: vi.fn().mockResolvedValue([]) });
    const { container } = renderUi(<AlertConsole onSelect={() => {}} />, "/health?device=d1&state=open");
    expect(await screen.findByText("Ninguna alerta coincide con este filtro.")).toBeInTheDocument();
    expect(screen.getByText("Amplía los filtros de estado, gravedad o resolución de arriba.")).toBeInTheDocument();
    expect(screen.getByText(/Un dispositivo:/)).toBeInTheDocument();
    expect(screen.getByText("este dispositivo")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Mostrar todos los dispositivos" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Ordenar primero las que se repiten" }));
    expect(screen.getByRole("button", { name: "Ordenar por gravedad" })).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("says plainly when the alert list is not available or could not load", async () => {
    stubApi({ listHealthAlerts: vi.fn().mockRejectedValue(new ApiError(403, "")) });
    const forbidden = renderUi(<AlertConsole onSelect={() => {}} />);
    expect(await screen.findByText("Las alertas no están disponibles (permisos insuficientes).")).toBeInTheDocument();
    expectNoEnglish(forbidden.container);
    forbidden.unmount();

    stubApi({ listHealthAlerts: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    const failed = renderUi(<AlertConsole onSelect={() => {}} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No se pudo cargar la lista de alertas, así que este panel no muestra las alertas del parque, ni siquiera cero.",
    );
    expectNoEnglish(failed.container);
  });

  it("re-words a mounted console when the language is switched", async () => {
    stubApi({ listHealthAlerts: vi.fn().mockResolvedValue(rows()) });
    renderUi(<AlertConsole onSelect={() => {}} />);
    await screen.findByText("ACT-HOST");
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("3 open of 5 shown")).toBeInTheDocument();
    const row = screen.getByText("REC-HOST").closest("tr") as HTMLElement;
    expect(within(row).getByText("Cleared — Recovered")).toBeInTheDocument();
    expect(within(screen.getByText("ACT-HOST").closest("tr") as HTMLElement).getByText("CPU busy")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Filter by state" })).toBeInTheDocument();
  });
});

// --------------------------------------------------------------- the detail

describe("the alert detail, in Spanish", () => {
  it("words an acknowledged alert's meaning, facts, controls and actions", async () => {
    const user = userEvent.setup();
    stubApi({
      getHealthAlert: vi.fn().mockResolvedValue(
        detail({
          state: "active",
          severity: "critical",
          selector: "C:",
          check_type: "disk.free_pct",
          observed_value: "3",
          threshold_display: "lt 10",
          flapping: true,
          flap_count: 4,
          previous_alert_id: "a0",
          previous_alert_opened_at: "2026-02-20T09:00:00.000Z",
          previous_alert_ticket_number: "INC-5",
          monitoring_deep_link: "https://monitor.example/x",
          actions: [
            action(),
            action({ id: "ar2", kind: "notify", state: "done", approved_action_key: null, approved_action_name: null, script_name: null, autonomy: null, expires_at: null }),
            action({ id: "ar3", kind: "remediate", state: "blocked_flapping", last_error: "actions suppressed: x", expires_at: null }),
            action({ id: "ar4", kind: "remediate", state: "blocked_rate_limit", last_error: "rate fuse tripped: jobs j1 j2", expires_at: null, autonomy: "autonomous", destructive: true }),
            action({ id: "ar5", kind: "open_ticket", state: "dead", job_id: "job-77", expires_at: null }),
          ],
        }),
      ),
    });
    const { container } = renderUi(<AlertDetail alertId="a1" canManageAlerts canRemediate />);
    expect(await screen.findByText("Disco libre (C:) en WS-01")).toBeInTheDocument();
    expect(screen.getByText("Abierta y sin responsable.")).toBeInTheDocument();
    expect(screen.getByText("3 por debajo de 10")).toBeInTheDocument();
    for (const fact of ["Dispositivo", "Perfil", "Comprobación", "Observado", "Abierta", "Vista por última vez", "Repeticiones", "Escalada", "Ticket", "Silenciada hasta", "Reconocida"]) {
      expect(screen.getByText(fact, { selector: ".k, dt, span, div" })).toBeInTheDocument();
    }
    expect(screen.getByText(/Reabierta tras una alerta anterior del .*\(ticket INC-5\)\./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Abrir en la monitorización" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reconocer" })).toBeInTheDocument();
    expect(screen.getByLabelText("Silenciar durante (horas)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Silenciar" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cerrar manualmente" })).toBeInTheDocument();
    // The actions.
    expect(screen.getAllByRole("heading", { name: "Ejecutar la corrección", level: 3 }).length).toBe(3);
    expect(screen.getAllByText("Esperando aprobación").length).toBeGreaterThan(0);
    expect(screen.getByText("Avisar a las personas", { selector: "h3" })).toBeInTheDocument();
    expect(screen.getByText("Abrir un ticket", { selector: "h3" })).toBeInTheDocument();
    expect(screen.getByText("Bloqueada (se repite)")).toBeInTheDocument();
    expect(screen.getByText("Bloqueada por un límite de frecuencia")).toBeInTheDocument();
    expect(screen.getByText("Fallida")).toBeInTheDocument();
    expect(screen.getAllByText("Necesita aprobación").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Se ejecuta automáticamente").length).toBeGreaterThan(0);
    expect(screen.getAllByText("destructiva").length).toBeGreaterThan(0);
    expect(screen.getByText(/Esta alerta ha vuelto 4 veces en poco tiempo/)).toBeInTheDocument();
    expect(screen.getByText("Lo que registró el servidor")).toBeInTheDocument();
    expect(screen.getByText(/Bloqueada:/)).toBeInTheDocument();
    expect(screen.getAllByText(/Script fijado:/).length).toBeGreaterThan(0);
    expect(screen.getByText(/La aprobación caduca el/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Aprobar y enviar" })).toBeInTheDocument();
    expect(screen.getByLabelText("Motivo del rechazo")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Rechazar" })).toBeInTheDocument();
    expectNoEnglish(container, [...STORED, "disk.free_pct", "actions suppressed: x", "rate fuse tripped: jobs j1 j2", "jobs", "job-77", "lt", "monitor.example"]);
    // Closing by hand asks first, and says it cannot be undone.
    await user.click(screen.getByRole("button", { name: "Cerrar manualmente" }));
    const close = await screen.findByRole("dialog", { name: "Cerrar esta alerta a mano" });
    expect(close).toHaveTextContent("No se puede reabrir.");
    expect(within(close).getByRole("button", { name: "Cerrar alerta" })).toBeInTheDocument();
    expectNoEnglish(close, STORED);
  });

  it("words the approval confirmation, plain and destructive", async () => {
    const user = userEvent.setup();
    stubApi({
      getHealthAlert: vi.fn().mockResolvedValue(detail({ actions: [action({ destructive: true })] })),
    });
    renderUi(<AlertDetail alertId="a1" canManageAlerts canRemediate />);
    await user.click(await screen.findByRole("button", { name: "Aprobar y enviar" }));
    const dialog = await screen.findByRole("dialog", { name: "Aprobar una corrección destructiva" });
    expect(dialog).toHaveTextContent("Esta acción está marcada como destructiva");
    expect(dialog).toHaveTextContent("restart-spooler — Reiniciar la cola — Restart Spooler v4");
    expectNoEnglish(dialog, STORED);
  });

  it("words a refused mutation and the cleared reason panel", async () => {
    const user = userEvent.setup();
    stubApi({
      getHealthAlert: vi.fn().mockResolvedValue(detail({ state: "cleared", cleared_reason: "policy_removed", cleared_at: "2026-03-01T10:00:00.000Z" })),
      acknowledgeHealthAlert: vi.fn().mockRejectedValue(new ApiError(503, "")),
    });
    const { container } = renderUi(<AlertDetail alertId="a1" canManageAlerts canRemediate />);
    expect(await screen.findByText("Comprobación eliminada.")).toBeInTheDocument();
    expect(screen.getByText("Alguien quitó la comprobación del perfil. No se corrigió nada.")).toBeInTheDocument();
    expect(screen.getByText("no cuenta como corrección")).toBeInTheDocument();
    expect(screen.getByText("Esta alerta no desencadenó ninguna acción. La comprobación que la generó no declara ninguna.")).toBeInTheDocument();
    expectNoEnglish(container, STORED);
    void user;
  });

  it("words a refused acknowledgement", async () => {
    const user = userEvent.setup();
    stubApi({ acknowledgeHealthAlert: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    renderUi(<AlertDetail alertId="a1" canManageAlerts canRemediate />);
    await user.click(await screen.findByRole("button", { name: "Reconocer" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Falló la acción «Reconocer»:");
  });

  it("words an alert that is missing or forbidden", async () => {
    stubApi({ getHealthAlert: vi.fn().mockRejectedValue(new ApiError(404, "")) });
    const missing = renderUi(<AlertDetail alertId="a1" canManageAlerts canRemediate />);
    expect(await screen.findByText("No existe esa alerta.")).toBeInTheDocument();
    missing.unmount();
    stubApi({ getHealthAlert: vi.fn().mockRejectedValue(new ApiError(403, "")) });
    renderUi(<AlertDetail alertId="a1" canManageAlerts canRemediate />);
    expect(await screen.findByText("La alerta no está disponible (permisos insuficientes).")).toBeInTheDocument();
  });

  it("re-words an open drawer when the language is switched", async () => {
    stubApi({ getHealthAlert: vi.fn().mockResolvedValue(detail({ actions: [action()] })) });
    renderUi(<AlertDetail alertId="a1" canManageAlerts canRemediate />);
    await screen.findByText("CPU ocupada en WS-01");
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("CPU busy on WS-01")).toBeInTheDocument();
    expect(screen.getByText("Open and unowned.")).toBeInTheDocument();
    expect(screen.getByText("Run the fix", { selector: "h3" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve and dispatch" })).toBeInTheDocument();
  });
});

// ----------------------------------------------------------------- the page

describe("the health page, in Spanish", () => {
  const SUMMARY = {
    open: 4,
    by_state: { active: 1, acknowledged: 2, muted: 1 },
    by_severity: { critical: 1, warning: 3, info: 0 },
    worst_severity: "critical",
  };

  it("words the head, the readiness panel, the tiles and the panels below", async () => {
    stubApi({
      getHealthAlertSummary: vi.fn().mockResolvedValue(SUMMARY),
      listHealthAlerts: vi.fn().mockResolvedValue([
        alert({ id: "c", severity: "critical", check_type: "disk.free_pct", device_hostname: "WS-01" }),
        alert({ id: "w1", severity: "warning", state: "acknowledged", acknowledged_by: "u1" }),
        alert({ id: "w2", severity: "warning", state: "acknowledged", acknowledged_by: "u2", check_type: "memory.used_pct" }),
        alert({ id: "w3", severity: "warning", state: "muted", muted_until: "2026-03-05T09:00:00.000Z" }),
      ]),
      listApprovedActions: vi.fn().mockResolvedValue([approved({ autonomy: "autonomous" })]),
    });
    const { container } = renderUi(<HealthPage />);
    expect(await screen.findByRole("heading", { name: "Salud y alertas" })).toBeInTheDocument();
    expect(screen.getByText("Las alertas que ha generado el parque y lo que puede ejecutarse para cerrarlas.")).toBeInTheDocument();
    expect(await screen.findByText("Disco libre en WS-01")).toBeInTheDocument();
    expect(screen.getByText("Críticas")).toBeInTheDocument();
    expect(screen.getByText("Advertencias")).toBeInTheDocument();
    expect(screen.getByText("2 cpu · 1 memoria")).toBeInTheDocument();
    expect(screen.getByText("Por 2 personas")).toBeInTheDocument();
    expect(screen.getByText(/^Hasta el /)).toBeInTheDocument();
    expect(screen.getByText("Antes de que las alertas lleguen a alguien")).toBeInTheDocument();
    expect(await screen.findByText("Perfiles de estado")).toBeInTheDocument();
    expect(await screen.findByText("Acciones aprobadas")).toBeInTheDocument();
    expect(await screen.findByText("Configuración de salud")).toBeInTheDocument();
    expect(screen.getByText("Informes de alertas")).toBeInTheDocument();
    expectNoEnglish(container, [...STORED, "Base de oficina", "Para los puestos", "Reiniciar la cola", "script.run", "ops"]);
  });

  it("says what it cannot show without permission", async () => {
    stubApi({ me: vi.fn().mockResolvedValue({ ...ME, permissions: [] }) });
    renderUi(<HealthPage />);
    expect(await screen.findByText("No tienes permiso para ver el estado de los dispositivos, así que los paneles de abajo estarán vacíos.")).toBeInTheDocument();
  });

  it("agrees the people count and re-words the tiles on a switch", async () => {
    stubApi({
      getHealthAlertSummary: vi.fn().mockResolvedValue(SUMMARY),
      listHealthAlerts: vi.fn().mockResolvedValue([
        alert({ id: "w1", severity: "warning", state: "acknowledged", acknowledged_by: "u1" }),
      ]),
    });
    renderUi(<HealthPage />);
    expect(await screen.findByText("Por 1 persona")).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("heading", { name: "Health & alerts" })).toBeInTheDocument();
    expect(screen.getByText("By 1 person")).toBeInTheDocument();
  });
});

describe("the readiness panel, in Spanish", () => {
  it("words each step and sends the operator where it is fixed", async () => {
    stubApi({ listDevices: vi.fn().mockResolvedValue(fleet(0)) });
    const { container } = renderUi(<AlertReadiness />);
    expect(await screen.findByText("Hay un perfil de estado asignado")).toBeInTheDocument();
    const profile = screen.getByText("Hay un perfil de estado asignado").closest("li") as HTMLElement;
    expect(profile).toHaveTextContent("Que aquí la lista de alertas esté vacía no significa que el parque esté en buen estado.");
    expect(within(profile).getByRole("link", { name: "Asignar un perfil de estado" })).toHaveAttribute("href", "/health#profiles");
    expect(screen.getByText("Las alertas salen de comprobaciones que el agente ejecuta en cada equipo, y todavía ningún equipo lo ejecuta.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: ADD_COMPUTER_CTA.label })).toBeInTheDocument();
    expect(screen.getByText("Alguien recibe aviso de las alertas")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Avisar a alguien de las alertas" })).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("agrees the assignment count when the profile step is done", async () => {
    stubApi({ listHealthAssignments: vi.fn().mockResolvedValue([{ id: "x", profile_id: "p1", level: "org" }]) });
    renderUi(<AlertReadiness />);
    expect(await screen.findByText("Hay 1 asignación de perfil en vigor.")).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("1 profile assignment is in place.")).toBeInTheDocument();
  });

  it("keeps the assign link's label in step with the language", () => {
    setActiveLanguage("es");
    expect(ASSIGN_PROFILE_CTA.label).toBe("Asignar un perfil de estado");
    setActiveLanguage("en");
    expect(ASSIGN_PROFILE_CTA.label).toBe("Assign a health profile");
  });
});

// ------------------------------------------------------------------ report

describe("the report panel, in Spanish", () => {
  it("words the figures, their feet and the exclusion note", async () => {
    stubApi({
      listHealthAlerts: vi.fn().mockResolvedValue([
        alert({ id: "1", state: "cleared", cleared_reason: "recovery", cleared_at: "2026-03-01T10:00:00.000Z" }),
        alert({ id: "2", state: "cleared", cleared_reason: "manual", cleared_at: "2026-03-01T11:00:00.000Z" }),
        alert({ id: "3", state: "cleared", cleared_reason: "policy_removed", cleared_at: "2026-03-01T11:00:00.000Z" }),
        alert({ id: "4", state: "active" }),
      ]),
    });
    const { container } = renderUi(<HealthReportPanel />);
    expect(await screen.findByText("Informes de alertas")).toBeInTheDocument();
    expect(await screen.findByText("Alertas a la vista")).toBeInTheDocument();
    for (const label of ["Aún abiertas", "Tiempo medio de resolución", "Resueltas por la máquina", "Cerradas por una persona", "Tasa de autocorrección"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.getByText("1 excluida del recuento de resoluciones")).toBeInTheDocument();
    expect(screen.getByText("Las alertas reconocidas y silenciadas siguen abiertas.")).toBeInTheDocument();
    expect(screen.getByText(/El reparto entre máquina y persona se lee del motivo de cierre/)).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("will not print zeroes for a list that did not load", async () => {
    stubApi({ listHealthAlerts: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    const { container } = renderUi(<HealthReportPanel />);
    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudieron calcular estas cifras porque la lista de alertas no se cargó.");
    expectNoEnglish(container);
  });
});

// ---------------------------------------------------------------- profiles

describe("the profile list, in Spanish", () => {
  const ASSIGNMENTS = [
    { id: "x1", profile_id: "p1", level: "org" },
    { id: "x2", profile_id: "p1", level: "group" },
    { id: "x3", profile_id: "p1", level: "group" },
    { id: "x4", profile_id: "p1", level: "device" },
  ];

  it("words the table, the badges and the row actions", async () => {
    stubApi({
      listHealthProfiles: vi.fn().mockResolvedValue([
        PROFILE,
        { ...PROFILE, id: "p2", name: "Servidores", description: "", check_count: 0, assignment_count: 0, active: false },
      ]),
      listHealthAssignments: vi.fn().mockResolvedValue(ASSIGNMENTS),
    });
    const { container } = renderUi(<ProfileList canAdmin onSelect={() => {}} />);
    await screen.findByText("Base de oficina");
    expect(screen.getByText("Con qué se comprueba el parque.")).toBeInTheDocument();
    for (const header of ["Nombre", "Se comprueba cada", "Comprobaciones", "Se aplica a", "Versión", "Activo"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    const first = screen.getByText("Base de oficina").closest("tr") as HTMLElement;
    expect(within(first).getByText("5 min en vivo")).toBeInTheDocument();
    expect(within(first).getByText("1 h configuración")).toBeInTheDocument();
    expect(await within(first).findByText("predeterminado de la organización, 2 grupos, 1 dispositivo")).toBeInTheDocument();
    expect(within(first).getByText("activo")).toBeInTheDocument();
    const second = screen.getByText("Servidores").closest("tr") as HTMLElement;
    expect(within(second).getByText("sin comprobaciones")).toBeInTheDocument();
    expect(within(second).getByText("no alerta de nada")).toBeInTheDocument();
    expect(within(second).getByText("sin asignar")).toBeInTheDocument();
    expect(within(second).getByText("no se aplica a nada")).toBeInTheDocument();
    expect(within(second).getByText("retirado")).toBeInTheDocument();
    expect(within(second).getByRole("button", { name: "Reactivar" })).toBeInTheDocument();
    expect(within(first).getByRole("button", { name: "Retirar" })).toBeInTheDocument();
    expect(within(first).getByRole("button", { name: "Comprobaciones" })).toBeInTheDocument();
    expectNoEnglish(container, ["Base de oficina", "Para los puestos", "Servidores"]);
  });

  it("names the missing profile name, and re-words the refusal on a switch", async () => {
    const user = userEvent.setup();
    stubApi();
    renderUi(<ProfileList canAdmin onSelect={() => {}} />);
    await screen.findByText("Base de oficina");
    await user.click(screen.getByRole("button", { name: "Crear" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("El campo Nombre del perfil nuevo es obligatorio");
    await user.type(screen.getByLabelText("Nombre del perfil nuevo"), "<b>");
    await user.click(screen.getByRole("button", { name: "Crear" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("El nombre del perfil no puede contener < ni >");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent("Profile name cannot contain < or >");
  });

  it("asks before deleting, naming what it costs", async () => {
    const user = userEvent.setup();
    stubApi();
    renderUi(<ProfileList canAdmin onSelect={() => {}} />);
    await screen.findByText("Base de oficina");
    await user.click(screen.getByRole("button", { name: /Base de oficina/ }));
    await user.click(await screen.findByRole("menuitem", { name: "Eliminar" }));
    const dialog = await screen.findByRole("dialog", { name: "Eliminar perfil de estado" });
    expect(dialog).toHaveTextContent("Los dispositivos resueltos por sus asignaciones (2) dejan de comprobarse con sus comprobaciones (2)");
    expect(dialog).toHaveTextContent("Esta acción no se puede deshacer");
    expect(within(dialog).getByRole("button", { name: "Eliminar perfil" })).toBeInTheDocument();
    expectNoEnglish(dialog, ["Base de oficina"]);
  });

  it("says what it does not know when the list or the breakdown fails", async () => {
    stubApi({ listHealthProfiles: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    const failed = renderUi(<ProfileList canAdmin onSelect={() => {}} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo cargar la lista de perfiles.");
    failed.unmount();
    stubApi({ listHealthAssignments: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    renderUi(<ProfileList canAdmin onSelect={() => {}} />);
    expect(await screen.findByText("2 asignaciones")).toBeInTheDocument();
    expect(screen.getByText("desglose no disponible")).toBeInTheDocument();
  });
});

describe("the profile editor, in Spanish", () => {
  it("words the checks table, its badges and the availability matrix", async () => {
    stubApi({
      getHealthProfileChecks: vi.fn().mockResolvedValue({
        profile_id: "p1",
        policy_version: 3,
        tick_interval_s: 300,
        checks: [
          check({ available_os_families: ["linux", "windows"], remediate_approved_action_id: "act1", declares_open_ticket: true }),
          check({ id: "c2", check_type: "posture.antivirus_healthy", comparator: "is", threshold_display: "true", severity: "critical", os_family: "windows", available_os_families: ["windows"], declares_notify: false, enabled: false, selector: "Defender" }),
          check({ id: "c3", check_type: "backup.success_age_h", evaluated_by: "server", os_family: null, available_os_families: ["windows", "linux"] }),
        ],
      }),
      listApprovedActions: vi.fn().mockResolvedValue([approved({ id: "act1", autonomy: "autonomous" })]),
    });
    const { container } = renderUi(<ProfileEditor profileId="p1" canAdmin />);
    expect(await screen.findByText("Versión 3 · comprobaciones en vivo cada 5 min")).toBeInTheDocument();
    for (const header of ["Comprobación", "Condición", "Duración", "Gravedad", "Responde en", "Declara"]) {
      expect(screen.getAllByRole("columnheader", { name: header }).length).toBeGreaterThan(0);
    }
    expect(screen.getAllByText("3 comprobaciones seguidas (≈15 min, comprobando cada 5 min)").length).toBeGreaterThan(0);
    expect(screen.getAllByText("por encima de 95").length).toBeGreaterThan(0);
    expect(screen.getByText("se esperaba true")).toBeInTheDocument();
    expect(screen.getByText("desactivada")).toBeInTheDocument();
    expect(screen.getByText("silenciosa a propósito")).toBeInTheDocument();
    expect(screen.getAllByText("avisar").length).toBeGreaterThan(0);
    expect(screen.getByText("ticket")).toBeInTheDocument();
    expect(screen.getByText("corrección: restart-spooler")).toBeInTheDocument();
    expect(screen.getAllByText("sin corrección").length).toBeGreaterThan(0);
    expect(screen.getByText("evaluada por el servidor")).toBeInTheDocument();
    expect(screen.getAllByText(/^no disponible en /).length).toBeGreaterThan(0);
    expect(screen.getByText(/· fijada a windows/)).toBeInTheDocument();
    expect(screen.getByText("Tipos de comprobación y dónde pueden responderse")).toBeInTheDocument();
    expect(screen.getAllByText("Calculada por el servidor").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Con las comprobaciones en vivo").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Con las comprobaciones de configuración").length).toBeGreaterThan(0);
    expectNoEnglish(container, [...STORED, "cpu.busy_pct", "posture.antivirus_healthy", "healthy", "backup.success_age_h", "Defender", "true", "Base de oficina", "Reiniciar la cola", "script.run"]);
  });

  it("agrees the pending removal notice with its count, and quotes the reason in words", async () => {
    const user = userEvent.setup();
    stubApi();
    renderUi(<ProfileEditor profileId="p1" canAdmin />);
    await user.click(await screen.findByRole("button", { name: "Quitar" }));
    const notice = await screen.findByText(/Al guardar se quitará 1 comprobación/);
    expect(notice).toHaveTextContent("se cierra como «comprobación eliminada», lo que no es una corrección");
    expect(notice.querySelector("strong")).toHaveTextContent("«comprobación eliminada»");
    expect(screen.getByRole("button", { name: "Guardar el conjunto de comprobaciones" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Conservar" })).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText(/Saving will remove 1 check\(s\)\./)).toBeInTheDocument();
  });

  it("words an empty profile and a refused load", async () => {
    stubApi({ getHealthProfileChecks: vi.fn().mockResolvedValue({ profile_id: "p1", policy_version: 1, tick_interval_s: 300, checks: [] }) });
    const empty = renderUi(<ProfileEditor profileId="p1" canAdmin />);
    expect(await screen.findByText("Este perfil no incluye ninguna comprobación, así que no alerta de nada.")).toBeInTheDocument();
    empty.unmount();
    stubApi({ getHealthProfileChecks: vi.fn().mockRejectedValue(new ApiError(403, "")) });
    renderUi(<ProfileEditor profileId="p1" canAdmin />);
    expect(await screen.findByText("Las comprobaciones no están disponibles (permisos insuficientes).")).toBeInTheDocument();
  });

  it("words the matrix cells for each family", () => {
    const { container } = renderUi(<CheckTypeMatrix types={CHECK_TYPES} />);
    expect(screen.getAllByLabelText(/^cpu\.busy_pct responde en windows$/).length).toBe(1);
    expect(screen.getByLabelText("cpu.busy_pct no está disponible en macos")).toBeInTheDocument();
    expect(screen.getAllByText("sí").length).toBeGreaterThan(0);
    expectNoEnglish(container, ["cpu.busy_pct", "backup.success_age_h", "posture.antivirus_healthy", "healthy", "windows", "linux", "macos"]);
  });
});

// ------------------------------------------------------- approved actions

describe("the approved-action registry, in Spanish", () => {
  const EXEMPT = approved({
    id: "act2",
    key: "clean-temp",
    name: "Limpiar temporales",
    job_type: "script.run",
    script_name: "Clean Temp",
    script_version: 2,
    script_live_version: 3,
    autonomy: "autonomous",
    risk_class: "high",
    destructive: true,
    active: false,
    script_exemption: { id: "ex1", script_id: "s2", script_name: "Clean Temp", script_version: 2, reason: "", granted_by: "ops", granted_at: "2026-02-01T00:00:00.000Z" },
  });

  it("words the table, the loud notice, the exemptions and the grant form", async () => {
    stubApi({ listApprovedActions: vi.fn().mockResolvedValue([approved(), EXEMPT]) });
    const { container } = renderUi(<ApprovedActionList canActionAdmin />);
    await screen.findByText("Reiniciar la cola");
    expect(screen.getByText("Lo que puede ejecutarse en un equipo como SYSTEM o root.")).toBeInTheDocument();
    expect(screen.getByText("1 acción se ejecuta automáticamente")).toBeInTheDocument();
    expect(screen.getAllByText(/sin ninguna persona en el circuito, en cualquier dispositivo cuya comprobación la declare\./).length).toBeGreaterThan(0);
    for (const header of ["Acción", "Qué se ejecuta", "Riesgo", "Destructiva", "Autonomía", "Límites", "Activa"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    const plain = screen.getByText("Reiniciar la cola").closest("tr") as HTMLElement;
    expect(within(plain).getByText("Medio")).toBeInTheDocument();
    expect(within(plain).getByText("no destructiva")).toBeInTheDocument();
    expect(within(plain).getByText("Necesita aprobación")).toBeInTheDocument();
    expect(within(plain).getByText("1 h de espera · 3/dispositivo/día · 20/organización/hora")).toBeInTheDocument();
    expect(within(plain).getByText("activa")).toBeInTheDocument();
    expect(within(plain).getByRole("button", { name: "Desactivar" })).toBeInTheDocument();
    const loud = screen.getByText("Limpiar temporales", { selector: ".cell-title" }).closest("tr") as HTMLElement;
    expect(within(loud).getByText("Alto")).toBeInTheDocument();
    expect(within(loud).getByText("Script editado")).toBeInTheDocument();
    expect(within(loud).getByText(/La versión vigente es la v3, así que esta acción se negará a ejecutarse\./)).toBeInTheDocument();
    expect(within(loud).getByText("Se ejecuta automáticamente")).toBeInTheDocument();
    expect(within(loud).getByText("inactiva")).toBeInTheDocument();
    expect(within(loud).getByRole("button", { name: "Activar" })).toBeInTheDocument();
    // Exemptions.
    expect(screen.getByText("Scripts marcados como seguros")).toBeInTheDocument();
    expect(screen.getByText("— (sin motivo registrado)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Revocar" })).toBeInTheDocument();
    expect(screen.getByLabelText("Eximir restart-spooler (Restart Spooler v4)")).toHaveAttribute("placeholder", "¿Por qué es segura esta versión exacta?");
    expectNoEnglish(container, [...STORED, "clean-temp", "Clean Temp", "ops", "script.run", "Reiniciar la cola", "Limpiar temporales"]);
  });

  it("words each privileged confirmation before it is made", async () => {
    const user = userEvent.setup();
    stubApi({ listApprovedActions: vi.fn().mockResolvedValue([approved(), EXEMPT]) });
    renderUi(<ApprovedActionList canActionAdmin />);
    await screen.findByText("Reiniciar la cola");
    // Activating an autonomous action.
    await user.click(screen.getByRole("button", { name: "Activar" }));
    let dialog = await screen.findByRole("dialog", { name: "Activar esta acción aprobada" });
    expect(dialog).toHaveTextContent("Esta acción se ejecuta AUTOMÁTICAMENTE, sin ninguna persona en el circuito");
    expect(dialog).toHaveTextContent("hasta 3 por dispositivo y día y 20 por hora en toda la organización");
    expectNoEnglish(dialog, [...STORED, "clean-temp", "Clean Temp"]);
    await user.click(within(dialog).getByRole("button", { name: "Cancelar" }));
    // Revoking an exemption.
    await user.click(screen.getByRole("button", { name: "Revocar" }));
    dialog = await screen.findByRole("dialog", { name: "Revocar esta exención de script" });
    expect(dialog).toHaveTextContent("una autónoma pasa a «Necesita aprobación»");
    expectNoEnglish(dialog, [...STORED, "clean-temp", "Clean Temp"]);
  });

  it("words the exemption confirmation and a refused change", async () => {
    const user = userEvent.setup();
    stubApi({
      listApprovedActions: vi.fn().mockResolvedValue([approved()]),
      exemptApprovedActionScript: vi.fn().mockRejectedValue(new ApiError(503, "")),
    });
    renderUi(<ApprovedActionList canActionAdmin />);
    await screen.findByText("Reiniciar la cola");
    await user.type(screen.getByLabelText("Eximir restart-spooler (Restart Spooler v4)"), "ya revisado");
    await user.click(screen.getByRole("button", { name: "Conceder exención" }));
    const dialog = await screen.findByRole("dialog", { name: "Declarar esta versión del script como no destructiva" });
    expect(dialog).toHaveTextContent("La concesión está ligada a la versión, no a restart-spooler");
    await user.click(within(dialog).getByRole("button", { name: "Conceder exención" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Falló la acción «Conceder exención»:");
  });

  it("words an empty registry, a refusal and a failed load", async () => {
    stubApi({ listApprovedActions: vi.fn().mockResolvedValue([]) });
    const empty = renderUi(<ApprovedActionList canActionAdmin={false} />);
    expect(await screen.findByText("No hay acciones aprobadas. Esta organización no puede enviar ninguna corrección.")).toBeInTheDocument();
    expect(screen.getByText("No hay exenciones vigentes.")).toBeInTheDocument();
    empty.unmount();
    stubApi({ listApprovedActions: vi.fn().mockRejectedValue(new ApiError(403, "")) });
    const forbidden = renderUi(<ApprovedActionList canActionAdmin />);
    expect(await screen.findByText("El registro de acciones aprobadas no está disponible (permisos insuficientes).")).toBeInTheDocument();
    forbidden.unmount();
    stubApi({ listApprovedActions: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    renderUi(<ApprovedActionList canActionAdmin />);
    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo cargar el registro de acciones aprobadas.");
  });

  it("agrees the automatic-action count with several actions", async () => {
    stubApi({ listApprovedActions: vi.fn().mockResolvedValue([approved({ autonomy: "autonomous" }), approved({ id: "b", key: "b", autonomy: "autonomous" })]) });
    renderUi(<ApprovedActionList canActionAdmin />);
    expect(await screen.findByText("2 acciones se ejecutan automáticamente")).toBeInTheDocument();
    expect(screen.getByText(/en cualquier dispositivo cuya comprobación las declare\./)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- settings

describe("the settings panel, in Spanish", () => {
  it("words the badges, the fields with their units and the notice for no queue", async () => {
    stubApi({
      getHealthSettings: vi.fn().mockResolvedValue({ ...SETTINGS, configured: false, alert_tickets_enabled: false, auto_resolve_alert_tickets: false }),
    });
    const { container } = renderUi(<HealthSettingsPanel canAdmin />);
    expect(await screen.findByText("Usando los valores predeterminados")).toBeInTheDocument();
    expect(screen.getByText("Las alertas aún no pueden generar tickets")).toBeInTheDocument();
    expect(screen.getByText("los tickets de alertas siguen abiertos al recuperarse")).toBeInTheDocument();
    expect(screen.getByText(/No hay ninguna cola de alertas ni categoría de ticket predeterminada/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Elegir adónde van los tickets de alertas" })).toBeInTheDocument();
    expect(screen.getByText("Marcar un dispositivo como sin conexión tras")).toBeInTheDocument();
    expect(screen.getByText("Cuánto puede quedarse en silencio un dispositivo antes de que generemos una alerta.")).toBeInTheDocument();
    expect(screen.getByText("Eliminar las alertas cerradas tras")).toBeInTheDocument();
    expect(screen.getByText("Las alertas cerradas se eliminan pasados estos días.")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Guardar" }).length).toBeGreaterThan(0);
    expectNoEnglish(container, ["ops"]);
  });

  it("says nothing has changed, and re-words that on a switch", async () => {
    const user = userEvent.setup();
    stubApi();
    renderUi(<HealthSettingsPanel canAdmin />);
    await screen.findByText("configurada");
    await user.click(screen.getByRole("button", { name: "Guardar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Todavía no ha cambiado nada: edita un valor antes de guardar");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent("Nothing has changed yet — edit a value before saving");
  });

  it("quotes the bound in the field's own unit", async () => {
    const user = userEvent.setup();
    stubApi();
    renderUi(<HealthSettingsPanel canAdmin />);
    await screen.findByText("configurada");
    const retention = screen.getByText("Eliminar las alertas cerradas tras").closest(".field") as HTMLElement;
    const input = within(retention).getByRole("textbox");
    await user.clear(input);
    await user.type(input, "5");
    await user.click(screen.getByRole("button", { name: "Guardar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Eliminar las alertas cerradas tras debe estar entre 90 y 3650 días");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent("Delete closed alerts after must be between 90 and 3650 days");
  });

  it("words a forbidden or failed load and the read-only reason", async () => {
    stubApi({ getHealthSettings: vi.fn().mockRejectedValue(new ApiError(403, "")) });
    const forbidden = renderUi(<HealthSettingsPanel canAdmin />);
    expect(await screen.findByText("La configuración no está disponible (permisos insuficientes).")).toBeInTheDocument();
    forbidden.unmount();
    stubApi({ getHealthSettings: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    const failed = renderUi(<HealthSettingsPanel canAdmin />);
    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo cargar la configuración de salud");
    failed.unmount();
    stubApi();
    renderUi(<HealthSettingsPanel canAdmin={false} />);
    expect(await screen.findByText("No tienes permiso para cambiar la configuración de salud.")).toBeInTheDocument();
  });
});

// ------------------------------------------------------------ device panel

describe("a device's health panel, in Spanish", () => {
  it("words the headline, the four bands, the checks and the open alerts", async () => {
    stubApi({ getDeviceHealth: vi.fn().mockResolvedValue(deviceHealth()) });
    const { container } = renderUi(<DeviceHealthPanel deviceId="d1" canCheckNow onOpenAlert={() => {}} />);
    expect(await screen.findByText("Salud de WS-01")).toBeInTheDocument();
    expect(screen.getAllByText("1 alerta abierta").length).toBeGreaterThan(0);
    expect(screen.getByText("agente informando")).toBeInTheDocument();
    expect(screen.getByText("una asignación de grupo (grp-1)")).toBeInTheDocument();
    expect(screen.getByText("el agente tiene v2")).toBeInTheDocument();
    expect(screen.getByText("0 dentro del umbral")).toBeInTheDocument();
    expect(screen.getByText("1 incumpliendo")).toBeInTheDocument();
    expect(screen.getByText("0 no disponibles")).toBeInTheDocument();
    expect(screen.getByText("1 nunca ha informado")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Muestrear ahora" })).toBeInTheDocument();
    expect(screen.getByText("Nunca ha informado")).toBeInTheDocument();
    expect(screen.getByText("Esta comprobación no ha informado de ningún valor")).toBeInTheDocument();
    expect(screen.getByText("nunca")).toBeInTheDocument();
    expect(screen.getByText("Alertas abiertas")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Abrir" })).toBeInTheDocument();
    // "enabled" is the wire word inside the recorded check type "posture.firewall_enabled".
    expectNoEnglish(container, [...STORED, "disk.free_pct", "posture.firewall_enabled", "enabled", "true", "Base de oficina", "grp-1"]);
  });

  it("will not call a quiet or unmonitored device healthy", async () => {
    stubApi({ getDeviceHealth: vi.fn().mockResolvedValue(deviceHealth({ has_profile: false, profile: null, assignment: null, resolution_level: null, checks: [], open_alerts: [], agent_online: false, agent_last_seen_at: null })) });
    const { container } = renderUi(<DeviceHealthPanel deviceId="d1" canCheckNow={false} />);
    expect(await screen.findByText("Sin perfil de estado")).toBeInTheDocument();
    expect(screen.getByText("Ningún perfil de estado se aplica a este dispositivo.")).toBeInTheDocument();
    expect(screen.getByText(/Último contacto nunca, fuera de la ventana de desconexión de 15 min\./)).toBeInTheDocument();
    expect(screen.getByText("No hay alertas abiertas, pero este dispositivo no está informando, así que eso no prueba que esté bien.")).toBeInTheDocument();
    expectNoEnglish(container, STORED);
  });

  it("words a missing or forbidden device", async () => {
    stubApi({ getDeviceHealth: vi.fn().mockRejectedValue(new ApiError(404, "")) });
    const missing = renderUi(<DeviceHealthPanel deviceId="d1" canCheckNow />);
    expect(await screen.findByText("No existe ese dispositivo.")).toBeInTheDocument();
    missing.unmount();
    stubApi({ getDeviceHealth: vi.fn().mockRejectedValue(new ApiError(403, "")) });
    renderUi(<DeviceHealthPanel deviceId="d1" canCheckNow />);
    expect(await screen.findByText("El estado del dispositivo no está disponible (permisos insuficientes).")).toBeInTheDocument();
  });
});

// ------------------------------------------- sentences built outside React

describe("the health sentences built outside React, in Spanish", () => {
  beforeEach(() => setActiveLanguage("es"));

  it("names every check the registry knows, in both languages", () => {
    const registry = [
      "cpu.busy_pct", "memory.used_pct", "disk.free_pct", "disk.free_bytes", "disk.smart_ok", "process.present",
      "service.state", "posture.disk_encryption", "posture.firewall_enabled", "eventlog.disk_error",
      "eventlog.service_crash", "eventlog.kernel_fault", "script.policy_failed", "agent.offline",
      "backup.success_age_h", "backup.last_run_failed", "backup.offsite_lag_h", "backup.restore_test_age_d",
      "posture.antivirus_healthy", "service.running", "cert.days_left",
    ];
    const spanish = registry.map(checkLabel);
    expect(spanish.every((label, at) => label !== registry[at])).toBe(true);
    expect(checkLabel("cpu.busy_pct")).toBe("CPU ocupada");
    expect(checkLabel("disk.free_pct")).toBe("Disco libre");
    expect(checkLabel("cert.days_left")).toBe("Caducidad del certificado");
    // A type the console has not met reads as its own key.
    expect(checkLabel("future.check")).toBe("future.check");
    setActiveLanguage("en");
    expect(checkLabel("disk.free_pct")).toBe("Disk free");
    expect(registry.map(checkLabel).every((label, at) => label !== registry[at])).toBe(true);
  });

  it("words thresholds and observations with their operator", () => {
    expect(describeThreshold("gt", "90")).toBe("por encima de 90");
    expect(describeThreshold("lte", "5")).toBe("igual o por debajo de 5");
    expect(describeThreshold("neq", "running")).toBe("distinto de running");
    expect(describeObservation({ observed_value: "96", threshold_display: "gt 95" })).toBe("96 por encima de 95");
    expect(describeObservation({ observed_value: "stopped", threshold_display: "is running" })).toBe("stopped, se esperaba running");
    expect(describeObservation({ observed_value: "", threshold_display: "" })).toBe("No se observó nada");
    expect(describeObservation({ observed_value: "96", threshold_display: "weird 90" })).toBe("96 frente a weird 90");
  });

  it("words the state tables and the status line", () => {
    expect(ALERT_STATE_PRESENTATION.muted.label).toBe("Silenciada");
    expect(CLEARED_REASON_PRESENTATION.device_inactive.label).toBe("Dispositivo dado de baja");
    expect(SEVERITY_PRESENTATION.warning.label).toBe("Advertencia");
    expect(CHECK_STATE_PRESENTATION.unavailable.label).toBe("No disponible");
    expect(ACTION_STATE_PRESENTATION.awaiting_approval.label).toBe("Esperando aprobación");
    expect(AUTONOMY_PRESENTATION.autonomous.label).toBe("Se ejecuta automáticamente");
    expect(ACTION_KIND_LABEL.open_ticket).toBe("Abrir un ticket");
    expect(describeAlertStatus({ state: "cleared", cleared_reason: "manual" })).toBe("Cerrada — Cerrada por una persona");
    // The variants and flags are data, not words.
    expect(ALERT_STATE_PRESENTATION.acknowledged.open).toBe(true);
    expect(CLEARED_REASON_PRESENTATION.manual.countsAsResolution).toBe(true);
    setActiveLanguage("en");
    expect(ALERT_STATE_PRESENTATION.muted.label).toBe("Muted");
    expect(ACTION_STATE_PRESENTATION.blocked_flapping.label).toBe("Blocked (repeating)");
  });

  it("agrees the device headline with its alert count", () => {
    const base = deviceHealth({ checks: [], open_alerts: [] });
    expect(describeDeviceHealth({ ...base, open_alerts: [alert(), alert({ id: "b" })] }).label).toBe("2 alertas abiertas");
    expect(describeDeviceHealth({ ...base, open_alerts: [alert()] }).label).toBe("1 alerta abierta");
    expect(describeDeviceHealth({ ...base, has_profile: false }).label).toBe("Sin perfil de estado");
    expect(describeDeviceHealth({ ...base, agent_online: false }).label).toBe("Sin conexión");
    expect(describeDeviceHealth(base).label).toBe("Sin comprobaciones");
    const partial = describeDeviceHealth({
      ...base,
      checks: [
        { ...deviceHealth().checks[0]!, state: "unavailable" },
        { ...deviceHealth().checks[0]!, state: "unavailable" },
        { ...deviceHealth().checks[1]!, state: "unknown" },
      ],
    });
    expect(partial.label).toBe("Respuesta parcial");
    expect(partial.detail).toBe("2 comprobaciones que el dispositivo no pudo responder y 1 sin ninguna respuesta hasta ahora.");
    expect(
      describeDeviceHealth({ ...base, checks: [{ ...deviceHealth().checks[0]!, state: "ok" }] }).label,
    ).toBe("En buen estado");
  });

  it("words durations, pauses and seconds in the reader's number format", () => {
    expect(describeDuration({ for_ticks: 1, for_seconds: 300, tick_interval_s: 300 })).toBe("1 comprobación (≈5 min, comprobando cada 5 min)");
    expect(describeDuration({ for_ticks: 3, for_seconds: 900, tick_interval_s: 300 })).toBe("3 comprobaciones seguidas (≈15 min, comprobando cada 5 min)");
    expect(describeRepeatPause(4)).toContain("ha vuelto 4 veces");
    expect(describeRepeatPause(1)).toContain("ha vuelto varias veces");
    expect(formatSeconds(90)).toBe("1,5 min");
    expect(formatSeconds(45)).toBe("45 s");
    expect(formatSeconds(7200)).toBe("2 h");
    expect(formatSeconds(129600)).toBe("1,5 d");
    setActiveLanguage("en");
    expect(formatSeconds(90)).toBe("1.5 min");
  });

  it("words the profile and approval sentences", () => {
    expect(describeAssignments({ id: "p" }, [])).toBe("sin asignar");
    expect(describeAssignments({ id: "p" }, [{ profile_id: "p", level: "group" }, { profile_id: "p", level: "device" }, { profile_id: "p", level: "device" }])).toBe("1 grupo, 2 dispositivos");
    expect(describeDeleteImpact({ assignment_count: 0, check_count: 1 })).toBe(
      "No está asignado a nada, así que ningún dispositivo deja de comprobarse, pero se va con él su comprobación (1).",
    );
    expect(describeDeleteImpact({ assignment_count: 0, check_count: 3 })).toContain("se van con él sus comprobaciones (3)");
    expect(describeApprovalSubject({ kind: "remediate", approved_action_key: "k", approved_action_name: "Nombre", script_name: "S", script_version: 2 })).toBe("k — Nombre — S v2");
    const a = approved({ autonomy: "autonomous", active: true });
    expect(decisionTitle({ kind: "active", action: a })).toBe("Desactivar esta acción aprobada");
    expect(decisionConfirmLabel({ kind: "active", action: a })).toBe("Desactivar");
    expect(decisionSubject({ kind: "active", action: a })).toBe("restart-spooler — Reiniciar la cola");
    expect(decisionBody({ kind: "active", action: { ...a, active: false } })).toContain("AUTOMÁTICAMENTE");
    expect(decisionBody({ kind: "active", action: { ...a, active: false, autonomy: "suggest" } })).toContain("sigue esperando la aprobación de una persona");
  });

  it("keeps the settings table in step with the language", () => {
    const retention = NUMERIC_FIELDS.find((field) => field.key === "alert_retention_days")!;
    expect(retention.label).toBe("Eliminar las alertas cerradas tras");
    expect(retention.unit).toBe("días");
    expect(NUMERIC_FIELDS.map((field) => field.label)).toHaveLength(8);
    setActiveLanguage("en");
    expect(retention.label).toBe("Delete closed alerts after");
    expect(retention.unit).toBe("days");
  });
});

void waitFor;
