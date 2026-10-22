// Minimal ambient typing for guacamole-common-js@1.5.0 (Apache-2.0). The
// package ships with no TypeScript types and a huge surface area — only the
// members actually used by SessionViewer/RecordingPlayer are typed here.
// See node_modules/guacamole-common-js/dist/esm/guacamole-common.js for the
// full (untyped) implementation this mirrors.
declare module "guacamole-common-js" {
  namespace Guacamole {
    /** Error/status object passed to Client/Tunnel `onerror` handlers. */
    class Status {
      readonly code: number;
      readonly message?: string;
    }

    /** The rendered remote-desktop surface; `getElement()` is attached to the DOM. */
    class Display {
      getElement(): HTMLElement;
      getWidth(): number;
      getHeight(): number;
      scale(scale: number): void;
    }

    /** Base tunnel type; WebSocketTunnel is the concrete transport used here. */
    abstract class Tunnel {
      onerror: ((status: Status) => void) | null;
      onstatechange: ((state: number) => void) | null;
      connect(data?: string): void;
      disconnect(): void;
    }

    class WebSocketTunnel extends Tunnel {
      constructor(tunnelURL: string);
    }

    /** Numeric lifecycle states shared by Client and SessionRecording. */
    interface ClientStateMap {
      readonly IDLE: 0;
      readonly CONNECTING: 1;
      readonly WAITING: 2;
      readonly CONNECTED: 3;
      readonly DISCONNECTING: 4;
      readonly DISCONNECTED: 5;
    }

    class Client {
      constructor(tunnel: Tunnel);
      static State: ClientStateMap;
      onstatechange: ((state: number) => void) | null;
      onerror: ((status: Status) => void) | null;
      getDisplay(): Display;
      connect(data?: string): void;
      disconnect(): void;
    }

    /** Playback of a previously recorded session, read from a Blob. */
    class SessionRecording {
      constructor(source: Blob | Tunnel);
      getDisplay(): Display;
      isPlaying(): boolean;
      getPosition(): number;
      getDuration(): number;
      play(): void;
      pause(): void;
      seek(position: number, callback?: () => void): void;
      connect(data?: string): void;
      disconnect(): void;
      onplay: (() => void) | null;
      onpause: (() => void) | null;
      onseek: ((position: number) => void) | null;
      /** The load lifecycle, fired while the file is being parsed. */
      onload: (() => void) | null;
      onerror: ((message: string) => void) | null;
      onprogress: ((duration: number, parsedSize: number) => void) | null;
    }

    /**
     * The protocol reader. `recordingSource` feeds it the bytes of a stored
     * recording and reads instructions out the other side.
     *
     * DECLARED HERE, not in the module that uses it. It was written in
     * `modules/remote/recordingSource.ts` because `src/types/` is shared
     * ground -- but a hand-written shape for a library's real class is a
     * SECOND declaration of that class, and the module reached it through
     * `as unknown as { Parser: new () => GuacamoleParser }`, which is a cast
     * that would keep compiling after the library changed. This file is what
     * shared ground is FOR: it is this package's typing, and nothing in it
     * belongs to a module.
     */
    class Parser {
      oninstruction: ((opcode: string, args: string[]) => void) | null;
      receive(packet: string): void;
    }
  }

  export default Guacamole;
}
