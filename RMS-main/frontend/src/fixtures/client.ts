/**
 * THE TYPED PROXY THAT STANDS IN FOR `ApiClient` IN FIXTURE MODE.
 *
 * Every public method of `ApiClient` may have a fixture: a function with the
 * same parameters returning the same result (or a promise of it). The registry
 * is `core.ts` -- the reads the shell itself makes on every page, plus the
 * worked example -- merged with one `fixtures.ts` per module under
 * `src/modules/*`, found by `import.meta.glob` so a module registers by
 * existing rather than by editing a central list.
 *
 * A METHOD WITH NO FIXTURE REJECTS, IT DOES NOT HANG OR RETURN NOTHING. The
 * call is recorded on `window.__fixtureMisses` and rejects with
 * `FixtureMissing`, so the page renders its real error state (the same one a
 * 500 would produce) rather than a blank, and `scripts/harmony.mjs` can read
 * the list and say which fixtures a route still lacks. `--strict-fixtures`
 * turns that list into a failure once a module's fixtures are meant to be
 * complete.
 *
 * Results are `structuredClone`d on every call. Pages and TanStack Query
 * treat a response as their own; a fixture that handed out one shared object
 * would leak one page's edits into the next route the gate walks.
 *
 * Nothing here runs at module scope. `lib/ApiContext.tsx` reaches this file
 * only behind `FIXTURE_MODE`, which is `false` in a production build, so this
 * module and everything it globs is shaken out of the artifact
 * (`preview.test.ts` asserts it).
 */

import type { ApiClient } from "../lib/api";
import { fixtures as core } from "./core";

type AnyMethod = (...args: never[]) => Promise<unknown>;

/** The public method names of `ApiClient`. Private members are not in `keyof`. */
type MethodName = {
  [K in keyof ApiClient]: ApiClient[K] extends AnyMethod ? K : never;
}[keyof ApiClient];

/**
 * One optional entry per `ApiClient` method, with that method's parameters
 * and result. A fixture may answer synchronously; the proxy wraps it.
 */
export type FixtureMap = {
  [K in MethodName]?: ApiClient[K] extends (...args: infer A) => Promise<infer R>
    ? (...args: A) => R | Promise<R>
    : never;
};

export class FixtureMissing extends Error {
  readonly method: string;

  constructor(method: string) {
    super(
      `No fixture answers ApiClient.${method}(). Add one to src/fixtures/core.ts ` +
        "or to the owning module's src/modules/<module>/fixtures.ts.",
    );
    this.name = "FixtureMissing";
    this.method = method;
  }
}

declare global {
  interface Window {
    /** Method names called in fixture mode that no fixture answered, in first-call order. */
    __fixtureMisses?: string[];
  }
}

interface FixtureModule {
  default?: FixtureMap;
  fixtures?: FixtureMap;
}

/** `core.ts` merged with every module's `fixtures.ts`. A module's entry wins, with a warning. */
export function fixtureRegistry(): FixtureMap {
  const merged: Record<string, unknown> = { ...core };
  const modules = import.meta.glob<FixtureModule>("../modules/*/fixtures.ts", { eager: true });
  for (const [path, mod] of Object.entries(modules)) {
    const map = mod.fixtures ?? mod.default;
    if (!map) {
      console.warn(`fixtures: ${path} exports neither \`fixtures\` nor a default export`);
      continue;
    }
    for (const [name, fn] of Object.entries(map)) {
      if (name in merged) console.warn(`fixtures: ${path} overrides an existing fixture for ${name}`);
      merged[name] = fn;
    }
  }
  return merged as FixtureMap;
}

/** Deep copy where the value supports it; a fixture returning `undefined` (a 204) passes through. */
function copy<T>(value: T): T {
  return value === undefined ? value : structuredClone(value);
}

/**
 * An `ApiClient`-shaped object answering from the registry. Every property
 * read is a method; a name with no fixture is a method that records the miss
 * and rejects. `then` is deliberately absent so nothing mistakes the client
 * for a promise.
 */
export function fixtureClient(registry: FixtureMap = fixtureRegistry()): ApiClient {
  const table = registry as Record<string, ((...args: unknown[]) => unknown) | undefined>;
  const misses = (window.__fixtureMisses ??= []);
  const methods = new Map<string, (...args: unknown[]) => Promise<unknown>>();

  const methodFor = (name: string) => {
    const cached = methods.get(name);
    if (cached) return cached;
    const answer = table[name];
    const method =
      typeof answer === "function"
        ? async (...args: unknown[]) => copy(await answer(...args))
        : async () => {
            if (!misses.includes(name)) misses.push(name);
            throw new FixtureMissing(name);
          };
    methods.set(name, method);
    return method;
  };

  return new Proxy({} as ApiClient, {
    get(_target, prop) {
      if (typeof prop !== "string" || prop === "then") return undefined;
      return methodFor(prop);
    },
    has(_target, prop) {
      return typeof prop === "string" && typeof table[prop] === "function";
    },
  });
}
