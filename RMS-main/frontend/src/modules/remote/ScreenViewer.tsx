import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useReducer, useRef, useState, type FormEvent } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ConfirmDialog } from "../../components/confirm";
import { EmptyState } from "../../components/emptystate";
import { describeSubmitError } from "../../components/form";
import { inputClass } from "../../components/ui";
import { FIXTURE_MODE } from "../../fixtures/mode";
import { useT } from "../../i18n/context";
import { translate, type MessageKey } from "../../i18n/core";
import { lazyLabels } from "../../i18n/lazy";
import { accessToken } from "../../lib/auth";
import { useApi } from "../../lib/ApiContext";
import { Btn, PageHead, Panel, StatusPill } from "../../shell/components";
import { Facts, type Kind } from "../../shell/viz";
import {
  agentToViewer,
  decode,
  encodeAck,
  encodeControl,
  encodeKey,
  encodeMonitorSelect,
  encodePointer,
  encodeQuality,
  encodeRefresh,
  encodeText,
  encodeWheel,
  type Bytes,
  type Hello,
  type Monitor,
  type QualityLevel,
} from "./rsp";
import { CanvasRenderer } from "./screenCanvas";
import { SHORTCUTS, HeldKeys, buttonsMask, sendableCode, textChunks, toRemote, wheelUnits } from "./screenInput";
import { Assembler, INITIAL_STATUS, nextStatus, type Phase, type ScreenEvent } from "./screenModel";
import { consentModeLabel } from "./sessionState";
import { canOpenTunnel, screenProtocols, screenUrl } from "./tunnel";

// Accessors over the catalogue: the label is read when it is shown, so a
// language switch re-words it.
const PHASE_LABEL: Readonly<Record<Phase, string>> = lazyLabels<Phase>({
  connecting: "remote.screen.phase.connecting",
  waiting: "remote.screen.phase.waiting",
  consent: "remote.screen.phase.consent",
  live: "remote.screen.phase.live",
  ended: "remote.screen.phase.ended",
  failed: "remote.screen.phase.failed",
});

const PHASE_KIND: Record<Phase, Kind> = {
  connecting: "run",
  waiting: "run",
  consent: "warn",
  live: "ok",
  ended: "mut",
  failed: "crit",
};

const QUALITY: { value: QualityLevel; label: MessageKey }[] = [
  { value: 0, label: "remote.screen.quality.auto" },
  { value: 1, label: "remote.screen.quality.low" },
  { value: 2, label: "remote.screen.quality.medium" },
  { value: 3, label: "remote.screen.quality.high" },
];

interface Cursor {
  x: number;
  y: number;
  visible: boolean;
}

interface CursorImage {
  url: string;
  hotX: number;
  hotY: number;
  w: number;
  h: number;
}

type KeyboardWithLock = Navigator & { keyboard?: { lock?: () => Promise<void>; unlock?: () => void } };

/**
 * The live screen of a device (spec-screen-share.md §5.2): rsp/1 frames from
 * `/api/v1/remote/sessions/{id}/screen` drawn on a canvas, and the
 * technician's pointer, wheel and keys sent back while in control. Every
 * permission and every input rule is enforced by the api, the gateway and the
 * agent; this page only reflects them.
 */
