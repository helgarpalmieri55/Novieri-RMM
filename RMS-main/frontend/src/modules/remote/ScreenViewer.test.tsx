import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, Device, Page, RemoteSession } from "../../lib/api";
import { T, decode, encode, encodeKey, jsonBody, type Frame } from "./rsp";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
const mockAccessToken = vi.fn<() => string | null>(() => "test-token");
vi.mock("../../lib/auth", () => ({ accessToken: () => mockAccessToken() }));

/** A WebSocket the test drives: it records what the page sends. */
class FakeSocket {
  static OPEN = 1;
  static last: FakeSocket | null = null;
  readyState = 0;
  binaryType = "blob";
  sent: Frame[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onclose: ((e: CloseEvent) => void) | null = null;
  constructor(
    readonly url: string,
    readonly protocols?: string | string[],
  ) {
    FakeSocket.last = this;
  }
  send(data: Uint8Array) {
    this.sent.push(decode(data.slice()));
  }
  close() {
    this.readyState = 3;
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
  deliver(bytes: Uint8Array) {
    this.onmessage?.(new MessageEvent("message", { data: bytes.slice().buffer }));
  }
  json(type: number, seq: number, body: object) {
    this.deliver(encode(type, seq, new TextEncoder().encode(JSON.stringify(body))));
  }
  shut(code: number, reason = "") {
    this.readyState = 3;
    this.onclose?.(new CloseEvent("close", { code, reason }));
  }
  types(): number[] {
    return this.sent.map((f) => f.type);
  }
}

const { ScreenViewer } = await import("./ScreenViewer");

const SESSION: RemoteSession = {
  id: "s1",
  device_id: "d1",
  technician_id: "u1",
  protocol: "screen",
  consent_mode: "attended",
  state: "awaiting_consent",
  credential_id: null,
  has_recording: false,
  started_at: null,
  ended_at: null,
  end_reason: null,
  created_at: "2026-01-01T00:00:00.000Z",
  input_enabled: true,
};

const DEVICE = { id: "d1", hostname: "laptop-7", fqdn: null } as unknown as Device;

const HELLO = {
  version: 1,
  capabilities: ["tile", "copyrect", "cursor", "control", "text", "monitor_select", "quality", "refresh"],
  monitors: [
    { id: 0, x: 0, y: 0, width: 1920, height: 1080, dpi: 96, primary: true },
    { id: 1, x: 1920, y: 0, width: 1280, height: 1024, dpi: 96, primary: false },
  ],
  codecs: ["jpeg", "fill"],
  selected: 0,
};

function renderViewer(session: RemoteSession = SESSION) {
  const api: Partial<ApiClient> = {
    getRemoteSession: vi.fn().mockResolvedValue(session),
    listDevices: vi.fn().mockResolvedValue({ items: [DEVICE], total: 1 } as Page<Device>),
    stopRemoteSession: vi.fn().mockResolvedValue({ ...session, state: "ended" }),
  };
  mockUseApi.mockReturnValue(api);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/remote/s1"]}>
        <Routes>
          <Route path="/remote/:sessionId" element={<ScreenViewer />} />
          <Route path="/remote" element={<p>sessions list</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return api;
}

function socket(): FakeSocket {
  const ws = FakeSocket.last;
  if (!ws) throw new Error("no socket opened");
  return ws;
}

async function goLive(ws: FakeSocket) {
  await act(async () => {
    ws.open();
    ws.json(T.STATE, 1, { state: "consent_pending", timeout_s: 30 });
    ws.json(T.STATE, 2, { state: "consent_granted" });
    ws.json(T.HELLO, 3, HELLO);
  });
}

function box(el: HTMLElement) {
  el.getBoundingClientRect = () =>
    ({ left: 0, top: 0, width: 960, height: 540, right: 960, bottom: 540, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect;
}

describe("ScreenViewer", () => {
  beforeEach(() => {
    FakeSocket.last = null;
    vi.stubGlobal("WebSocket", FakeSocket);
    // jsdom has no canvas; the renderer draws nothing and still acknowledges.
    HTMLCanvasElement.prototype.getContext = vi.fn(() => null) as unknown as HTMLCanvasElement["getContext"];
    mockAccessToken.mockReturnValue("test-token");
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("opens the session's screen stream with the console's token", async () => {
    renderViewer();
    expect(socket().url).toBe(`ws://${window.location.host}/api/v1/remote/sessions/s1/screen`);
    // The token rides in the subprotocol header, never the URL an access log records.
    expect(socket().protocols).toEqual(["bearer", "test-token"]);
    expect(socket().binaryType).toBe("arraybuffer");
    expect(await screen.findByText("Connecting")).toBeInTheDocument();
  });

  it("says who it is waiting for and counts down the consent prompt", async () => {
    renderViewer();
    const ws = socket();
    await act(async () => {
      ws.open();
      ws.json(T.STATE, 1, { state: "consent_pending", timeout_s: 30 });
    });
    expect(await screen.findByText("Waiting for the person at the device to allow this session")).toBeInTheDocument();
    expect(screen.getByText(/They have 30 seconds to answer/)).toBeInTheDocument();
  });

  it("asks for the picture on HELLO and acknowledges every frame it draws", async () => {
    renderViewer();
    const ws = socket();
    await goLive(ws);
    expect(ws.types()).toEqual([T.REFRESH]);
    const canvas = screen.getByTestId("screen-canvas") as HTMLCanvasElement;
    expect([canvas.width, canvas.height]).toEqual([1920, 1080]);
    await act(async () => {
      ws.deliver(encode(T.FRAME_BEGIN, 4));
      ws.deliver(encode(T.TILE, 5, Uint8Array.of(0, 0, 0, 0, 0, 64, 0, 64, 0, 255, 0, 0)));
      ws.deliver(encode(T.FRAME_END, 6));
    });
    const ack = ws.sent.find((f) => f.type === T.ACK);
    expect(ack && new DataView(ack.body.buffer, ack.body.byteOffset).getUint32(0)).toBe(6);
  });

  it("sends the pointer in the remote monitor's pixels while in control", async () => {
    renderViewer();
    const ws = socket();
    await goLive(ws);
    const canvas = screen.getByTestId("screen-canvas");
    box(canvas);
    fireEvent.pointerDown(canvas, { clientX: 480, clientY: 270, buttons: 1, pointerId: 1 });
    const p = ws.sent.find((f) => f.type === T.POINTER);
    expect(p).toBeDefined();
    const v = new DataView(p!.body.buffer, p!.body.byteOffset);
    expect([v.getUint16(0), v.getUint16(2), v.getUint8(4)]).toEqual([960, 540, 1]);
  });

  it("sends physical keys only while the screen has focus, and lets go of them on blur", async () => {
    renderViewer();
    const ws = socket();
    await goLive(ws);
    const stage = screen.getByTestId("screen-stage");
    stage.focus();
    fireEvent.keyDown(stage, { code: "ShiftLeft", key: "Shift" });
    fireEvent.keyDown(stage, { code: "KeyA", key: "A" });
    fireEvent.keyUp(stage, { code: "KeyA", key: "A" });
    fireEvent.blur(stage);
    const keys = ws.sent.filter((f) => f.type === T.KEY).map((f) => [f.body[0], new TextDecoder().decode(f.body.subarray(1))]);
    expect(keys).toEqual([
      [1, "ShiftLeft"],
      [1, "KeyA"],
      [0, "KeyA"],
      [0, "ShiftLeft"],
    ]);
  });

  it("goes view only on request and then sends no input", async () => {
    const user = userEvent.setup();
    renderViewer();
    const ws = socket();
    await goLive(ws);
    await user.click(screen.getByRole("button", { name: /switch to view only/i }));
    const control = ws.sent.find((f) => f.type === T.CONTROL);
    expect(control?.body[0]).toBe(0);
    const stage = screen.getByTestId("screen-stage");
    fireEvent.keyDown(stage, { code: "KeyQ" });
    expect(ws.sent.some((f) => f.type === T.KEY)).toBe(false);
    expect(screen.getByText("View only")).toBeInTheDocument();
  });

  it("starts view only when the policy allows no input", async () => {
    renderViewer({ ...SESSION, input_enabled: false });
    const ws = socket();
    await goLive(ws);
    await screen.findByText("View only");
    fireEvent.keyDown(screen.getByTestId("screen-stage"), { code: "KeyQ" });
    expect(ws.sent.some((f) => f.type === T.KEY)).toBe(false);
  });

  it("tells the technician when the user took back control, for good", async () => {
    renderViewer();
    const ws = socket();
    await goLive(ws);
    await act(async () => ws.json(T.STATE, 9, { state: "control_revoked_by_user" }));
    expect(await screen.findByText(/took back control/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /take control/i })).toBeDisabled();
  });

  it("types text through TEXT frames", async () => {
    const user = userEvent.setup();
    renderViewer();
    const ws = socket();
    await goLive(ws);
    await user.type(screen.getByLabelText("Text to type on the device"), "héllo");
    await user.click(screen.getByRole("button", { name: "Type" }));
    const t = ws.sent.find((f) => f.type === T.TEXT);
    expect(t && new TextDecoder().decode(t.body)).toBe("héllo");
  });

  it("switches monitor and quality", async () => {
    const user = userEvent.setup();
    renderViewer();
    const ws = socket();
    await goLive(ws);
    await user.selectOptions(screen.getByLabelText("Monitor"), "1");
    await user.selectOptions(screen.getByLabelText("Picture quality"), "3");
    const ms = ws.sent.find((f) => f.type === T.MONITOR_SELECT);
    const q = ws.sent.find((f) => f.type === T.QUALITY);
    expect([ms?.body[0], q?.body[0]]).toEqual([1, 3]);
  });

  it("explains how the session ended", async () => {
    renderViewer();
    const ws = socket();
    await goLive(ws);
    await act(async () => {
      ws.json(T.STATE, 20, { state: "ended", reason: "ended_by_user" });
      ws.shut(4000, "ended_by_user");
    });
    expect(await screen.findByText("The person at the device ended the session.")).toBeInTheDocument();
  });

  it("explains a declined request", async () => {
    renderViewer();
    const ws = socket();
    await act(async () => {
      ws.open();
      ws.json(T.STATE, 1, { state: "consent_pending", timeout_s: 30 });
      ws.json(T.STATE, 2, { state: "consent_timeout" });
    });
    expect(
      await screen.findByText("Nobody at the device answered in time, so the request was declined."),
    ).toBeInTheDocument();
  });

  it("names a refused or already-open stream rather than blaming the device", async () => {
    renderViewer();
    await act(async () => socket().shut(4409));
    expect(await screen.findByText(/already open in another window/)).toBeInTheDocument();
  });

  it("opens no socket without a credential and says why", async () => {
    mockAccessToken.mockReturnValue(null);
    renderViewer();
    expect(FakeSocket.last).toBeNull();
    expect(await screen.findByText(/holds no access token/)).toBeInTheDocument();
  });

  it("drops frames that travel the wrong way", async () => {
    renderViewer();
    const ws = socket();
    await goLive(ws);
    await act(async () => ws.deliver(encodeKey(1, true, "KeyA")));
    expect(ws.types()).toEqual([T.REFRESH]);
    expect(jsonBody<{ version: number }>(decode(encode(T.HELLO, 1, new TextEncoder().encode("{\"version\":1}")))).version).toBe(1);
  });
});
