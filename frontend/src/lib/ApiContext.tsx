import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { FIXTURE_MODE } from "../fixtures/mode";
import { ApiClient } from "./api";
import { isClientId, selectedClient } from "./clientChoice";
import { watchPersonInput } from "./personInput";
import { sessionEnded } from "./sessionEnd";

const ApiContext = createContext<ApiClient | null>(null);

export function ApiProvider({ children }: { children: ReactNode }) {
  // FIXTURE MODE (`?__fixtures`, DEV only): every page talks to a typed proxy
  // answering from `src/fixtures/` instead of the network.
  //
  // LOADED BY A DYNAMIC IMPORT, NOT A STATIC ONE, and the difference is the
  // production bundle. This file used to `import { fixtureClient }` at the
  // top and pick it behind `FIXTURE_MODE`; the branch folded away, but rollup
  // keeps an imported module whose top level has side effects, and a module
  // `fixtures.ts` that computes a timestamp or reads a global at module scope
  // is such a module. The first time one of those landed, `lis-ws-041` and a
  // KB number were in `dist/` -- `src/test/preview.test.ts` caught it. With
  // the import inside a branch that is `false` at build time there is no
  // static path from production code into `src/fixtures/` for rollup to
  // follow, whatever a fixture file does at its top level.
  const [fixture, setFixture] = useState<ApiClient | null>(null);
  useEffect(() => {
    // `import.meta.env.DEV` is written out here, not only inside `mode.ts`:
    // Vite replaces it with the literal `false` in THIS module, so the whole
    // `if` is dead before rollup ever sees the `import()`. Reading it through
    // `FIXTURE_MODE` alone left the branch alive and the chunk emitted.
    if (import.meta.env.DEV && FIXTURE_MODE) {
      let live = true;
      void import("../fixtures/client").then((mod) => {
        if (live) setFixture(mod.fixtureClient());
      });
      return () => {
        live = false;
      };
    }
    return undefined;
  }, []);
  const client = useMemo(
    () =>
      FIXTURE_MODE
        ? fixture
        : new ApiClient(
            // A 401 IS THE SESSION ENDING, NOT A NAVIGATION (own-auth design
            // §2.4). `lib/auth.ts`'s `signIn()` used to redirect from here, on
            // any 401, from any page -- including a sign-in page whose own
            // language save answered 401, which is a loop (plan decision
            // D20). The flag is read by the guard, which sends a tab that
            // never had a session to sign in, and by the shell, which keeps a
            // page whose session ended mid-task and says so.
            sessionEnded,
            "/api/v1",
            // The top bar's choice, read on every request (`X-Client-Id`) --
            // and sent ONLY AS A CLIENT ID. The server answers a malformed
            // header with 400 on every route, `/me` included, which would
            // leave the console unable even to learn that it should drop the
            // choice. A real choice is an id from `/me`; what this refuses
            // is a value that was never one (a fixture id outside fixture
            // mode, a bug), and it goes as no header: the person's whole
            // scope, never wider -- and the provider, finding it in no `/me`
            // list, falls back to every client and says so
            // (`./clientScope`). Fixture mode never builds this client.
            () => {
              const client = selectedClient();
              return isClientId(client) ? client : null;
            },
          ),
    [fixture],
  );
  // A PERSON WHO IS WORKING STAYS SIGNED IN (`./personInput.ts`, final
  // re-review P1): while they give input and no request without the
  // background header went out for 60 s, a plain `GET /me` does.
  useEffect(() => {
    if (!client) return undefined;
    return watchPersonInput(window, () => {
      client.me().catch(() => {
        // A 401 is the session's business (`onUnauthorized`); an outage is
        // the next request's.
      });
    });
  }, [client]);

  // Nothing renders until the fixture client is here: a page mounted against
  // a null client would throw from `useApi`, and the gate would screenshot
  // the error boundary instead of the page.
  if (!client) return null;
  return <ApiContext.Provider value={client}>{children}</ApiContext.Provider>;
}

export function useApi(): ApiClient {
  const client = useContext(ApiContext);
  if (!client) throw new Error("useApi outside ApiProvider");
  return client;
}
