/**
 * WHETHER A PERSON IS AT THE CONSOLE, as the server's inactivity limit needs
 * to know it (final re-review, P1; coordinator decision, 2026-11-05).
 *
 * The session's 30-minute inactivity limit (own-auth design §2.4) is the
 * server's: "Session timeout management and expiration must be enforced
 * server-side" (OWASP Session Management Cheat Sheet). But the server renewed
 * the session on every request, and the console polls -- the shell's health
 * and review counts every 60 s on every page -- so a console left visible on
 * an unattended computer never went idle: whoever sat down next held the
 * technician's whole authority. NIST SP 800-63B-4 §5.2: "An inactivity
 * timeout terminates a session without activity from the subscriber for a
 * specific period." A poll is not the subscriber.
 *
 * SO THE CONSOLE SAYS WHICH REQUESTS A PERSON CAUSED. A person's input -- a
 * key, a press, a touch, a wheel, a moved pointer -- marks the next 60 s as
 * theirs. Every request made outside that window carries
 * `X-ITMS-Background: 1` (`./api.ts::requestHeaders`), and the server does
 * not renew the session for it.
 *
 * AND A PERSON WHO IS WORKING STAYS SIGNED IN. A page that asks nothing of its
 * own while it is used -- a remote screen, a long form -- would otherwise
 * idle out under someone typing. So while input continues, a request without
 * the header goes out at least every 60 s: the page's own, or, when it made
 * none, the keep-alive (`ApiProvider`'s plain `GET /me`).
 *
 * WINDOW, CAPTURE PHASE: input over the remote screen counts, although the
 * viewer stops its keys and pointer moves from bubbling to the page. Passive
 * listeners, and pointer moves are sampled once a second.
 *
 * NOTHING HERE GATES ANYTHING (invariant 6). A script on the page can say a
 * person is there; it already holds the session. The header can only ask the
 * server NOT to renew.
 */

/** How long one input counts as a person at the console. */
export const ACTIVE_FOR_MS = 60_000;
/** Pointer moves are sampled at most this often. */
const MOVE_SAMPLE_MS = 1_000;

const INPUTS = ["keydown", "pointerdown", "wheel", "touchstart"] as const;

let lastInput = Number.NEGATIVE_INFINITY;
/** The last request that went out without the header. */
let lastForeground = Number.NEGATIVE_INFINITY;
let keepAlive: (() => void) | null = null;

/** A person gave input in the last 60 s. */
export function personActive(now: number = Date.now()): boolean {
  return now - lastInput < ACTIVE_FOR_MS;
}

/**
 * For `requestHeaders`: whether a request made now is background. One that is
 * not is the person's, and is remembered as the last request that renewed
 * the session.
 */
export function backgroundRequest(now: number = Date.now()): boolean {
  if (!personActive(now)) return true;
  lastForeground = now;
  return false;
}

function noteInput(now: number): void {
  lastInput = now;
  if (keepAlive !== null && now - lastForeground >= ACTIVE_FOR_MS) {
    lastForeground = now;
    keepAlive();
  }
}

/**
 * Start listening for a person on `win`. `onKeepAlive` sends a request
 * without the header; it is called on an input when none went out for 60 s.
 * Returns the function that stops listening.
 */
export function watchPersonInput(win: Window, onKeepAlive: () => void): () => void {
  keepAlive = onKeepAlive;
  let lastMove = Number.NEGATIVE_INFINITY;
  const onInput = () => noteInput(Date.now());
  const onMove = () => {
    const now = Date.now();
    if (now - lastMove < MOVE_SAMPLE_MS) return;
    lastMove = now;
    noteInput(now);
  };
  const options: AddEventListenerOptions = { capture: true, passive: true };
  for (const type of INPUTS) win.addEventListener(type, onInput, options);
  win.addEventListener("pointermove", onMove, options);
  return () => {
    for (const type of INPUTS) win.removeEventListener(type, onInput, options);
    win.removeEventListener("pointermove", onMove, options);
    if (keepAlive === onKeepAlive) keepAlive = null;
  };
}

/** For tests: nobody has given input, and nothing has been sent. */
export function resetPersonInput(): void {
  lastInput = Number.NEGATIVE_INFINITY;
  lastForeground = Number.NEGATIVE_INFINITY;
}
