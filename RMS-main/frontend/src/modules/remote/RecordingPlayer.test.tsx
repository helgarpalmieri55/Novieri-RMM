import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../../lib/api";
import { T, encode } from "./rsp";

// Shallow wrapper test: asserts the
// recording is fetched and SessionRecording is mounted, without a real
// Guacamole protocol stream.
class FakeDisplay {
  /** The remote screen's own size, as the archived stream declares it. */
  private width = 1024;
  private height = 768;
  readonly element = document.createElement("div");
  /** Every factor `scale()` was called with, in order. */
  readonly scales: number[] = [];
  onresize: ((width: number, height: number) => void) | null = null;
  getElement() {
    return this.element;
  }
  getWidth() {
    return this.width;
  }
  getHeight() {
    return this.height;
  }
  scale(factor: number) {
    this.scales.push(factor);
  }
}

/** The display the player mounted, for the fit-to-container assertions. */
let lastDisplay: FakeDisplay | null = null;

/**
 * WHAT THE PLAYER IS TOLD once it has connected. These are the library's own
 * three outcomes for a file it has been handed: frames arrived, the file
 * parsed and held nothing, or the file could not be parsed. The page draws a
 * different panel for each, so each has a test.
 */
type Scenario = "frames" | "empty" | "parse-error";
let scenario: Scenario = "frames";

// What the fake's CONSTRUCTOR throws, when a test wants the library to refuse
// the source outright. 1.5.0 really does throw synchronously from
// `new SessionRecording(blob)` -- that defect is why the recording is now
// handed over as a tunnel (`recordingSource.ts`) -- and the guard against a
// throwing constructor stays regardless.
let constructorError: Error | null = null;

/** The source the page handed the player, for the assertion that it is not a Blob. */
let lastSource: unknown = null;

