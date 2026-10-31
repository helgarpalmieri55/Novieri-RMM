import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { FIXTURE_MODE } from "../fixtures/mode";
import { ApiClient } from "./api";
import { accessToken, signIn } from "./auth";
import { selectedClient } from "./clientChoice";

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
            accessToken,
            () => {
              void signIn();
            },
            "/api/v1",
            // The top bar's choice, read on every request (`X-Client-Id`).
            selectedClient,
          ),
    [fixture],
  );
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
