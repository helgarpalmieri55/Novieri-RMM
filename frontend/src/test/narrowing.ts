/**
 * AN API DOUBLE THAT CAN TELL A NARROWED READ FROM AN UN-NARROWED ONE
 * (decision D6; Task 5 review, fix rounds 1 and 2).
 *
 * The first version answered `forClient(id)` with the same double, so a test
 * asserting "the list read through `forClient(<client>)`" passed whenever
 * ANYTHING on the page narrowed, even while the list itself read through the
 * console's `useApi()`: the property could not go red. Now:
 *
 * - `api` is the console's API as `useApi()` gives it. It answers only the
 *   methods named in `options.console` (plus `me`, which the shell's client
 *   scope always reads through it). ANY OTHER CALL ON IT REJECTS and is
 *   recorded in `unnarrowed`, so a read that should have been narrowed fails
 *   loudly: the page draws its error and the narrowed spy is never called.
 * - `api.forClient(id)` returns that client's OWN double: every method a
 *   `vi.fn` spy (one per client and method) that calls the wrapped API. Its
 *   `forClient(other)` is `other`'s double, as `ApiClient.forClient` is.
 * - `narrowing.client(id)` is the same spies, for the test to assert on,
 *   whether or not the page has reached them yet.
 * - IT CHECKS ITSELF (fix round 2): `recordingNarrowing` registers an
 *   `onTestFinished` hook that fails the test when `unnarrowed` is not empty
 *   at its end, so a test that forgets to assert it still fails on a leak.
 *   Call it inside a test (not at a `describe`'s top level). A test whose
 *   subject IS the leak asserts it and then empties the array
 *   (`narrowing.unnarrowed.length = 0`).
 *
 *   const narrowing = recordingNarrowing(fixtureClient(), { console: ["getClientRecord"] });
 *   mockUseApi.mockReturnValue(narrowing.api);
 *   renderPage(...);
 *   await waitFor(() => expect(narrowing.client(ANDES).listHealthAlerts).toHaveBeenCalledWith({ state: "active" }));
 *   expect(narrowing.unnarrowed).toEqual([]);   // also checked by the helper at the test's end
 */
import { expect, onTestFinished, vi, type Mock } from "vitest";

/** Every method of `T` as a spy with that method's own signature. */
export type Spies<T> = {
  readonly [K in keyof T]: T[K] extends (...args: infer A) => infer R ? Mock<(...args: A) => R> : never;
};

export interface Narrowing<T> {
  /** The console's (un-narrowed) API: give it to the page through the mocked `useApi`. */
  readonly api: T;
  /** Every id `forClient` was called with, in order, on the console's API or on a narrowed one. */
  readonly narrowedTo: string[];
  /**
   * Methods called on the console's API that `options.console` does not allow, in call order. Each one
   * rejected, and a non-empty list fails the test at its end.
   */
  readonly unnarrowed: string[];
  /** `id`'s narrowed double, as spies: what its reads went through. */
  client(id: string): Spies<T>;
}

/** The console's own read the shell makes on every page: `/me` (the client scope, the permissions). */
const ALWAYS: readonly string[] = ["me"];

export function recordingNarrowing<T extends object>(
  api: T,
  options: {
    /**
     * What the page may call on the console's API WITHOUT narrowing: global
     * routes (a client's own record), writes (which keep `useApi()`), and
     * reads made for the top bar's own choice. Anything else rejects.
     */
    console?: readonly string[];
  } = {},
): Narrowing<T> {
  const narrowedTo: string[] = [];
  const unnarrowed: string[] = [];
  const allowed = new Set([...ALWAYS, ...(options.console ?? [])]);
  // The helper's own check, whether or not the test asserts `unnarrowed`.
  onTestFinished(() => {
    expect(unnarrowed, "reads called on the console's un-narrowed API (recordingNarrowing)").toEqual([]);
  });
  const doubles = new Map<string, T>();
  const spies = new Map<string, Map<string, Mock>>();

  /** The wrapped API's own method, called on the wrapped API (a real `ApiClient` reads `this`). */
  function real(method: string): ((...args: unknown[]) => unknown) | undefined {
    const value: unknown = Reflect.get(api, method);
    return typeof value === "function" ? (...args: unknown[]) => (value as (...a: unknown[]) => unknown).apply(api, args) : undefined;
  }

  function spyFor(clientId: string, method: string): Mock {
    let byMethod = spies.get(clientId);
    if (!byMethod) {
      byMethod = new Map();
      spies.set(clientId, byMethod);
    }
    let spy = byMethod.get(method);
    if (!spy) {
      spy = vi.fn((...args: unknown[]) => {
        const call = real(method);
        return call ? call(...args) : Promise.reject(new Error(`the wrapped API has no ${method}()`));
      });
      byMethod.set(method, spy);
    }
    return spy;
  }

  function forClient(clientId: string): T {
    narrowedTo.push(clientId);
    let double = doubles.get(clientId);
    if (!double) {
      double = new Proxy(api, {
        get(_target, prop) {
          if (typeof prop !== "string" || prop === "then") return undefined;
          if (prop === "forClient") return forClient;
          return spyFor(clientId, prop);
        },
      });
      doubles.set(clientId, double);
    }
    return double;
  }

  const consoleApi: T = new Proxy(api, {
    get(_target, prop) {
      if (typeof prop !== "string" || prop === "then") return undefined;
      if (prop === "forClient") return forClient;
      if (allowed.has(prop)) return real(prop);
      return () => {
        unnarrowed.push(prop);
        return Promise.reject(new Error(`${prop}() was called on the console's un-narrowed API`));
      };
    },
  });

  return {
    api: consoleApi,
    narrowedTo,
    unnarrowed,
    client: (id) =>
      new Proxy({} as Spies<T>, {
        get: (_target, prop) => (typeof prop === "string" ? spyFor(id, prop) : undefined),
      }),
  };
}
