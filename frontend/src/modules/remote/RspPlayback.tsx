import { useEffect, useMemo, useRef, useState } from "react";
import { EmptyState } from "../../components/emptystate";
import { useT } from "../../i18n/context";
import { Btn, Panel } from "../../shell/components";
import { agentToViewer, decode, readRecording, type Monitor, type RecordedFrame } from "./rsp";
import { CanvasRenderer } from "./screenCanvas";
import { Assembler, type ScreenEvent } from "./screenModel";

function formatSeconds(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(total / 60)}:${(total % 60).toString().padStart(2, "0")}`;
}

/**
 * Plays an rsp/1 recording (`.rsp`, spec-screen-share.md §3.9): the frames the
 * technician saw, drawn by the same assembler and renderer as the live viewer,
 * at the times they arrived. Nothing here reaches a device.
 */
export function RspPlayback({ bytes }: { bytes: Uint8Array }) {
  const t = useT();
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const records = useMemo<RecordedFrame[] | null>(() => {
    try {
      return readRecording(bytes);
    } catch {
      return null;
    }
  }, [bytes]);
  const duration = records && records.length > 0 ? (records[records.length - 1]?.tMs ?? 0) : 0;
  const [position, setPosition] = useState(0);
  const [playing, setPlaying] = useState(false);
  const player = useRef<{ index: number; assembler: Assembler; renderer: CanvasRenderer | null }>({
    index: 0,
    assembler: new Assembler(),
    renderer: null,
  });

  /** Applies every record up to `t` ms, from wherever playback is now. */
  function advance(t: number) {
    const p = player.current;
    if (!records) return;
    while (p.index < records.length && (records[p.index]?.tMs ?? 0) <= t) {
      const record = records[p.index];
      p.index += 1;
      if (!record) continue;
      let event: ScreenEvent | null = null;
      try {
        const frame = decode(record.frame);
        if (agentToViewer(frame.type)) event = p.assembler.push(frame);
      } catch {
        continue;
      }
      if (event?.kind === "hello") {
        const m: Monitor | undefined =
          event.hello.monitors.find((x) => x.id === event.hello.selected) ??
          event.hello.monitors.find((x) => x.primary) ??
          event.hello.monitors[0];
        if (m) p.renderer?.resize(m.width, m.height);
      } else if (event?.kind === "frame") {
        void p.renderer?.apply(event.ops);
      }
    }
  }

  function restartAt(t: number) {
    const canvas = canvasRef.current;
    player.current = { index: 0, assembler: new Assembler(), renderer: canvas ? new CanvasRenderer(canvas) : null };
    advance(t);
    setPosition(t);
  }

  // The first frame as a poster, so a loaded recording is not a blank box.
  useEffect(() => {
    if (!records) return;
    const firstFrame = records.findIndex((r) => {
      try {
        return decode(r.frame).type === 0x05;
      } catch {
        return false;
      }
    });
    restartAt(firstFrame >= 0 ? (records[firstFrame]?.tMs ?? 0) : 0);
  }, [records]);

  useEffect(() => {
    if (!playing) return;
    const startedAt = performance.now() - position;
    let raf = 0;
    const tick = () => {
      const t = Math.min(performance.now() - startedAt, duration);
      advance(t);
      setPosition(t);
      if (t >= duration) {
        setPlaying(false);
        return;
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing]);

  if (!records) {
    return (
      <p role="alert" className="form-error">
        {t("remote.recording.rspUnreadable")}
      </p>
    );
  }

  return (
    <Panel
      title={t("remote.recording.panelTitle")}
      actions={
        <div className="flex items-center gap-3">
          <span className="mono cell-sub" aria-label={t("remote.recording.position")}>
            {formatSeconds(position)} / {formatSeconds(duration)}
          </span>
          <Btn
            label={
              playing
                ? t("remote.recording.pause")
                : position >= duration && duration > 0
                  ? t("remote.recording.replay")
                  : t("remote.recording.play")
            }
            icon={playing ? "player-pause" : "player-play"}
            disabled={records.length === 0}
            onClick={() => {
              if (playing) {
                setPlaying(false);
                return;
              }
              if (position >= duration) restartAt(0);
              setPlaying(true);
            }}
          />
        </div>
      }
    >
      {records.length === 0 ? (
        <EmptyState
          icon="player-play"
          title={t("remote.recording.emptyTitle")}
          body={t("remote.recording.rspEmptyBody")}
        />
      ) : (
        <>
          <input
            type="range"
            aria-label={t("remote.recording.seek")}
            min={0}
            max={duration}
            value={position}
            onChange={(event) => {
              setPlaying(false);
              restartAt(Number(event.target.value));
            }}
            style={{ width: "100%", marginBottom: 8 }}
          />
          <canvas
            ref={canvasRef}
            data-testid="rsp-canvas"
            style={{ width: "100%", height: "auto", display: "block", background: "#111" }}
          />
        </>
      )}
    </Panel>
  );
}
