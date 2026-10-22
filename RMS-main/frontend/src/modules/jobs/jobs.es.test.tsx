/**
 * The job history and the dispatch dialog in Spanish. What is STORED (a wire
 * job type shown beside its name, a job id, a hostname, a log line) reads as
 * recorded; every label, sentence and status around it is Spanish.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { en } from "../../i18n/en";
import { es } from "../../i18n/es";
import { setActiveLanguage } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import type { ApiClient, Device, Job, Me } from "../../lib/api";
import { DispatchJobDialog } from "./DispatchJobDialog";
import { JobsPage } from "./JobsPage";
import {
  canRetry,
  describeAge,
  describeCancelRequest,
  describeCancellation,
  describeLateCancel,
  describeRetry,
  describeSpan,
  describeStop,
  describeStopEffect,
  typeBreakdown,
  whyNotRetryable,
  whyNotStoppable,
} from "./jobState";
import { DISPATCHABLE_JOB_TYPES, jobTypeLabel, KNOWN_JOB_TYPES } from "./jobTypes";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
vi.mock("../../lib/ws", () => ({ useLiveEvents: () => {} }));

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operador",
  permissions: ["jobs.read", "jobs.dispatch"],
  timezone: "Europe/Madrid",
};

const NOW = new Date("2026-08-20T10:00:00Z");
const DEVICE_ID = "11111111-1111-1111-1111-111111111111";

const BASE: Job = {
  id: "22222222-2222-2222-2222-222222222222",
  type: "inventory.collect",
  device_id: DEVICE_ID,
  status: "succeeded",
  attempts: 1,
  params: {},
  result: { ok: true },
  issued_at: "2026-08-20T09:00:00Z",
  expires_at: "2026-08-20T09:05:00Z",
  started_at: null,
  finished_at: null,
  cancel_requested_at: null,
  cancel_outcome: null,
  logs: [],
};
const STALE: Job = {
  ...BASE,
  id: "33333333-3333-3333-3333-333333333333",
  type: "patch.scan",
  status: "queued",
  attempts: 0,
  result: null,
  issued_at: "2026-08-19T09:00:00Z",
};
const RUNNING: Job = {
  ...BASE,
  id: "44444444-4444-4444-4444-444444444444",
  type: "patch.apply",
  status: "running",
  result: null,
  started_at: "2026-08-20T09:30:00Z",
};
const FAILED: Job = {
  ...BASE,
  id: "55555555-5555-5555-5555-555555555555",
  type: "backup.run",
  status: "failed",
  result: null,
  started_at: "2026-08-20T09:10:00Z",
  finished_at: "2026-08-20T09:20:00Z",
  logs: [{ level: "error", message: "disk full" }],
} as Job;

function renderPage(jobs: Job[], path = "/jobs", detail?: Job) {
  mockUseApi.mockReturnValue({
    me: vi.fn().mockResolvedValue(ME),
    listJobs: vi.fn().mockResolvedValue({ items: jobs, total: jobs.length }),
    getJob: vi.fn().mockResolvedValue(detail ?? jobs[0]),
    listDevices: vi.fn().mockResolvedValue({ items: [{ id: DEVICE_ID, hostname: "ws-alpha" }], total: 1 }),
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <JobsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(NOW);
  mockUseApi.mockReset();
});
afterEach(() => {
  vi.useRealTimers();
});

// The hostname and the wire job types are stored text, shown as recorded.
const STORED = ["ws-alpha"];
// "disk" is the word the job's own log line (`[error] disk full`) is recorded in.
const WITH_LOG = [...STORED, "disk"];

describe("the job history in Spanish", () => {
  it("words the tiles, the stuck banner, the timeline note, the filters and the rows", async () => {
    const { container } = renderPage([BASE, STALE, RUNNING, FAILED]);
    expect(await screen.findByRole("heading", { name: "Historial de tareas" })).toBeInTheDocument();
    expect(await screen.findByText("Aplicar parches")).toBeInTheDocument();
    // Tiles.
    for (const label of ["Tareas", "En ejecución", "Fallidas", "En espera más de 15 min"]) {
      expect([...document.querySelectorAll(".stat-label")].some((el) => el.textContent === label)).toBe(true);
    }
    expect(screen.getByText("Ejecución más larga: 30 min")).toBeInTheDocument();
    expect(screen.getByText("1 backup.run")).toBeInTheDocument();
    expect(screen.getByText("1 sin enviar")).toBeInTheDocument();
    // The banner needs nothing running; this fleet has a run, so it is absent.
    expect(screen.queryByText(/lleva esperando más de 15 minutos/)).not.toBeInTheDocument();
    // The timeline note for the unstarted job.
    expect(screen.getByText("Últimas seis horas")).toBeInTheDocument();
    expect(screen.getByText(/Una tarea de esta ventana no ha empezado/)).toBeInTheDocument();
    // Filters and count.
    expect(screen.getByRole("button", { name: "Esperando demasiado" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "En cola" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancelado" })).toBeInTheDocument();
    expect(screen.getByText("4 tareas")).toBeInTheDocument();
    expect(screen.getByLabelText("Buscar tareas")).toBeInTheDocument();
    // The table.
    const table = screen.getByRole("table");
    for (const header of ["Nombre", "Estado", "Intentos", "Emitida", "Espera"]) {
      expect(within(table).getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(within(table).getByText("Recopilar inventario")).toBeInTheDocument();
    expect(within(table).getByText("Buscar parches")).toBeInTheDocument();
    expect(within(table).getByText("Ejecutar copia de seguridad")).toBeInTheDocument();
    // The wire type stays beside the name, as recorded.
    expect(within(table).getByText("patch.scan")).toBeInTheDocument();
    expect(within(table).getAllByRole("link", { name: "Detalle" })).toHaveLength(4);
    // Ages.
    expect(within(table).getByText("hace 25 h")).toBeInTheDocument();
    expectNoEnglish(container, STORED);
  });

  it("words the stuck banner and the empty states", async () => {
    const stuck = renderPage([STALE]);
    expect(await screen.findByText(/1 tarea lleva esperando más de 15 minutos y no se está ejecutando nada/)).toBeInTheDocument();
    expect(screen.getByText("Buscar parches")).toBeInTheDocument();
    expectNoEnglish(stuck.container, STORED);
    stuck.unmount();

    const none = renderPage([]);
    expect(await screen.findByText("Aún no se ha enviado ninguna tarea.")).toBeInTheDocument();
    expect(screen.getByText("Nunca se ha enviado ninguna tarea")).toBeInTheDocument();
    expect(screen.getByText("No se ha emitido nada en las últimas seis horas")).toBeInTheDocument();
    expectNoEnglish(none.container, STORED);
  });

  it("filters to nothing with a Spanish message", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const { container } = renderPage([BASE], "/jobs?status=failed");
    expect(await screen.findByText("Ninguna tarea coincide con ese filtro.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Todas" }));
    expect(await screen.findByText("Recopilar inventario")).toBeInTheDocument();
    expectNoEnglish(container, STORED);
  });

  it("words the detail of a finished job, its logs and its retry dialog", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const { container } = renderPage([FAILED], `/jobs?job=${FAILED.id}`);
    expect(await screen.findByRole("heading", { name: "Ejecutar copia de seguridad" })).toBeInTheDocument();
    for (const label of ["Destino", "Intentos", "Emitida", "Caduca", "Iniciada", "Finalizada"]) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
    expect(screen.getByText("Esta tarea no lleva parámetros.")).toBeInTheDocument();
    expect(screen.getByText("Aún no hay resultado: esta tarea no se ha ejecutado.")).toBeInTheDocument();
    expect(screen.getByText("[error] disk full")).toBeInTheDocument();
    expect(screen.getByText(/Esta tarea ha terminado\. Una tarea terminada es definitiva/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Ejecutar esta tarea de nuevo" }));
    const dialog = await screen.findByRole("dialog", { name: "Ejecutar esta tarea de nuevo" });
    expect(within(dialog).getByText(/Esto no vuelve a enviar la tarea original/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Crear la nueva tarea" })).toBeInTheDocument();
    expectNoEnglish(container, WITH_LOG);
  });

  it("words the stop control and its confirmation for a queued and a running job", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const queued = renderPage([STALE], `/jobs?job=${STALE.id}`);
    await user.click(await screen.findByRole("button", { name: "Cancelar esta tarea" }));
    const withdraw = await screen.findByRole("dialog", { name: "Retirar esta tarea" });
    expect(within(withdraw).getByText(/antes de que el dispositivo llegue a recibirla/)).toBeInTheDocument();
    expect(within(withdraw).getByRole("button", { name: "Retirar la tarea" })).toBeInTheDocument();
    expect(screen.getByText(/Esta tarea aún no se ha enviado al dispositivo/)).toBeInTheDocument();
    expectNoEnglish(queued.container, STORED);
    queued.unmount();

    const running = renderPage([RUNNING], `/jobs?job=${RUNNING.id}`);
    await user.click(await screen.findByRole("button", { name: "Pedir al dispositivo que detenga esta tarea" }));
    const interrupt = await screen.findByRole("dialog", { name: "Pedir al dispositivo que detenga esta tarea" });
    expect(within(interrupt).getByRole("button", { name: "Enviar la instrucción de detener" })).toBeInTheDocument();
    expectNoEnglish(running.container, STORED);
  });

  it("words a cancelled job, a late cancel and a retry of an earlier attempt", async () => {
    const cancelled = renderPage(
      [{ ...FAILED, status: "cancelled", cancel_outcome: "interrupted", cancel_requested_at: "2026-08-20T09:15:00Z" }],
      `/jobs?job=${FAILED.id}`,
    );
    expect(await screen.findByText(/El equipo detuvo esta tarea mientras se ejecutaba/)).toBeInTheDocument();
    expectNoEnglish(cancelled.container, WITH_LOG);
    cancelled.unmount();

    const retried = renderPage([{ ...FAILED, retry_of_job_id: "66666666-6666-6666-6666-666666666666" } as Job], `/jobs?job=${FAILED.id}`);
    expect(await screen.findByText(/Esta tarea es un reintento de un intento anterior/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Mostrar el intento anterior" })).toBeInTheDocument();
    expectNoEnglish(retried.container, WITH_LOG);
  });

  it("says in Spanish that the job is loading", async () => {
    mockUseApi.mockReturnValue({
      me: vi.fn().mockResolvedValue(ME),
      listJobs: vi.fn().mockResolvedValue({ items: [BASE], total: 1 }),
      getJob: () => new Promise<never>(() => {}),
      listDevices: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    } as unknown as ApiClient);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { container } = renderInSpanish(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[`/jobs?job=${BASE.id}`]}>
          <JobsPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByText("Cargando la tarea…")).toBeInTheDocument();
    expectNoEnglish(container, STORED);
  });
});

const DEVICE = {
  id: DEVICE_ID,
  hostname: "ws-alpha",
  agent_status: "online",
  last_seen_at: "2026-08-20T09:00:00Z",
} as Device;

function renderDialog(device: Device = DEVICE, dispatchJob = vi.fn().mockResolvedValue({ ...BASE, status: "queued" })) {
  mockUseApi.mockReturnValue({ dispatchJob, me: vi.fn().mockResolvedValue(ME) } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <QueryClientProvider client={queryClient}>
      <DispatchJobDialog device={device} onClose={() => {}} />
    </QueryClientProvider>,
  );
}

describe("the dispatch dialog in Spanish", () => {
  it("words the form, the type hint, the required-field complaint and the confirmation", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const dispatchJob = vi.fn().mockResolvedValue({ ...BASE, status: "queued" });
    renderDialog(DEVICE, dispatchJob);
    const dialog = await screen.findByRole("dialog", { name: "Ejecutar tarea en ws-alpha" });
    expect(within(dialog).getByRole("option", { name: "Selecciona un tipo de tarea…" })).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "Recopilar inventario" })).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "Enviar registros del agente" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Enviar" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("El campo Tipo de tarea es obligatorio");
    await user.selectOptions(within(dialog).getByLabelText("Tipo de tarea"), "inventory.collect");
    expect(within(dialog).getByText(/Vuelve a leer el hardware, el software y las interfaces de red/)).toBeInTheDocument();
    expect(within(dialog).getByText("(inventory.collect)")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Enviar" }));
    const confirm = await screen.findByRole("dialog", { name: "Enviar esta tarea al dispositivo" });
    expect(within(confirm).getByText(/«Recopilar inventario» se ejecutará en este equipo como una tarea firmada\./)).toBeInTheDocument();
    expect(within(confirm).getByText(/Una tarea enviada no se puede cancelar desde la consola\./)).toBeInTheDocument();
    await user.click(within(confirm).getByRole("button", { name: "Enviar tarea" }));
    expect(await screen.findByText("Se envió «Recopilar inventario». Estado: en cola.")).toBeInTheDocument();
    expect(dispatchJob).toHaveBeenCalled();
    expectNoEnglish(dialog, STORED);
  });

  it("warns in Spanish about an offline agent and about an asset no job can reach", async () => {
    const offline = renderDialog({ ...DEVICE, agent_status: "offline" } as Device);
    expect(await screen.findByText(/Este agente está sin conexión \(visto por última vez el/)).toBeInTheDocument();
    expectNoEnglish(offline.container, STORED);
    offline.unmount();

    const asset = renderDialog({ ...DEVICE, agent_status: null, hostname: "", primary_ip: null, description: null, last_seen_at: null } as unknown as Device);
    expect(await screen.findByRole("dialog", { name: "Ejecutar tarea en este activo" })).toBeInTheDocument();
    expect(screen.getByText("Ningún agente se ha inscrito en este activo, así que una tarea firmada no puede llegar a él.")).toBeInTheDocument();
    expectNoEnglish(asset.container, STORED);
  });
});

describe("a live language switch", () => {
  it("re-words the job names, the ages and the filters on a mounted page", async () => {
    renderPage([BASE, STALE]);
    expect(await screen.findByText("Recopilar inventario")).toBeInTheDocument();
    expect(screen.getByText("hace 25 h")).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Collect inventory")).toBeInTheDocument();
    expect(screen.getByText("25h ago")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Waiting too long" })).toBeInTheDocument();
  });
});

describe("a refused dispatch form held across a language switch", () => {
  it("re-words the 'required' complaint on a mounted dialog", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderDialog(DEVICE, vi.fn());
    const dialog = await screen.findByRole("dialog", { name: "Ejecutar tarea en ws-alpha" });
    await user.click(within(dialog).getByRole("button", { name: "Enviar" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("El campo Tipo de tarea es obligatorio");
    act(() => setActiveLanguage("en"));
    expect(within(screen.getByRole("dialog")).getByRole("alert")).toHaveTextContent("Job type is required");
  });
});

describe("job words", () => {
  it("name every known job type in both languages, keeping the English the queue always showed", () => {
    const english = en.enums.JobType as Record<string, string>;
    const spanish = es.enums.JobType as Record<string, string>;
    expect(Object.keys(spanish).sort()).toEqual(Object.keys(english).sort());
    for (const type of KNOWN_JOB_TYPES) {
      const key = type.replace(/\./g, "_");
      expect(english[key], type).toBeTruthy();
      expect(spanish[key], type).toBeTruthy();
      setActiveLanguage("es");
      expect(jobTypeLabel(type)).toBe(spanish[key]);
      setActiveLanguage("en");
      expect(jobTypeLabel(type)).toBe(english[key]);
    }
    expect(KNOWN_JOB_TYPES.length).toBe(Object.keys(english).length);
  });

  it("keep an unknown job type as its wire name in Spanish too", () => {
    setActiveLanguage("es");
    expect(jobTypeLabel("module.invented.tomorrow")).toBe("module.invented.tomorrow");
  });

  it("describe the dispatchable types in Spanish", () => {
    setActiveLanguage("es");
    for (const entry of DISPATCHABLE_JOB_TYPES) {
      expect(entry.label).toBe(jobTypeLabel(entry.type));
      expect(entry.description).toBe(es.jobs.dispatchable[entry.type.replace(".", "_") as "logs_ship"]);
    }
  });

  it("read ages and spans in Spanish", () => {
    setActiveLanguage("es");
    const now = NOW.getTime();
    expect(describeAge(new Date(now - 10_000).toISOString(), now)).toBe("ahora mismo");
    expect(describeAge(new Date(now - 5 * 60_000).toISOString(), now)).toBe("hace 5 min");
    expect(describeAge(new Date(now - 3 * 3_600_000).toISOString(), now)).toBe("hace 3 h");
    expect(describeAge(new Date(now - 72 * 3_600_000).toISOString(), now)).toBe("hace 3 d");
    expect(describeSpan(10_000)).toBe("menos de un minuto");
    expect(describeSpan(12 * 60_000)).toBe("12 min");
    expect(typeBreakdown([{ type: "a" }, { type: "b" }, { type: "c" }, { type: "c" }, { type: "d" }])).toBe("2 c · 1 a · +2 más");
  });

  it("word every lifecycle sentence in Spanish", () => {
    setActiveLanguage("es");
    const lifecycle = { status: "cancelled" as const, expires_at: "x", cancel_requested_at: "y" };
    expect(describeCancellation({ ...lifecycle, cancel_outcome: "withdrawn" })).toBe("Retirada antes de enviarse al dispositivo.");
    expect(describeCancellation({ ...lifecycle, cancel_outcome: "dropped" })).toMatch(/descartó esta tarea/);
    expect(describeCancellation({ ...lifecycle, cancel_outcome: "other" })).toBe("Cancelada.");
    expect(describeLateCancel({ ...lifecycle, status: "succeeded" })).toMatch(/demasiado tarde/);
    expect(describeLateCancel({ ...lifecycle, status: "expired" })).toMatch(/nunca confirmó/);
    expect(describeCancelRequest({ ...lifecycle, status: "running" }, "ayer")).toBe("Cancelación solicitada el ayer. El equipo no la ha confirmado.");
    expect(describeCancelRequest({ ...lifecycle, status: "running", cancel_outcome: "unsupported" }, "ayer")).toMatch(/no es compatible/);
    expect(describeStop("withdraw").confirm).toBe("Retirar la tarea");
    expect(describeStop("interrupt").confirm).toBe("Enviar la instrucción de detener");
    expect(describeStopEffect("withdraw", "mañana")).toMatch(/caduca por sí sola el mañana/);
    expect(describeStopEffect("interrupt", "mañana")).toMatch(/caduca el mañana/);
    expect(describeRetry({ ...lifecycle }).body).toMatch(/^Alguien canceló esta tarea\./);
    expect(describeRetry({ ...lifecycle, status: "failed" }).body).toMatch(/^Esto no vuelve a enviar/);
    expect(whyNotRetryable({ ...lifecycle, status: "succeeded" })).toMatch(/se completó correctamente/);
    expect(whyNotRetryable({ ...lifecycle, status: "running" })).toMatch(/no ha terminado/);
    expect(canRetry({ ...lifecycle })).toBe(true);
    expect(whyNotStoppable({ ...lifecycle, cancel_requested_at: null, status: "failed" }, "hoy")).toMatch(/Esta tarea ha terminado/);
  });
});
