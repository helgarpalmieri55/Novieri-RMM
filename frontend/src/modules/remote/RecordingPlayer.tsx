import { useQuery } from "@tanstack/react-query";
import Guacamole from "guacamole-common-js";
import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { EmptyState } from "../../components/emptystate";
import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import { useApi } from "../../lib/ApiContext";
import { Btn, PageHead, Panel } from "../../shell/components";
import { recordingSource } from "./recordingSource";
import { isRspRecording } from "./rsp";
import { RspPlayback } from "./RspPlayback";

function formatSeconds(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${seconds.toString().padStart(2, "0")}`;
}

/**
 * The player. `onload` / `onerror` / `onprogress` used to be spliced on here,
 * because the package's ambient typing did not carry them; they are on
 * `Guacamole.SessionRecording` in `src/types/` now, which is where a
 * library's own members belong. The alias stays for the call sites.
 */
type Playable = Guacamole.SessionRecording;

/**
 * `Display.onresize` EXISTS AT RUNTIME AND NOT IN THE AMBIENT TYPING.
 * `guacamole-common.js` fires it from `Display.resize()` the moment the
 * archived stream declares the remote screen's real dimensions -- which is
 * the only moment this page can know what it has to fit. The ambient file in
 * `src/types/` carries the members the two remote pages call, and this widens
 * it at the one call site rather than by hand-editing a shared declaration.
 */
type SizedDisplay = Guacamole.Display & {
  onresize: ((width: number, height: number) => void) | null;
};

/**
 * THE STAGE IS FLUID, AND THE RECORDING KEEPS ITS SHAPE INSIDE IT.
 *
 * `SessionRecording` draws at the remote screen's intrinsic size -- 1024x768
 * for the archives this console holds -- and the element it hands back is a
 * div with that many pixels of width hard-coded on it. At 390 that is wider
 * than the card it sits in: the review found the stage cut off by the card's
 * right edge with the picture running off-screen mid-frame, which on a
 * handset reads as a rendering failure rather than as a player. At 1440 the
 * same fixed 1024 sat off-centre in an 1140px card, 20px of gap on one side
 * and 96px on the other.
 *
 * `Display.scale()` is the library's own answer: it sets a CSS transform on
 * the drawing and resizes the box around it, so ONE factor on both axes keeps
 * the aspect ratio exactly. The factor is the container's width over the
 * recording's own, and it is CAPPED AT 1 -- a 1024px recording blown up to
 * fill 1140px of card is a blurred picture of the same frame, and the review
 * asked for letterboxed and centred, not stretched. `margin: 0 auto` on the
 * library's element does the centring; below the recording's own width there
 * is nothing left over to centre.
 */
function fitToBox(box: HTMLElement, display: Guacamole.Display): void {
  const available = box.clientWidth;
  const intrinsic = display.getWidth();
  // Either can be zero and both mean "not yet": no layout (jsdom, a hidden
  // panel) and no frame parsed. Dividing by nothing would scale the picture
  // to nothing, so the drawing keeps the size it has until there is an
  // answer -- `onresize` and the observer below both fire again.
  if (available <= 0 || intrinsic <= 0) return;
  display.scale(Math.min(1, available / intrinsic));
}

/** What the panel holds while there is no picture in it. */
type Playback = "loading" | "empty" | "failed" | "ready";

// Plays back a stored recording (remote.recordings.read) via
// Guacamole.SessionRecording, which parses the Guacamole protocol out of the
// archived file — no live tunnel to any device is involved.
//
// NO v3 SCREEN EXISTS FOR THIS ROUTE, so it is the kit's head over one panel:
// the position in mono and the one transport control in the action row.
//
// HOW THE RECORDING REACHES THE PLAYER, and why it is not simply the Blob:
// `guacamole-common-js` 1.5.0 cannot read a Blob source (its `recordingBlob`
// is declared and never assigned, so the constructor dereferences
// `undefined.size` and throws). This console printed that `TypeError` to the
// operator, under a panel that then stayed empty — engine text on a console
// screen, over the one thing the route exists to show. `recordingSource`
// hands the file over the tunnel path the same constructor implements
// properly, so an archived recording now actually plays, and the states below
// are real states of a recording rather than the library failing.
export function RecordingPlayer() {
  const t = useT();
  const { sessionId } = useParams<{ sessionId: string }>();
  const api = useApi();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const recordingRef = useRef<Playable | null>(null);
  const [playing, setPlaying] = useState(false);
  const [position, setPosition] = useState(0);
  const [duration, setDuration] = useState(0);
  // `playback` IS STATE, NOT A REF, and that is deliberate: the transport
  // control's enabled/disabled reading is derived from it, and a ref would
  // leave the button describing whatever was true at first render.
  const [playback, setPlayback] = useState<Playback>("loading");
  // Screen sessions record rsp/1 (`.rsp`); sessions from before guacd was
  // retired recorded Guacamole (`.guac`). The file says which by its magic.
  const [format, setFormat] = useState<"unknown" | "rsp" | "guac">("unknown");
  const [rspBytes, setRspBytes] = useState<Uint8Array | null>(null);

  const recording = useQuery({
    queryKey: ["remote-recording", sessionId],
    queryFn: () => api.getRemoteRecording(sessionId as string),
    enabled: sessionId != null,
  });

  useEffect(() => {
    if (!recording.data) return;
    let cancelled = false;
    setFormat("unknown");
    recording.data
      .slice(0, 8)
      .arrayBuffer()
      .then((head) => {
        if (cancelled || !recording.data) return;
        if (!isRspRecording(new Uint8Array(head))) {
          setFormat("guac");
          return;
        }
        return recording.data.arrayBuffer().then((all) => {
          if (cancelled) return;
          setRspBytes(new Uint8Array(all));
          setFormat("rsp");
        });
      })
      .catch(() => {
        if (!cancelled) setFormat("guac");
      });
    return () => {
      cancelled = true;
    };
  }, [recording.data]);

  useEffect(() => {
    if (format !== "guac" || !recording.data || !containerRef.current) return;

    setPlayback("loading");
    setPosition(0);
    setDuration(0);

    const source = recordingSource(recording.data);
    let sessionRecording: Playable;
    try {
      sessionRecording = new Guacamole.SessionRecording(source) as Playable;
    } catch (error) {
      // Not reachable through the tunnel path in 1.5.0, and kept anyway: a
      // constructor that throws must not blank the route, which is exactly
      // what it did before the guard existed.
      console.error("Guacamole.SessionRecording refused the recording", error);
      setPlayback("failed");
      return;
    }
    recordingRef.current = sessionRecording;

    const box = containerRef.current;
    const stage = sessionRecording.getDisplay();
    const display = stage.getElement();
    // The centring half of `fitToBox`. Set here rather than in a stylesheet
    // because the element is the library's, not this file's -- it has no
    // class to hang a rule on.
    display.style.margin = "0 auto";
    box.appendChild(display);

    const fit = () => fitToBox(box, stage);
    // THREE MOMENTS CHANGE THE ANSWER, and all three have to re-ask it: the
    // recording declaring its screen size (`onresize`, fired from the parse),
    // the card changing width (rotation, a resized window), and mounting.
    const sized = stage as SizedDisplay;
    sized.onresize = fit;
    // `ResizeObserver` is undefined in some test environments; fitting is
    // still correct without it, so its absence must not throw.
    const observer = typeof ResizeObserver === "function" ? new ResizeObserver(fit) : null;
    observer?.observe(box);
    fit();

    sessionRecording.onplay = () => setPlaying(true);
    sessionRecording.onpause = () => setPlaying(false);
    sessionRecording.onseek = (newPosition) => {
      setPosition(newPosition);
      setDuration(sessionRecording.getDuration());
    };
    // Frames arrive as the file is parsed; the transport control unlocks as
    // soon as there is something to play rather than at the end of the file.
    sessionRecording.onprogress = (loadedDuration) => {
      setDuration(loadedDuration);
      if (loadedDuration > 0) setPlayback("ready");
    };
    sessionRecording.onload = () => {
      // A RECORDING WITH NO FRAMES IS A REAL RECORDING. A session that ended
      // before anything was drawn archives a file with no instructions in it;
      // that is not a failure and must not wear one, but it is also not a
      // player, so the panel says which it is instead of drawing a 40px strip.
      if (sessionRecording.getDuration() <= 0) {
        setPlayback("empty");
        return;
      }
      setPlayback("ready");
      // THE FIRST FRAME AS A POSTER. `SessionRecording` draws nothing until
      // it is played or seeked, so a loaded recording is a blank canvas until
      // somebody presses Play — indistinguishable from a player that does not
      // work. Seeking to zero paints frame one and leaves playback paused,
      // which is what a person expects to find waiting for them.
      sessionRecording.seek(0);
    };
    sessionRecording.onerror = (message) => {
      // The engine's own words go to the console, where somebody filing the
      // defect will find them. They are not shown: "Cannot read properties of
      // undefined (reading 'size')" is not a sentence about a recording.
      console.error("Guacamole.SessionRecording could not read the recording", message);
      setPlayback("failed");
    };

    sessionRecording.connect();

    return () => {
      sessionRecording.onplay = null;
      sessionRecording.onpause = null;
      sessionRecording.onseek = null;
      sessionRecording.onload = null;
      sessionRecording.onerror = null;
      sessionRecording.onprogress = null;
      sized.onresize = null;
      observer?.disconnect();
      sessionRecording.disconnect();
      if (containerRef.current?.contains(display)) {
        containerRef.current.removeChild(display);
      }
      recordingRef.current = null;
    };
  }, [recording.data, format]);

  if (format === "rsp" && rspBytes) {
    return (
      <>
        <PageHead title={t("remote.recording.title")} description={t("remote.recording.description")} />
        <RspPlayback bytes={rspBytes} />
      </>
    );
  }

  return (
    <>
      <PageHead
        title={t("remote.recording.title")}
        description={t("remote.recording.description")}
        actions={
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <span className="mono cell-sub" aria-label={t("remote.recording.position")}>
              {formatSeconds(position)} / {formatSeconds(duration)}
            </span>
            <Btn
              label={playing ? t("remote.recording.pause") : t("remote.recording.play")}
              icon={playing ? "player-pause" : "player-play"}
              disabled={playback !== "ready"}
              onClick={() => {
                const current = recordingRef.current;
                if (!current) return;
                if (current.isPlaying()) current.pause();
                else current.play();
              }}
            />
          </div>
        }
      />
      {/* AUDIT CLASS (cycle 4): `String(recording.error)` printed "Error: "
          noise for an ordinary failure and NOTHING AT ALL for the bodyless 503
          `ApiError` -- an empty message stringifies to "ApiError: ", and a
          recording that could not be fetched looked like one that was simply
          slow. It also carried no `role`, so a screen reader was told nothing.
          A missing recording is evidence somebody came here to review. */}
      {recording.isError && (
        <p role="alert" className="form-error">
          {t("remote.recording.loadFailed", { detail: describeSubmitError(recording.error) })}
        </p>
      )}
      {/* THE SENTENCE IS THE ALERT, AND IT IS THE WHOLE OF WHAT IS SHOWN. The
          parser's `TypeError` used to sit under it in mono: engine text, with
          the word "undefined" in it, on a console screen, above a panel that
          was then empty. It goes to `console.error` instead — the place a
          person filing the defect looks — and the operator is told what it
          means for them. */}
      {playback === "failed" && (
        <p role="alert" className="form-error">
          {t("remote.recording.playerFailed")}
        </p>
      )}
      <Panel title={t("remote.recording.panelTitle")}>
        {/* THE PANEL SAYS WHICH OF ITS THREE EMPTY STATES IT IS IN. Before
            this it was a title over a white strip in all three, which read as
            a broken page whichever one was true. */}
        {playback === "loading" && !recording.isError ? (
          <EmptyState
            icon="progress"
            title={t("remote.recording.readingTitle")}
            body={t("remote.recording.readingBody")}
          />
        ) : null}
        {playback === "empty" ? (
          <EmptyState
            icon="player-play"
            title={t("remote.recording.emptyTitle")}
            body={t("remote.recording.emptyBody")}
          />
        ) : null}
        {playback === "failed" ? (
          <EmptyState
            icon="circle-x"
            title={t("remote.recording.failedTitle")}
            body={t("remote.recording.failedBody")}
          />
        ) : null}
        {/* A TAB STOP, because it can still be a scroll container: `fitToBox`
            fits the picture to this box's width, but a box narrower than the
            recording is scrollable for the instant before the first frame
            declares its size, and a scrollable region a keyboard cannot reach
            is an axe `scrollable-region-focusable` failure (measured at 390px
            by the harmony gate). Same answer the kit's `Table` gives for the
            same reason, and it is named so the stop announces what it holds. */}
        <div
          ref={containerRef}
          data-testid="guac-recording-container"
          /* `position: relative; z-index: 0` MAKES THIS A STACKING
             CONTEXT, and without it the picture is invisible. Guacamole
             gives the default layer's canvas `z-index: -1`, so with no
             context of its own here it paints BEHIND the panel's white
             background: the canvas is drawn, correctly, and nobody can
             see it. Measured in a real browser -- the pixels were there
             (24, 39, 48) under a white panel. */
          style={{ overflow: "auto", position: "relative", zIndex: 0 }}
          tabIndex={0}
          role="group"
          aria-label={t("remote.recording.displayLabel")}
        />
      </Panel>
    </>
  );
}