export function ScreenViewer() {
  const t = useT();
  const { sessionId } = useParams<{ sessionId: string }>();
  const api = useApi();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const stageRef = useRef<HTMLDivElement | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const seqRef = useRef(0);
  const rendererRef = useRef<CanvasRenderer | null>(null);
  const heldRef = useRef(new HeldKeys());
  const lastPointer = useRef("");
  const monitorRef = useRef<Monitor | null>(null);

  const [status, dispatch] = useReducer(nextStatus, INITIAL_STATUS);
  const [hello, setHello] = useState<Hello | null>(null);
  const [control, setControl] = useState(true);
  const [quality, setQuality] = useState<QualityLevel>(0);
  const [cursor, setCursor] = useState<Cursor | null>(null);
  const [cursorImage, setCursorImage] = useState<CursorImage | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [text, setText] = useState("");
  const [focused, setFocused] = useState(false);
  const [confirmingEnd, setConfirmingEnd] = useState(false);

  const session = useQuery({
    queryKey: ["remote-sessions", sessionId],
    queryFn: () => api.getRemoteSession(sessionId as string),
    enabled: sessionId != null,
  });
  const devices = useQuery({ queryKey: ["devices"], queryFn: () => api.listDevices() });
  const device = devices.data?.items.find((item) => item.id === session.data?.device_id);
  const deviceName = device?.hostname ?? device?.fqdn ?? null;

  const stop = useMutation({
    mutationFn: () => api.stopRemoteSession(sessionId as string),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["remote-sessions"] });
      navigate("/remote");
    },
  });

  // The server's word on control, once: policy allow_input at start, false
  // for good after the person at the device pressed Stop control.
  const inputEnabled = session.data?.input_enabled;
  useEffect(() => {
    if (inputEnabled === false) setControl(false);
  }, [inputEnabled]);

  const send = useCallback((build: (seq: number) => Bytes) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    seqRef.current += 1;
    try {
      ws.send(build(seqRef.current));
    } catch {
      // A frame the codec refuses (an oversized key code) is not sent.
    }
  }, []);

  const releaseKeys = useCallback(() => {
    for (const code of heldRef.current.releaseAll()) send((seq) => encodeKey(seq, false, code));
  }, [send]);

  const monitor =
    hello?.monitors.find((m) => m.id === hello.selected) ??
    hello?.monitors.find((m) => m.primary) ??
    hello?.monitors[0] ??
    null;
  monitorRef.current = monitor;

  const canControl =
    status.phase === "live" &&
    control &&
    !status.controlTaken &&
    (hello?.capabilities.includes("control") ?? false);

  useEffect(() => {
    if (!sessionId) return;
    // Under `?__fixtures` there is no api to stream from; the page holds the
    // state it would hold before the device answers.
    if (FIXTURE_MODE) {
      dispatch({ kind: "open" });
      return;
    }
    const token = accessToken();
    if (!canOpenTunnel(token)) {
      dispatch({ kind: "failed", messageKey: "remote.screen.noCredential" });
      return;
    }
    const canvas = canvasRef.current;
    if (canvas) rendererRef.current = new CanvasRenderer(canvas);
    const assembler = new Assembler();
    const ws = new WebSocket(screenUrl(sessionId, window.location), screenProtocols(token));
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;
    seqRef.current = 0;

    const handle = (event: ScreenEvent) => {
      switch (event.kind) {
        case "hello": {
          setHello(event.hello);
          const m =
            event.hello.monitors.find((x) => x.id === event.hello.selected) ??
            event.hello.monitors.find((x) => x.primary) ??
            event.hello.monitors[0];
          if (m) rendererRef.current?.resize(m.width, m.height);
          dispatch({ kind: "hello" });
          // Frames start when the viewer asks: it may have attached late.
          send((seq) => encodeRefresh(seq));
          break;
        }
        case "state":
          dispatch({ kind: "state", state: event.state, now: Date.now() });
          if (event.state.state === "control_revoked_by_user") {
            heldRef.current.releaseAll();
            setControl(false);
          }
          break;
        case "frame": {
          const seq = event.seq;
          const done = rendererRef.current?.apply(event.ops) ?? Promise.resolve();
          void done.then(() => send((s) => encodeAck(s, seq)));
          dispatch({ kind: "frame" });
          break;
        }
        case "cursor":
          setCursor({ x: event.x, y: event.y, visible: event.visible });
          break;
        case "cursor-shape": {
          const url = URL.createObjectURL(new Blob([new Uint8Array(event.png)], { type: "image/png" }));
          const image = new Image();
          image.onload = () =>
            setCursorImage((previous) => {
              if (previous) URL.revokeObjectURL(previous.url);
              return { url, hotX: event.hotX, hotY: event.hotY, w: image.naturalWidth, h: image.naturalHeight };
            });
          image.src = url;
          break;
        }
      }
    };

    ws.onopen = () => dispatch({ kind: "open" });
    ws.onmessage = (message: MessageEvent) => {
      if (!(message.data instanceof ArrayBuffer)) return;
      try {
        const frame = decode(message.data);
        if (!agentToViewer(frame.type)) return;
        const event = assembler.push(frame);
        if (event) handle(event);
      } catch {
        // A malformed frame is dropped; the next full frame repairs the picture.
      }
    };
    ws.onclose = (event: CloseEvent) => dispatch({ kind: "closed", code: event.code, reason: event.reason });

    return () => {
      ws.onopen = null;
      ws.onmessage = null;
      ws.onclose = null;
      ws.close();
      wsRef.current = null;
      rendererRef.current = null;
    };
  }, [sessionId, send]);

  // The consent countdown.
  useEffect(() => {
    if (status.phase !== "consent") return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(timer);
  }, [status.phase]);

  // Leaving control or losing the session lets go of every key.
  useEffect(() => {
    if (!canControl) releaseKeys();
  }, [canControl, releaseKeys]);

  // Wheel needs a non-passive listener to keep the page from scrolling.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !canControl) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const dx = wheelUnits(event.deltaX, event.deltaMode);
      const dy = wheelUnits(event.deltaY, event.deltaMode);
      if (dx !== 0 || dy !== 0) send((seq) => encodeWheel(seq, dx, dy));
    };
    canvas.addEventListener("wheel", onWheel, { passive: false });
    return () => canvas.removeEventListener("wheel", onWheel);
  }, [canControl, send]);

  useEffect(() => () => {
    if (cursorImage) URL.revokeObjectURL(cursorImage.url);
  }, [cursorImage]);

  function pointer(event: React.PointerEvent<HTMLCanvasElement>) {
    const m = monitorRef.current;
    if (!canControl || !m) return;
    const { x, y } = toRemote(event.clientX, event.clientY, event.currentTarget.getBoundingClientRect(), m);
    const buttons = buttonsMask(event.buttons);
    const key = `${x},${y},${buttons}`;
    if (key === lastPointer.current) return;
    lastPointer.current = key;
    send((seq) => encodePointer(seq, x, y, buttons));
  }

  function keyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (!canControl || event.target !== stageRef.current || !sendableCode(event.code)) return;
    event.preventDefault();
    heldRef.current.down(event.code);
    send((seq) => encodeKey(seq, true, event.code));
  }

  function keyUp(event: React.KeyboardEvent<HTMLDivElement>) {
    if (!canControl || !heldRef.current.up(event.code)) return;
    event.preventDefault();
    send((seq) => encodeKey(seq, false, event.code));
  }

  function toggleControl() {
    const next = !control;
    if (!next) releaseKeys();
    setControl(next);
    send((seq) => encodeControl(seq, next));
  }

  function sendShortcut(name: string) {
    const shortcut = SHORTCUTS[name];
    if (!canControl || !shortcut) return;
    for (const [code, down] of shortcut.keys) send((seq) => encodeKey(seq, down, code));
    stageRef.current?.focus();
  }

  function typeText(event: FormEvent) {
    event.preventDefault();
    if (!canControl || text === "") return;
    for (const chunk of textChunks(text)) send((seq) => encodeText(seq, chunk));
    setText("");
  }

  async function fullscreen() {
    const stage = stageRef.current;
    if (!stage) return;
    if (document.fullscreenElement) {
      await document.exitFullscreen();
      return;
    }
    await stage.requestFullscreen();
    // Chromium's Keyboard Lock sends Win, Alt+Tab and Esc to the device.
    await (navigator as KeyboardWithLock).keyboard?.lock?.();
    stage.focus();
  }

  const consentLeft = status.consentDeadline ? Math.max(0, Math.ceil((status.consentDeadline - now) / 1000)) : null;
  const placeholder = placeholderFor(status.phase, status.messageKey, deviceName, consentLeft);
  const controlLabel = status.controlTaken
    ? t("remote.screen.controlTaken")
    : canControl
      ? t("remote.screen.controlYou")
      : t("remote.screen.controlViewOnly");

  return (
    <>
      <PageHead
        title={deviceName ?? t("remote.screen.titleFallback")}
        description={t("remote.screen.description")}
        actions={
          <Btn
            label={stop.isPending ? t("remote.screen.ending") : t("remote.screen.end")}
            icon="x"
            variant="danger"
            onClick={() => setConfirmingEnd(true)}
            disabled={stop.isPending || session.data?.state === "ended"}
          />
        }
      />

      <Facts
        items={[
          {
            k: t("remote.screen.factConnection"),
            v: <StatusPill value={status.phase} kind={PHASE_KIND[status.phase]} label={PHASE_LABEL[status.phase]} />,
          },
          {
            k: t("remote.screen.factConsent"),
            v: session.data ? (
              <StatusPill
                value={session.data.consent_mode}
                kind={session.data.consent_mode === "unattended" ? "warn" : "mut"}
                label={consentModeLabel(session.data.consent_mode)}
              />
            ) : (
              t("remote.screen.na")
            ),
          },
          { k: t("remote.screen.factControl"), v: controlLabel },
          { k: t("remote.screen.factDevice"), v: deviceName ?? t("remote.screen.na") },
        ]}
      />

      {stop.isError && (
        <p role="alert" className="form-error" style={{ marginBottom: 8 }}>
          {t("remote.screen.stopFailed", { detail: describeSubmitError(stop.error) })}
        </p>
      )}
      {status.controlTaken && status.phase === "live" && (
        <p role="status" className="form-hint" style={{ marginBottom: 8 }}>
          {t("remote.screen.controlTakenNote")}
        </p>
      )}

      <Panel
        title={t("remote.screen.panelTitle")}
        actions={
          status.phase === "live" ? (
            <div className="flex flex-wrap items-center gap-2">
              <Btn
                label={control && !status.controlTaken ? t("remote.screen.switchToView") : t("remote.screen.takeControl")}
                icon={control && !status.controlTaken ? "eye" : "hand-click"}
                small
                pressed={canControl}
                disabled={status.controlTaken || !(hello?.capabilities.includes("control") ?? false)}
                onClick={toggleControl}
              />
              {hello && hello.monitors.length > 1 ? (
                <select
                  className={inputClass}
                  aria-label={t("remote.screen.monitorLabel")}
                  value={monitor?.id ?? 0}
                  onChange={(event) => send((seq) => encodeMonitorSelect(seq, Number(event.target.value)))}
                >
                  {hello.monitors.map((m) => (
                    <option key={m.id} value={m.id}>
                      {t(m.primary ? "remote.screen.monitorOptionMain" : "remote.screen.monitorOption", {
                        number: m.id + 1,
                        width: m.width,
                        height: m.height,
                      })}
                    </option>
                  ))}
                </select>
              ) : null}
              <select
                className={inputClass}
                aria-label={t("remote.screen.qualityLabel")}
                value={quality}
                onChange={(event) => {
                  const level = Number(event.target.value) as QualityLevel;
                  setQuality(level);
                  send((seq) => encodeQuality(seq, level));
                }}
              >
                {QUALITY.map((q) => (
                  <option key={q.value} value={q.value}>
                    {t("remote.screen.qualityOption", { label: t(q.label) })}
                  </option>
                ))}
              </select>
              <Btn label={t("remote.screen.refresh")} icon="refresh" small onClick={() => send((seq) => encodeRefresh(seq))} />
              <Btn label={t("remote.screen.fullScreen")} icon="device-desktop" small onClick={() => void fullscreen()} />
            </div>
          ) : null
        }
      >
        {placeholder ? <EmptyState icon={placeholder.icon} title={placeholder.title} body={placeholder.body} /> : null}
        {status.phase === "live" && status.desktopHidden ? (
          <p role="status" className="form-hint">
            {t("remote.screen.desktopHidden")}
          </p>
        ) : null}
        {status.phase === "live" && canControl && !focused ? (
          <p className="form-hint">{t("remote.screen.clickToFocus")}</p>
        ) : null}

        {/* ALWAYS MOUNTED: the renderer draws into this canvas from the
            first frame, and unmounting it would drop the picture. */}
        <div
          ref={stageRef}
          data-testid="screen-stage"
          role="group"
          aria-label={t("remote.screen.stageLabel")}
          tabIndex={0}
          onKeyDown={keyDown}
          onKeyUp={keyUp}
          onFocus={() => setFocused(true)}
          onBlur={() => {
            setFocused(false);
            releaseKeys();
          }}
          style={{
            position: "relative",
            display: status.phase === "live" ? "block" : "none",
            outline: focused && canControl ? "2px solid var(--accent, #2563eb)" : undefined,
            background: "#111",
            lineHeight: 0,
          }}
        >
          <canvas
            ref={canvasRef}
            data-testid="screen-canvas"
            style={{ width: "100%", height: "auto", display: "block", touchAction: "none" }}
            onPointerDown={(event) => {
              stageRef.current?.focus();
              if (canControl) {
                event.preventDefault();
                event.currentTarget.setPointerCapture?.(event.pointerId);
              }
              pointer(event);
            }}
            onPointerMove={pointer}
            onPointerUp={pointer}
            onContextMenu={(event) => {
              if (canControl) event.preventDefault();
            }}
          />
          {cursor?.visible && monitor && cursorImage ? (
            <img
              alt=""
              src={cursorImage.url}
              style={{
                position: "absolute",
                pointerEvents: "none",
                left: `${((cursor.x - cursorImage.hotX) / monitor.width) * 100}%`,
                top: `${((cursor.y - cursorImage.hotY) / monitor.height) * 100}%`,
                width: `${(cursorImage.w / monitor.width) * 100}%`,
              }}
            />
          ) : null}
        </div>

        {status.phase === "live" && canControl ? (
          <div className="mt-3 flex flex-wrap items-center gap-2">
            {Object.entries(SHORTCUTS).map(([name, s]) => (
              <Btn key={name} label={t("remote.screen.sendShortcut", { label: s.label })} small onClick={() => sendShortcut(name)} />
            ))}
            <form onSubmit={typeText} className="flex flex-wrap items-center gap-2">
              <input
                className={inputClass}
                aria-label={t("remote.screen.textLabel")}
                placeholder={t("remote.screen.textPlaceholder")}
                value={text}
                onChange={(event) => setText(event.target.value)}
              />
              <Btn label={t("remote.screen.type")} type="submit" small disabled={text === ""} />
            </form>
          </div>
        ) : null}
      </Panel>

      <ConfirmDialog
        open={confirmingEnd}
        title={t("remote.screen.endTitle")}
        subject={deviceName ?? t("remote.screen.endSubject")}
        body={t("remote.screen.endBody")}
        confirmLabel={t("remote.screen.end")}
        destructive
        onCancel={() => setConfirmingEnd(false)}
        onConfirm={() => {
          setConfirmingEnd(false);
          stop.mutate();
        }}
      />
    </>
  );
}

