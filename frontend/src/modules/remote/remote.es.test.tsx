/**
 * Remote sessions in Spanish: the sessions list with its tiles, tabs and
 * search, the Start session dialog (its refusals and both confirmations), the
 * live screen (the consent wait, the live view, every way a session ends), the
 * recording player (both recording formats), the readiness panel, and the
 * sentences the module builds outside React. Hostnames and protocol names read
 * as recorded; the words around them are Spanish.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import { ApiError } from "../../lib/api";
import type { ApiClient, Device, Me, Page, RemoteSession } from "../../lib/api";
import { ADD_COMPUTER_CTA } from "../../lib/detectors";
import { devicePickerOptions, credentialPickerOptions, unreachableReason } from "./pickerOptions";
import { RemoteReadiness } from "./RemoteReadiness";
import { T, encode } from "./rsp";
import { describeClose, describeEnd, INITIAL_STATUS, nextStatus } from "./screenModel";
import { consentModeLabel, formatDuration } from "./sessionState";
import { SessionsPage } from "./SessionsPage";
import { StartSessionDialog, UPDATE_AGENT_CTA } from "./StartSessionDialog";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
const mockAccessToken = vi.fn<() => string | null>(() => "test-token");
vi.mock("../../lib/auth", () => ({ accessToken: () => mockAccessToken() }));

// ---------------------------------------------------------------- the engine

class FakeDisplay {
  readonly element = document.createElement("div");
  getElement() {
    return this.element;
  }
  getWidth() {
    return 1024;
  }
  getHeight() {
    return 768;
  }
  scale() {}
  onresize: ((width: number, height: number) => void) | null = null;
}

type Scenario = "frames" | "empty" | "parse-error";
let scenario: Scenario = "frames";

class FakeSessionRecording {
  onplay: (() => void) | null = null;
  onpause: (() => void) | null = null;
  onseek: ((position: number) => void) | null = null;
  onload: (() => void) | null = null;
  onerror: ((message: string) => void) | null = null;
  onprogress: ((duration: number, parsedSize: number) => void) | null = null;
  private duration = 0;
  constructor(private readonly source: unknown) {}
  private readonly display = new FakeDisplay();
  getDisplay() {
    return this.display;
  }
  isPlaying() {
    return false;
  }
  getPosition() {
    return 0;
  }
  getDuration() {
    return this.duration;
  }
  play() {}
  pause() {}
  seek() {}
  connect() {
    (this.source as { connect?: () => void }).connect?.();
    this.onseek?.(0);
    if (scenario === "parse-error") {
      this.onerror?.("Illegal terminator.");
      return;
    }
    if (scenario === "frames") {
      this.duration = 60_000;
      this.onprogress?.(60_000, 4096);
    }
    this.onload?.();
  }
  disconnect() {}
}

class FakeParser {
  oninstruction: ((opcode: string, args: string[]) => void) | null = null;
  receive() {}
}

vi.mock("guacamole-common-js", () => ({
  default: { SessionRecording: FakeSessionRecording, Parser: FakeParser },
}));

/** A WebSocket the test drives. */
class FakeSocket {
  static OPEN = 1;
  static last: FakeSocket | null = null;
  readyState = 0;
  binaryType = "blob";
  onopen: (() => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onclose: ((e: CloseEvent) => void) | null = null;
  constructor(
    readonly url: string,
    readonly protocols?: string | string[],
  ) {
    FakeSocket.last = this;
  }
  send() {}
  close() {
    this.readyState = 3;
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
  json(type: number, seq: number, body: object) {
    this.onmessage?.(
      new MessageEvent("message", { data: encode(type, seq, new TextEncoder().encode(JSON.stringify(body))).slice().buffer }),
    );
  }
  shut(code: number, reason = "") {
    this.readyState = 3;
    this.onclose?.(new CloseEvent("close", { code, reason }));
  }
}

const { ScreenViewer } = await import("./ScreenViewer");
const { RecordingPlayer } = await import("./RecordingPlayer");

// ------------------------------------------------------------------ fixtures

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  permissions: ["devices.read", "remote.control", "remote.recordings.read"],
};

function device(overrides: Partial<Device> = {}): Device {
  return {
    id: "d1",
    hostname: "workstation-1",
    fqdn: null,
    os_family: "windows",
    os_version: "11",
    architecture: "x64",
    device_group_id: null,
    status: "active",
    agent_status: "online",
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

const ONLINE = device();
const OFFLINE = device({ id: "d2", hostname: "sentinel-2", os_family: "linux", agent_status: "offline" });
const OLD_AGENT = device({ id: "d3", hostname: "legacy-3", screen_capable: false });
const NO_AGENT = device({ id: "d4", hostname: "printer-4", agent_status: null });
const DEVICES = [ONLINE, OFFLINE, OLD_AGENT, NO_AGENT];

function session(overrides: Partial<RemoteSession> = {}): RemoteSession {
  return {
    id: "s1",
    device_id: "d1",
    technician_id: "u1",
    protocol: "screen",
    consent_mode: "attended",
    state: "active",
    credential_id: null,
    has_recording: false,
    started_at: "2026-01-01T00:00:00.000Z",
    ended_at: null,
    end_reason: null,
    created_at: "2026-01-01T00:00:00.000Z",
    input_enabled: true,
    ...overrides,
  };
}

const SESSIONS = [
  session(),
  session({ id: "s2", state: "awaiting_consent", started_at: null, consent_mode: "unattended" }),
  session({ id: "s3", state: "connecting", started_at: null }),
  session({
    id: "s4",
    state: "ended",
    has_recording: true,
    ended_at: "2026-01-01T00:02:05.000Z",
  }),
];

function renderApp(ui: ReactElement, entry = "/remote") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[entry]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

function stubApi(overrides: Record<string, unknown> = {}) {
  const api = {
    me: vi.fn().mockResolvedValue(ME),
    listRemoteSessions: vi.fn().mockResolvedValue({ items: SESSIONS, total: 4 } satisfies Page<RemoteSession>),
    listDevices: vi.fn().mockResolvedValue({ items: DEVICES, total: 4 } satisfies Page<Device>),
    startRemoteSession: vi.fn().mockResolvedValue({ session_id: "s9", state: "awaiting_consent", consent_mode: "attended" }),
    getRemoteSession: vi.fn().mockResolvedValue(session({ state: "awaiting_consent", started_at: null })),
    stopRemoteSession: vi.fn().mockResolvedValue(session({ state: "ended" })),
    getRemoteRecording: vi.fn().mockResolvedValue(new Blob(["x"])),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

beforeEach(() => {
  scenario = "frames";
  FakeSocket.last = null;
  vi.stubGlobal("WebSocket", FakeSocket);
  HTMLCanvasElement.prototype.getContext = vi.fn(() => null) as unknown as HTMLCanvasElement["getContext"];
  mockAccessToken.mockReturnValue("test-token");
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ------------------------------------------------------------ sessions page

describe("the sessions page, in Spanish", () => {
  it("words the head, the tiles, the tabs, the table and the row actions", async () => {
    stubApi();
    const { container } = renderApp(<SessionsPage />);
    expect(await screen.findByRole("heading", { name: "Sesiones remotas" })).toBeInTheDocument();
    await screen.findAllByText("workstation-1");
    expect(screen.getByRole("button", { name: "Iniciar sesión remota" })).toBeInTheDocument();
    expect(screen.getByText("Dispositivos en línea")).toBeInTheDocument();
    expect(screen.getByText("Flota, ahora mismo")).toBeInTheDocument();
    expect(screen.getByText("Activas ahora")).toBeInTheDocument();
    expect(screen.getByText("Grabadas")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Todas las sesiones/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Sesiones activas/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Con grabación/ })).toBeInTheDocument();
    expect(screen.getByRole("tabpanel", { name: "Todas las sesiones" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Buscar sesiones" })).toHaveAttribute(
      "placeholder",
      "Buscar por dispositivo, protocolo o estado",
    );
    for (const header of ["Dispositivo", "Consentimiento", "Estado", "Duración", "Grabación"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    // The state badge, the consent word, the duration and the recording column.
    expect(screen.getAllByText("Activa").length).toBeGreaterThan(0);
    expect(screen.getByText("Esperando consentimiento")).toBeInTheDocument();
    expect(screen.getByText("Conectando")).toBeInTheDocument();
    expect(screen.getByText("Finalizada")).toBeInTheDocument();
    expect(screen.getByText("Desatendida")).toBeInTheDocument();
    expect(screen.getAllByText("Asistida").length).toBeGreaterThan(0);
    expect(screen.getByText("2 min 05 s")).toBeInTheDocument();
    expect(screen.getByText("Grabada")).toBeInTheDocument();
    expect(screen.getAllByText("Sin grabar").length).toBeGreaterThan(0);
    expect(screen.getAllByRole("link", { name: "Abrir" }).length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: "Reproducir" })).toHaveAttribute("href", "/remote/s4/recording");
    expectNoEnglish(container);
  });

  it("says why a filter found nothing, and that there are no sessions at all", async () => {
    const user = userEvent.setup();
    stubApi();
    const { container } = renderApp(<SessionsPage />);
    await screen.findAllByText("workstation-1");
    await user.type(screen.getByRole("textbox", { name: "Buscar sesiones" }), "zzz");
    expect(await screen.findByText("Ninguna sesión coincide con ese filtro.")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words the empty page and the zero tiles", async () => {
    stubApi({
      listRemoteSessions: vi.fn().mockResolvedValue({ items: [], total: 0 }),
      listDevices: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    });
    const { container } = renderApp(<SessionsPage />);
    expect(await screen.findByText("Aún no hay sesiones remotas.")).toBeInTheDocument();
    expect(screen.getByText("Ningún agente está conectado")).toBeInTheDocument();
    expect(screen.getByText("Aún no se ha iniciado ninguna sesión remota")).toBeInTheDocument();
    expect(screen.getByText("No hay nadie conectado a un dispositivo")).toBeInTheDocument();
    expect(screen.getByText("Ninguna sesión de esta lista se grabó")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("re-words a mounted page when the language is switched", async () => {
    stubApi();
    renderApp(<SessionsPage />);
    await screen.findAllByText("workstation-1");
    expect(screen.getByRole("tabpanel", { name: "Todas las sesiones" })).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("heading", { name: "Remote sessions" })).toBeInTheDocument();
    expect(screen.getByRole("tabpanel", { name: "All sessions" })).toBeInTheDocument();
    expect(screen.getByText("Awaiting consent")).toBeInTheDocument();
    expect(screen.getByText("2m 05s")).toBeInTheDocument();
    expect(screen.getByText("Unattended")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Playback" })).toBeInTheDocument();
  });
});

// ------------------------------------------------------------- the readiness

describe("the readiness panel, in Spanish", () => {
  const fleet = (managed: number, online: number) => ({
    items: [],
    total: managed,
    state_counts: { all: 0, online: 0, offline: 0, assets: 0, certs: 0 },
    fleet: { total: managed, managed, online, cert_attention: 0 },
  });

  it("sends a fleet with no agent to add a computer", async () => {
    stubApi({ listDevices: vi.fn().mockResolvedValue(fleet(0, 0)) });
    const { container } = renderApp(<RemoteReadiness />);
    const item = (await screen.findByText("Hay un equipo con el agente conectado")).closest("li") as HTMLElement;
    expect(screen.getByText("Antes de poder iniciar una sesión")).toBeInTheDocument();
    expect(screen.getByText("Una sesión comparte la pantalla de un equipo cuyo agente está conectado ahora mismo.")).toBeInTheDocument();
    expect(within(item).getByRole("link", { name: ADD_COMPUTER_CTA.label })).toHaveAttribute("href", "/devices/onboard");
    expectNoEnglish(container);
  });

  it("re-words itself when the language is switched", async () => {
    stubApi({ listDevices: vi.fn().mockResolvedValue(fleet(3, 0)) });
    renderApp(<RemoteReadiness />);
    await screen.findByText("Hay un equipo con el agente conectado");
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("A computer with the agent is connected")).toBeInTheDocument();
    expect(screen.getByText("Before a session can start")).toBeInTheDocument();
  });
});

// ------------------------------------------------------------- start dialog

describe("the Start session dialog, in Spanish", () => {
  async function openDialog() {
    const api = stubApi();
    const view = renderApp(<StartSessionDialog open onClose={() => {}} />);
    const dialog = await screen.findByRole("dialog", { name: "Iniciar sesión remota" });
    await screen.findByRole("combobox", { name: "Dispositivo" });
    return { api, dialog, ...view };
  }

  async function pick(user: ReturnType<typeof userEvent.setup>, name: RegExp) {
    await user.click(screen.getByRole("combobox", { name: "Dispositivo" }));
    await user.click(await screen.findByRole("option", { name }));
  }

  it("words the form, the mode options and the recording note", async () => {
    const { container } = await openDialog();
    expect(screen.getByRole("option", { name: "Asistida (primero se pregunta a la persona que está en el dispositivo)" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Desatendida (sin solicitud de consentimiento)" })).toBeInTheDocument();
    expect(screen.getByLabelText("Motivo")).toHaveAttribute("placeholder", "p. ej., ticket de soporte 4821");
    expect(screen.getByText("La sesión se graba y este motivo se escribe en el registro de auditoría.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancelar" })).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("says what each device can answer in the picker", async () => {
    const user = userEvent.setup();
    await openDialog();
    await user.click(screen.getByRole("combobox", { name: "Dispositivo" }));
    expect(await screen.findByRole("option", { name: /workstation-1/ })).toHaveTextContent("windows · en línea");
    expect(screen.getByRole("option", { name: /sentinel-2/ })).toHaveTextContent("sin conexión · no se puede conectar");
    expect(screen.getByRole("option", { name: /legacy-3/ })).toHaveTextContent("agente demasiado antiguo para compartir su pantalla");
    expect(screen.getByRole("option", { name: /printer-4/ })).toHaveTextContent("sin agente · no se puede conectar");
  });

  it("names the fields a refused submission is missing, and re-words them on a switch", async () => {
    const user = userEvent.setup();
    const { api } = await openDialog();
    await user.click(screen.getByRole("button", { name: "Iniciar sesión remota" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("El campo Dispositivo es obligatorio");
    expect(alert).toHaveTextContent("El campo Motivo es obligatorio");
    expect(api.startRemoteSession).not.toHaveBeenCalled();
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent("Device is required");
    expect(screen.getByRole("alert")).toHaveTextContent("Reason is required");
  });

  it("refuses a reason too short to be an audit record, in Spanish", async () => {
    const user = userEvent.setup();
    await openDialog();
    await pick(user, /workstation-1/);
    await user.type(screen.getByLabelText("Motivo"), "x");
    await user.click(screen.getByRole("button", { name: "Iniciar sesión remota" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "El motivo se escribe en el registro de auditoría — indica al menos una referencia de ticket",
    );
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Reason is written to the audit log — give at least a ticket reference",
    );
  });

  it("warns about unattended access beside the mode and again in the confirmation", async () => {
    const user = userEvent.setup();
    const { api } = await openDialog();
    await pick(user, /workstation-1/);
    await user.type(screen.getByLabelText("Motivo"), "INC-4821");
    await user.selectOptions(screen.getByLabelText("Modo"), "unattended");
    expect(screen.getByRole("status")).toHaveTextContent("No se pide consentimiento a nadie en el dispositivo.");
    await user.click(screen.getByRole("button", { name: "Iniciar sesión remota" }));
    const confirm = await screen.findByRole("dialog", { name: "Iniciar una sesión desatendida" });
    expect(confirm).toHaveTextContent("El modo desatendido se conecta SIN pedir consentimiento a la persona que está en ese dispositivo.");
    expect(confirm).toHaveTextContent("El motivo que has indicado se escribe en el registro de auditoría inmutable junto con esta sesión.");
    expect(within(confirm).getByRole("button", { name: "Iniciar sesión remota desatendida" })).toBeInTheDocument();
    expectNoEnglish(confirm);
    expect(api.startRemoteSession).not.toHaveBeenCalled();
    // The confirmation follows a language switch.
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("dialog", { name: "Start an unattended session" })).toBeInTheDocument();
  });

  it("confirms before starting on a device that cannot answer", async () => {
    const user = userEvent.setup();
    await openDialog();
    await pick(user, /sentinel-2/);
    await user.type(screen.getByLabelText("Motivo"), "INC-4821");
    expect(screen.getAllByRole("status")[0]).toHaveTextContent(
      "sentinel-2 está sin conexión — el agente no responde, así que la conexión no se completará.",
    );
    await user.click(screen.getByRole("button", { name: "Iniciar sesión remota" }));
    const confirm = await screen.findByRole("dialog", { name: "Iniciar una sesión en un dispositivo que no puede responder" });
    expect(within(confirm).getByRole("button", { name: "Iniciar sesión remota de todos modos" })).toBeInTheDocument();
    expectNoEnglish(confirm);
  });

  it("explains an agent too old to share its screen, with the way to update it", async () => {
    const user = userEvent.setup();
    await openDialog();
    await pick(user, /legacy-3/);
    expect(
      await screen.findByText(
        "legacy-3 tiene un agente demasiado antiguo para compartir su pantalla — la sesión se rechaza hasta que se actualice el agente.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Actualizar el agente" })).toHaveAttribute(
      "href",
      "/agent-packages#fleet-versions",
    );
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("link", { name: "Update the agent" })).toBeInTheDocument();
  });

  it("explains an empty device list and links to adding a computer", async () => {
    stubApi({ listDevices: vi.fn().mockResolvedValue({ items: [], total: 0 }) });
    const { container } = renderApp(<StartSessionDialog open onClose={() => {}} />);
    expect(await screen.findByRole("link", { name: ADD_COMPUTER_CTA.label })).toHaveAttribute("href", "/devices/onboard");
    expectNoEnglish(container);
  });

  it("shows a refusal from the server in Spanish", async () => {
    const user = userEvent.setup();
    const { api } = await openDialog();
    api.startRemoteSession.mockRejectedValue(new ApiError(409, ""));
    await pick(user, /workstation-1/);
    await user.type(screen.getByLabelText("Motivo"), "INC-4821");
    await user.click(screen.getByRole("button", { name: "Iniciar sesión remota" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expectNoEnglish(screen.getByRole("dialog", { name: "Iniciar sesión remota" }));
  });
});

// ---------------------------------------------------------------- the screen

describe("the live screen, in Spanish", () => {
  function renderViewer(sessionOverrides: Partial<RemoteSession> = {}) {
    stubApi({
      getRemoteSession: vi.fn().mockResolvedValue(session({ state: "awaiting_consent", started_at: null, ...sessionOverrides })),
      listDevices: vi.fn().mockResolvedValue({ items: [device({ hostname: "laptop-7" })], total: 1 }),
    });
    return renderApp(
      <Routes>
        <Route path="/remote/:sessionId" element={<ScreenViewer />} />
        <Route path="/remote" element={<p>lista</p>} />
      </Routes>,
      "/remote/s1",
    );
  }

  const HELLO = {
    version: 1,
    capabilities: ["tile", "control", "text", "monitor_select", "quality", "refresh"],
    monitors: [
      { id: 0, x: 0, y: 0, width: 1920, height: 1080, dpi: 96, primary: true },
      { id: 1, x: 1920, y: 0, width: 1280, height: 1024, dpi: 96, primary: false },
    ],
    codecs: ["jpeg", "fill"],
    selected: 0,
  };

  function ws(): FakeSocket {
    if (!FakeSocket.last) throw new Error("no socket");
    return FakeSocket.last;
  }

  it("words the wait for the person at the device, with the countdown", async () => {
    const { container } = renderViewer();
    await screen.findByRole("heading", { name: "laptop-7" });
    expect(screen.getByText("Conectando", { selector: "h3, h2, p, div" })).toBeInTheDocument();
    await act(async () => {
      ws().open();
      ws().json(T.STATE, 1, { state: "consent_pending", timeout_s: 30 });
    });
    expect(await screen.findByText("Esperando a que la persona que está en el dispositivo permita esta sesión")).toBeInTheDocument();
    expect(screen.getByText("Tiene 30 segundos para responder. Si nadie responde, la solicitud se rechaza.")).toBeInTheDocument();
    expect(screen.getByText("Esperando el consentimiento")).toBeInTheDocument();
    expect(screen.getByText("La pantalla del dispositivo, en directo.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Finalizar sesión" })).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words the wait for the device and re-words it on a switch", async () => {
    renderViewer();
    await screen.findByRole("heading", { name: "laptop-7" });
    await act(async () => ws().open());
    expect(await screen.findByText("Esperando a laptop-7")).toBeInTheDocument();
    expect(screen.getByText("Se ha pedido al dispositivo que comparta su pantalla. La imagen aparece en cuanto empiece.")).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Waiting for laptop-7")).toBeInTheDocument();
    expect(screen.getByText("Waiting for the device…")).toBeInTheDocument();
  });

  it("words the live view: the controls, the monitor and quality pickers, the shortcuts", async () => {
    const user = userEvent.setup();
    const { container } = renderViewer({ state: "active" });
    await screen.findByRole("heading", { name: "laptop-7" });
    await act(async () => {
      ws().open();
      ws().json(T.STATE, 1, { state: "consent_pending", timeout_s: 30 });
      ws().json(T.STATE, 2, { state: "consent_granted" });
      ws().json(T.HELLO, 3, HELLO);
    });
    expect(await screen.findByText("Tienes el control")).toBeInTheDocument();
    expect(screen.getByText("En directo")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pasar a solo visualización" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Monitor" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Monitor 1 (1920×1080), principal" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Monitor 2 (1280×1024)" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Calidad de imagen" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Calidad: Automática" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pantalla completa" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Enviar Alt+Tab" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Texto que escribir en el dispositivo" })).toHaveAttribute(
      "placeholder",
      "Escribe texto en el dispositivo",
    );
    expect(screen.getByRole("group", { name: "Pantalla remota" })).toBeInTheDocument();
    expect(screen.getByText("Haz clic en la pantalla para enviar tu teclado al dispositivo.")).toBeInTheDocument();
    expectNoEnglish(container);
    await user.click(screen.getByRole("button", { name: "Pasar a solo visualización" }));
    expect(screen.getByText("Solo visualización")).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("View only")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Monitor 1 (1920×1080), main" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Quality: Automatic" })).toBeInTheDocument();
  });

  it("says when the person at the device took the control back, or the desktop cannot be shown", async () => {
    const { container } = renderViewer({ state: "active" });
    await screen.findByRole("heading", { name: "laptop-7" });
    await act(async () => {
      ws().open();
      ws().json(T.HELLO, 3, HELLO);
      ws().json(T.STATE, 4, { state: "input_desktop_unavailable" });
      ws().json(T.STATE, 5, { state: "control_revoked_by_user" });
    });
    expect(await screen.findByText("Recuperado por el usuario")).toBeInTheDocument();
    expect(
      screen.getByText(/La persona que está en el dispositivo recuperó el control\. Aún puedes mirar/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/se encuentra en una pantalla que esta sesión no puede mostrar, como un aviso de administrador/),
    ).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it.each([
    ["denied_consent", "La persona que está en el dispositivo rechazó la solicitud."],
    ["consent_timeout", "Nadie respondió a tiempo en el dispositivo, así que la solicitud se rechazó."],
    ["no_user_session", "Nadie ha iniciado sesión en el dispositivo, así que no hay pantalla que compartir."],
    ["capture_unavailable", "El dispositivo no pudo capturar su pantalla."],
    ["permission_missing", "No se ha dado permiso al dispositivo para compartir su pantalla."],
    ["unsupported_display", "La pantalla del dispositivo no se puede compartir."],
    ["relay_refused", "El dispositivo no pudo llegar al servidor de retransmisión."],
    ["expired", "La sesión alcanzó su límite de tiempo, o el dispositivo no se conectó a tiempo."],
    ["something_new", "La sesión finalizó por un error en el dispositivo."],
  ])("words the end reason %s", async (reason, sentence) => {
    const { container } = renderViewer();
    await screen.findByRole("heading", { name: "laptop-7" });
    await act(async () => {
      ws().open();
      ws().json(T.STATE, 1, { state: "ended", reason });
    });
    expect(await screen.findByText("La sesión ha finalizado")).toBeInTheDocument();
    expect(screen.getByText(sentence)).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("re-words the reason a session ended when the language is switched", async () => {
    renderViewer();
    await screen.findByRole("heading", { name: "laptop-7" });
    await act(async () => {
      ws().open();
      ws().json(T.STATE, 1, { state: "ended", reason: "ended_by_user" });
    });
    expect(await screen.findByText("La persona que está en el dispositivo finalizó la sesión.")).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("The person at the device ended the session.")).toBeInTheDocument();
    expect(screen.getByText("The session is over")).toBeInTheDocument();
  });

  it.each([
    [4403, "No tienes permiso para ver esta sesión, o tu inicio de sesión ha caducado."],
    [4404, "Esta sesión no existe."],
    [4408, "El dispositivo no se conectó a tiempo."],
    [4409, "Esta sesión no está en directo, o ya está abierta en otra ventana. Inicia una sesión nueva para volver a conectarte."],
    [1006, "Se perdió la conexión con la sesión."],
  ])("words the close code %s", async (code, sentence) => {
    const { container } = renderViewer();
    await screen.findByRole("heading", { name: "laptop-7" });
    await act(async () => {
      ws().open();
      ws().shut(code);
    });
    expect(await screen.findByText("No se mostró ninguna pantalla")).toBeInTheDocument();
    expect(screen.getByText(sentence)).toBeInTheDocument();
    expect(screen.getByText("No conectado")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("explains that no token means no screen, and re-words it on a switch", async () => {
    mockAccessToken.mockReturnValue(null);
    renderViewer();
    await screen.findByRole("heading", { name: "laptop-7" });
    expect(
      await screen.findByText(
        "Esta consola no tiene ningún token de acceso, así que no se abrió la pantalla y no se pidió nada al dispositivo. Volver a iniciar sesión lo restablece.",
      ),
    ).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(
      screen.getByText(/This console holds no access token, so the screen was not opened/),
    ).toBeInTheDocument();
  });

  it("asks before ending the session and words a refusal to end it", async () => {
    const user = userEvent.setup();
    const api = stubApi({
      getRemoteSession: vi.fn().mockResolvedValue(session({ state: "active" })),
      listDevices: vi.fn().mockResolvedValue({ items: [device({ hostname: "laptop-7" })], total: 1 }),
      stopRemoteSession: vi.fn().mockRejectedValue(new ApiError(503, "")),
    });
    renderApp(
      <Routes>
        <Route path="/remote/:sessionId" element={<ScreenViewer />} />
      </Routes>,
      "/remote/s1",
    );
    await screen.findByRole("heading", { name: "laptop-7" });
    await user.click(screen.getByRole("button", { name: "Finalizar sesión" }));
    const confirm = await screen.findByRole("dialog", { name: "Finalizar esta sesión remota" });
    expect(confirm).toHaveTextContent(
      "La conexión se cierra de inmediato y la persona que está en el dispositivo ve cómo finaliza la sesión.",
    );
    expectNoEnglish(confirm);
    await user.click(within(confirm).getByRole("button", { name: "Finalizar sesión" }));
    await waitFor(() => expect(api.stopRemoteSession).toHaveBeenCalled());
    expect(await screen.findByRole("alert")).toHaveTextContent("Esa sesión sigue en marcha — no se finalizó.");
  });
});

// ------------------------------------------------------------- the recording

describe("the recording player, in Spanish", () => {
  function renderPlayer() {
    return renderApp(
      <Routes>
        <Route path="/remote/:sessionId/recording" element={<RecordingPlayer />} />
      </Routes>,
      "/remote/s1/recording",
    );
  }

  it("words a recording that has frames", async () => {
    stubApi();
    const { container } = renderPlayer();
    await waitFor(() => expect(screen.getByRole("button", { name: "Reproducir" })).toBeEnabled());
    expect(screen.getByText("Grabación de la sesión")).toBeInTheDocument();
    expect(screen.getByText("Una grabación almacenada. Nada de lo que hay aquí llega al dispositivo.")).toBeInTheDocument();
    expect(screen.getByLabelText("Posición de reproducción")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Pantalla de la grabación" })).toBeInTheDocument();
    expectNoEnglish(container);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("button", { name: "Play" })).toBeInTheDocument();
    expect(screen.getByText("Session recording")).toBeInTheDocument();
  });

  it("words a recording with no frames", async () => {
    scenario = "empty";
    stubApi();
    const { container } = renderPlayer();
    expect(await screen.findByText("Esta grabación no contiene fotogramas")).toBeInTheDocument();
    expect(
      screen.getByText("El archivo se analizó sin problemas, pero nunca se dibujó nada en la pantalla remota."),
    ).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words a recording that could not be read", async () => {
    scenario = "parse-error";
    stubApi();
    const { container } = renderPlayer();
    expect(await screen.findByText("No se pudo reproducir nada")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("El reproductor no pudo leer esta grabación.");
    expect(screen.getByText("El motivo está en la consola del navegador, para un informe de error.")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words a recording that could not be fetched", async () => {
    stubApi({ getRemoteRecording: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    const { container } = renderPlayer();
    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo cargar esa grabación.");
    expectNoEnglish(container);
  });

  // A screen-session recording: "ITMSRSP1" then (t_ms, len, frame) records.
  function rspRecording(records: [number, Uint8Array][]): Blob {
    const out = new Uint8Array(8 + records.reduce((n, [, f]) => n + 8 + f.length, 0));
    out.set(new TextEncoder().encode("ITMSRSP1"));
    const v = new DataView(out.buffer);
    let at = 8;
    for (const [t, f] of records) {
      v.setUint32(at, t);
      v.setUint32(at + 4, f.length);
      out.set(f, at + 8);
      at += 8 + f.length;
    }
    return new Blob([out]);
  }

  it("words an rsp/1 recording with no frames", async () => {
    stubApi({ getRemoteRecording: vi.fn().mockResolvedValue(rspRecording([])) });
    const { container } = renderPlayer();
    expect(await screen.findByText("Esta grabación no contiene fotogramas")).toBeInTheDocument();
    expect(screen.getByText("La sesión finalizó antes de que se mostrara nada.")).toBeInTheDocument();
    expectNoEnglish(container);
  });
});

// ------------------------------------------- sentences built outside React

describe("the remote sentences built outside React, in Spanish", () => {
  beforeEach(() => setActiveLanguage("es"));

  it("words the consent mode and the duration", () => {
    expect(consentModeLabel("attended")).toBe("Asistida");
    expect(consentModeLabel("unattended")).toBe("Desatendida");
    expect(formatDuration("2026-01-01T00:00:00.000Z", "2026-01-01T00:02:05.000Z")).toBe("2 min 05 s");
    expect(formatDuration(null, null)).toBe("—");
    setActiveLanguage("en");
    expect(formatDuration("2026-01-01T00:00:00.000Z", "2026-01-01T00:02:05.000Z")).toBe("2m 05s");
  });

  it("words why a device cannot be reached", () => {
    expect(unreachableReason(NO_AGENT)).toBe("no tiene agente — una sesión remota necesita uno, así que no se puede establecer la conexión");
    expect(unreachableReason(OFFLINE)).toBe("está sin conexión — el agente no responde, así que la conexión no se completará");
    expect(unreachableReason(OLD_AGENT)).toContain("demasiado antiguo para compartir su pantalla");
    expect(unreachableReason(ONLINE)).toBeNull();
  });

  it("words the picker options", () => {
    expect(devicePickerOptions(DEVICES).map((o) => o.sublabel)).toEqual([
      "windows · en línea",
      "linux · sin conexión · no se puede conectar",
      "windows · en línea · agente demasiado antiguo para compartir su pantalla",
      "windows · sin agente · no se puede conectar",
    ]);
    expect(
      credentialPickerOptions([
        { id: "c1", name: "Soporte", type: "password", has_secret: false } as unknown as Parameters<typeof credentialPickerOptions>[0][number],
      ])[0]?.sublabel,
    ).toContain("sin secreto guardado");
  });

  it("words the end reasons and the close codes", () => {
    expect(describeEnd("ended_by_user")).toBe("La persona que está en el dispositivo finalizó la sesión.");
    expect(describeEnd(null)).toBe("La sesión finalizó por un error en el dispositivo.");
    expect(describeClose(4000, "")).toBe("La sesión ha finalizado.");
    expect(describeClose(4404, "")).toBe("Esta sesión no existe.");
    // The status carries the entry that words it, so a held status follows a switch.
    const ended = nextStatus(INITIAL_STATUS, { kind: "state", state: { state: "ended", reason: "expired" }, now: 0 });
    expect(ended.messageKey).toBe("remote.end.expired");
  });

  it("follows a language switch in the update-agent link", () => {
    expect(UPDATE_AGENT_CTA.label).toBe("Actualizar el agente");
    setActiveLanguage("en");
    expect(UPDATE_AGENT_CTA.label).toBe("Update the agent");
  });
});
