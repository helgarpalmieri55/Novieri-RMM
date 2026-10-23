/**
 * A stored recording, fed to `Guacamole.SessionRecording` the one way that
 * library actually supports.
 *
 * WHY THIS FILE EXISTS. `guacamole-common-js` 1.5.0 documents two sources for
 * `SessionRecording`: a `Blob`, or a `Guacamole.Tunnel`. The Blob path is
 * broken in the shipped build — its constructor reaches
 *
 *     if (source instanceof Blob)
 *         parseBlob(recordingBlob, loadInstruction, notifyLoaded);
 *
 * where `recordingBlob` has been declared and never assigned, so the first
 * thing the parser touches is `undefined.size` and the constructor throws a
 * `TypeError` synchronously. The console caught that and printed the engine's
 * own words to the operator ("Cannot read properties of undefined (reading
 * 'size')"), under an empty panel: on this route the panel IS the function,
 * so every recording in the archive rendered as an error message over a
 * 40px white strip.
 *
 * The tunnel path in the same constructor is whole: it buffers the
 * instructions it is handed, builds the seekable blob itself, and reports
 * `onload` when the tunnel closes. So the recording is handed over as a
 * tunnel — this file is the smallest honest one: it reads the archived file,
 * replays its instructions through the library's own `Guacamole.Parser`, and
 * closes.
 *
 * NOT A NETWORK TUNNEL, and it never becomes one. It carries no URL, opens no
 * socket and sends nothing anywhere: `connect()` reads a Blob the caller
 * already holds. Nothing on this path reaches a device.
 */

import Guacamole from "guacamole-common-js";


/** `Guacamole.Tunnel.State.CLOSED`, named rather than spelled `2`. */
const OPEN = 1;
const CLOSED = 2;

/**
 * The part of `Guacamole.Tunnel` that `SessionRecording` uses: the three
 * handlers it assigns, and the two methods it calls. Everything else on a
 * real tunnel (uuid, send, receive timeouts) belongs to a live connection and
 * has no meaning for a file already on disk.
 */
export interface RecordingSource {
  oninstruction: ((opcode: string, args: string[]) => void) | null;
  /** Shaped as `Guacamole.Status`, so this is assignable where a tunnel is. */
  onerror: ((status: { code: number; message?: string }) => void) | null;
  onstatechange: ((state: number) => void) | null;
  connect(data?: string): void;
  disconnect(): void;
}

/**
 * The recording in `blob`, as something `SessionRecording` will read.
 *
 * Reading is asynchronous and cancellable: `disconnect()` before the read
 * finishes drops the result on the floor rather than pushing instructions
 * into a player the page has already torn down.
 */
export function recordingSource(blob: Blob): RecordingSource {
  let stopped = false;
  let closed = false;

  const source: RecordingSource = {
    oninstruction: null,
    onerror: null,
    onstatechange: null,

    connect() {
      stopped = false;
      source.onstatechange?.(OPEN);
      void readAll();
    },

    disconnect() {
      stopped = true;
      close();
    },
  };

  /** CLOSED exactly once: the library treats it as "the recording is loaded". */
  function close() {
    if (closed) return;
    closed = true;
    source.onstatechange?.(CLOSED);
  }

  async function readAll() {
    let text: string;
    try {
      text = await blob.text();
    } catch (error) {
      fail(error);
      return;
    }
    if (stopped) return;

    const parser = new Guacamole.Parser();
    parser.oninstruction = (opcode, args) => {
      // A COPY, because `Guacamole.Parser` reuses ONE array for every
      // instruction and empties it the moment this returns (`element_buffer
      // .length = 0`). Anything downstream that holds the arguments past the
      // call — a test, a log, a future capability — would otherwise be
      // holding an array that empties itself.
      if (!stopped) source.oninstruction?.(opcode, [...args]);
    };
    try {
      parser.receive(text);
    } catch (error) {
      // A file that is not a Guacamole log at all, or one truncated inside an
      // instruction. Reported, not swallowed: `SessionRecording` marks the
      // load failed and never claims the recording is complete.
      fail(error);
      return;
    }
    close();
  }

  function fail(error: unknown) {
    if (stopped) return;
    source.onerror?.({
      code: 0,
      message: error instanceof Error ? error.message : String(error),
    });
    close();
  }

  return source;
}