class FakeSessionRecording {
  onplay: (() => void) | null = null;
  onpause: (() => void) | null = null;
  onseek: ((position: number) => void) | null = null;
  onload: (() => void) | null = null;
  onerror: ((message: string) => void) | null = null;
  onprogress: ((duration: number, parsedSize: number) => void) | null = null;
  private playing = false;
  private duration = 0;
  constructor(private readonly source: unknown) {
    lastSource = source;
    if (constructorError) throw constructorError;
  }
  private readonly display = new FakeDisplay();
  getDisplay() {
    // ONE DISPLAY PER RECORDING, as the library gives: the page reads the
    // element, the intrinsic size and `scale()` off the same object, and a
    // fake that minted a new one per call would let a broken page pass.
    lastDisplay = this.display;
    return this.display;
  }
  isPlaying() {
    return this.playing;
  }
  getPosition() {
    return 0;
  }
  getDuration() {
    return this.duration;
  }
  play() {
    this.playing = true;
    this.onplay?.();
  }
  pause() {
    this.playing = false;
    this.onpause?.();
  }
  seek() {
    // not exercised by this smoke test
  }
  connect() {
    // The library reads the source through the handlers it was given; the
    // fake plays the same three outcomes back.
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
  disconnect() {
    // no-op
  }
}

/**
 * `Guacamole.Parser` is mocked too, because `recordingSource` -- the real one,
 * unmocked, which the page now hands the player -- reads the archived file
 * through it. A parser that yields nothing is enough here: the adapter's own
 * behaviour is covered against the real parser in `recordingSource.test.ts`.
 */
class FakeParser {
  oninstruction: ((opcode: string, args: string[]) => void) | null = null;
  receive() {
    // No instructions: this file's subject is the page, not the protocol.
  }
}

vi.mock("guacamole-common-js", () => ({
  default: { SessionRecording: FakeSessionRecording, Parser: FakeParser },
}));

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const { RecordingPlayer } = await import("./RecordingPlayer");

function renderPlayer() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/remote/s1/recording"]}>
        <Routes>
          <Route path="/remote/:sessionId/recording" element={<RecordingPlayer />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RecordingPlayer", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    scenario = "frames";
    constructorError = null;
    lastSource = null;
    lastDisplay = null;
    vi.spyOn(console, "error").mockImplementation(() => {});
    api = {
      getRemoteRecording: vi.fn().mockResolvedValue(new Blob(["fake-guac-log"])),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("downloads the recording and mounts the SessionRecording player", async () => {
    renderPlayer();

    expect(await screen.findByRole("button", { name: "Play" })).toBeInTheDocument();
    expect(api.getRemoteRecording).toHaveBeenCalledWith("s1");
  });

  // ---------------------------------------------------------------------
  // THE ROOT OF THE BLANK ROUTE. `guacamole-common-js` 1.5.0 declares
  // `recordingBlob` and never assigns it on the Blob path, so `new
  // SessionRecording(blob)` dereferences `undefined.size` and throws before
  // a single frame is read -- EVERY recording in the archive rendered as an
  // engine message over an empty panel. The file is handed to the tunnel
  // path the same constructor implements properly.
  //
  // BREAKS AGAINST: the Blob passed straight to the constructor again.
  // ---------------------------------------------------------------------
  it("hands the player a readable source rather than the Blob the library cannot read", async () => {
    renderPlayer();
    await waitFor(() => expect(lastSource).not.toBeNull());

    expect(lastSource).not.toBeInstanceOf(Blob);
    expect(lastSource).toMatchObject({
      connect: expect.any(Function),
      disconnect: expect.any(Function),
    });
  });

  it("enables the transport control once there are frames to play", async () => {
    renderPlayer();
    // The control is disabled until the file has produced a frame, so this
    // waits for the load rather than asserting on the first paint.
    await waitFor(() => expect(screen.getByRole("button", { name: "Play" })).toBeEnabled());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText(/holds no frames/i)).not.toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // CYCLE 4 on this route. The failure path was
  // `<p>{String(recording.error)}</p>`: "Error: …" noise for an ordinary
  // failure, and NOTHING AT ALL for the bodyless 503 `ApiError` an empty
  // message stringifies to. It carried no `role` either, so a screen reader
  // was told nothing at all. A recording that will not load is evidence
  // somebody came here to review.
  //
  // BREAKS AGAINST: that line, restored.
  // -------------------------------------------------------------------------
  it("explains a recording that could not be loaded, and announces it", async () => {
    api.getRemoteRecording = vi.fn().mockRejectedValue(new ApiError(503, ""));
    renderPlayer();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/could not be loaded/i);
    expect(alert).toHaveTextContent(/unavailable/i);
    expect(alert.textContent).not.toMatch(/^ApiError/);
  });

  // -------------------------------------------------------------------------
  // THE PARSER REFUSING A FILE THAT DID ARRIVE. The page has to say so in a
  // sentence -- and ONLY in a sentence. It used to print the engine's own
  // words under the alert in mono ("Cannot read properties of undefined
  // (reading 'size')"): a `TypeError` is not console copy, and the word
  // "undefined" on an operator's screen reads as a broken console rather
  // than as an unreadable file. The detail goes to `console.error`, where
  // the person filing the defect will find it.
  //
  // BREAKS AGAINST: the engine's message put back on the page, the guard
  // removed, or a display mounted for a player that produced no frames.
  // -------------------------------------------------------------------------
  it("says the player could not read the recording, in words of its own", async () => {
    scenario = "parse-error";
    renderPlayer();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The player could not read this recording.");
    expect(alert).not.toHaveTextContent(/undefined/i);
    expect(screen.queryByText(/Illegal terminator/)).not.toBeInTheDocument();
    // The engine's words are kept, off the page, for a defect report.
    expect(console.error).toHaveBeenCalled();
    // And the panel is not an empty strip: it says why it holds nothing.
    expect(screen.getByText("Nothing could be played")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Play" })).toBeDisabled();
  });

  it("survives a constructor that throws, without printing its words", async () => {
    constructorError = new TypeError("Cannot read properties of undefined (reading 'size')");
    renderPlayer();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The player could not read this recording.");
    expect(screen.queryByText(/Cannot read properties/)).not.toBeInTheDocument();
    expect(screen.getByTestId("guac-recording-container")).toBeEmptyDOMElement();
    expect(screen.getByRole("button", { name: "Play" })).toBeDisabled();
  });

  // -------------------------------------------------------------------------
  // THE STAGE WAS A FIXED-SIZE BOX, AND ON A HANDSET THAT IS NOT A PLAYER.
  // `SessionRecording` draws at the remote screen's intrinsic size and hands
  // back an element carrying it in pixels: 1024 wide inside a ~330px card at
  // 390x844, so the review found the dark stage sliced off by the card's
  // right edge and the picture running off-screen mid-frame, with no way to
  // reach the rest of it. At 1440 the same 1024 sat off-centre in an 1140px
  // card. The factor is the container's width over the recording's own, one
  // number for both axes, so the aspect ratio is preserved exactly.
  //
  // BREAKS AGAINST: the display mounted at its intrinsic size again, or
  // scaled by anything that is not the container's width over the
  // recording's.
  // -------------------------------------------------------------------------
  it("scales the recording to the width of the box it is in, keeping its shape", async () => {
    const width = vi.spyOn(Element.prototype, "clientWidth", "get").mockReturnValue(600);
    try {
      renderPlayer();
      await waitFor(() => expect(lastDisplay).not.toBeNull());
      await waitFor(() => expect(lastDisplay?.scales.length).toBeGreaterThan(0));
      // 600 of card over 1024 of recording. One factor, both axes.
      expect(lastDisplay?.scales.at(-1)).toBeCloseTo(600 / 1024, 5);
    } finally {
      width.mockRestore();
    }
  });

  // -------------------------------------------------------------------------
  // THE OTHER END OF THE SAME FIX. A 1024px recording stretched to fill an
  // 1140px card is a blurred picture of the same frame; the review asked for
  // letterboxed and centred, which is a factor of 1 and `margin: 0 auto`.
  //
  // BREAKS AGAINST: `available / intrinsic` without the cap at 1.
  // -------------------------------------------------------------------------
  it("never enlarges a recording past its own resolution, and centres it instead", async () => {
    const width = vi.spyOn(Element.prototype, "clientWidth", "get").mockReturnValue(1140);
    try {
      renderPlayer();
      await waitFor(() => expect(lastDisplay?.scales.length).toBeGreaterThan(0));
      expect(lastDisplay?.scales.at(-1)).toBe(1);
      expect(lastDisplay?.element.style.margin).toBe("0px auto");
    } finally {
      width.mockRestore();
    }
  });

  // -------------------------------------------------------------------------
  // THE RECORDING DECLARES ITS SCREEN SIZE WHILE THE FILE IS BEING PARSED,
  // which is after the element is in the DOM. A page that fits only once, at
  // mount, fits a display whose `getWidth()` is still 0 and then leaves the
  // real picture at its intrinsic size -- the defect this route had, with an
  // extra step.
  //
  // BREAKS AGAINST: `Display.onresize` left unwired.
  // -------------------------------------------------------------------------
  it("re-fits when the recording declares the remote screen's size", async () => {
    const width = vi.spyOn(Element.prototype, "clientWidth", "get").mockReturnValue(600);
    try {
      renderPlayer();
      await waitFor(() => expect(lastDisplay?.onresize).toBeTypeOf("function"));
      const before = lastDisplay?.scales.length ?? 0;
      lastDisplay?.onresize?.(1024, 768);
      expect(lastDisplay?.scales.length).toBe(before + 1);
      expect(lastDisplay?.scales.at(-1)).toBeCloseTo(600 / 1024, 5);
    } finally {
      width.mockRestore();
    }
  });

  // A box with no layout yet (jsdom's default, a panel not on screen) and a
  // recording with no frame parsed both report zero, and neither means "draw
  // this at nothing".
  it("does not scale the picture to nothing when there is no width to fit to", async () => {
    renderPlayer();
    await waitFor(() => expect(lastDisplay).not.toBeNull());
    expect(lastDisplay?.scales).toEqual([]);
  });

  // -------------------------------------------------------------------------
  // A RECORDING THAT PARSED AND HELD NOTHING IS NOT A FAILURE. A session that
  // ended before the desktop was drawn archives exactly this file, and the
  // route's whole function is the panel: an empty 40px strip says "broken" for
  // a state that is ordinary. It is an empty state, not an alarm.
  //
  // BREAKS AGAINST: the empty recording drawn as an error, or as nothing.
  // -------------------------------------------------------------------------
  it("says a recording that holds no frames holds no frames", async () => {
    scenario = "empty";
    renderPlayer();

    expect(await screen.findByText("This recording holds no frames")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Play" })).toBeDisabled();
  });
});

// A screen-session recording: "ITMSRSP1" then (t_ms, len, frame) records.
function rspRecording(records: [number, Uint8Array][]): Blob {
  const size = 8 + records.reduce((n, [, f]) => n + 8 + f.length, 0);
  const out = new Uint8Array(size);
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

describe("RecordingPlayer with an rsp/1 recording", () => {
  beforeEach(() => {
    lastSource = null;
    HTMLCanvasElement.prototype.getContext = vi.fn(() => null) as unknown as HTMLCanvasElement["getContext"];
  });

  it("plays it with the screen viewer's decoder, not Guacamole", async () => {
    const hello = new TextEncoder().encode(
      JSON.stringify({
        version: 1,
        capabilities: [],
        monitors: [{ id: 0, x: 0, y: 0, width: 800, height: 600, dpi: 96, primary: true }],
        codecs: ["fill"],
        selected: 0,
      }),
    );
    const tile = Uint8Array.of(0, 0, 0, 0, 0, 64, 0, 64, 0, 10, 20, 30);
    mockUseApi.mockReturnValue({
      getRemoteRecording: vi.fn().mockResolvedValue(
        rspRecording([
          [0, encode(T.HELLO, 1, hello)],
          [5, encode(T.FRAME_BEGIN, 2)],
          [5, encode(T.TILE, 3, tile)],
          [6, encode(T.FRAME_END, 4)],
          [65_000, encode(T.FRAME_BEGIN, 5)],
          [65_000, encode(T.FRAME_END, 6)],
        ]),
      ),
    } as Partial<ApiClient> as ApiClient);
    renderPlayer();

    expect(await screen.findByText("0:00 / 1:05")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Play" })).toBeEnabled();
    const canvas = screen.getByTestId("rsp-canvas") as HTMLCanvasElement;
    // WAITED FOR, not read at once: the poster frame (and with it the resize)
    // is applied in an effect after the commit that renders the time label,
    // so on a busy runner the label can be found while the canvas is still
    // the browser default 300x150 (CI, 2026-09-23).
    await waitFor(() => expect([canvas.width, canvas.height]).toEqual([800, 600]));
    expect(lastSource).toBeNull(); // Guacamole never saw it
  });

  it("says so when an rsp/1 recording holds no frames", async () => {
    mockUseApi.mockReturnValue({
      getRemoteRecording: vi.fn().mockResolvedValue(rspRecording([])),
    } as Partial<ApiClient> as ApiClient);
    renderPlayer();
    expect(await screen.findByText("This recording holds no frames")).toBeInTheDocument();
  });
});