function placeholderFor(
  phase: Phase,
  messageKey: MessageKey | null,
  deviceName: string | null,
  consentLeft: number | null,
): { icon: string; title: string; body: string } | null {
  switch (phase) {
    case "connecting":
      return {
        icon: "progress",
        title: translate("remote.screen.connectingTitle"),
        body: translate("remote.screen.connectingBody"),
      };
    case "waiting":
      return {
        icon: "clock-hour-4",
        title:
          deviceName != null
            ? translate("remote.screen.waitingForName", { name: deviceName })
            : translate("remote.screen.waitingForDevice"),
        body: translate("remote.screen.waitingBody"),
      };
    case "consent":
      return {
        icon: "clock-hour-4",
        title: translate("remote.screen.consentTitle"),
        body:
          consentLeft != null
            ? translate("remote.screen.consentBody", { count: consentLeft })
            : translate("remote.screen.consentBodyNoTimer"),
      };
    case "ended":
      return {
        icon: "device-desktop",
        title: translate("remote.screen.endedTitle"),
        body: translate(messageKey ?? "remote.screen.endedFallback"),
      };
    case "failed":
      return {
        icon: "circle-x",
        title: translate("remote.screen.failedTitle"),
        body: translate(messageKey ?? "remote.screen.failedFallback"),
      };
    case "live":
      return null;
  }
}
