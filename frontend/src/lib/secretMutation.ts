/**
 * A MUTATION THAT CARRIES A SECRET, AND LEAVES NONE BEHIND (operator decision
 * 4, 2026-11-05, "Barrer todas ahora"; final review D2, Minor 1).
 *
 * TanStack keeps every mutation -- the body it was sent (`state.variables`)
 * and its answer (`state.data`) -- in the mutation cache: while the component
 * that ran it stays mounted, and for `gcTime`, five minutes by default, after
 * it unmounts. For a relay password, a client secret, a license key or an
 * enrollment token that is the secret, kept in the tab after the form box was
 * emptied or the "shown once" panel was closed. `reset()` alone only detaches
 * a mutation from the page; `gcTime: 0` alone leaves it attached while the
 * page is mounted. The pattern of 60cf695 is both, and this is that pattern in
 * one place:
 *
 *   1. `gcTime: 0`, always: a mutation with no observer goes as soon as it
 *      settles, the page that sent it unmounted or not;
 *   2. the page's own callbacks run FIRST -- the answer's `onSuccess` is where
 *      a value shown once moves into the page's state, the one place it is
 *      held;
 *   3. then `reset()`, once it settles, success or failure: the body and the
 *      answer leave the cache.
 *
 * WHAT CAME OF IT IS KEPT, IN REACT STATE: pending, succeeded, or the refusal
 * the page shows -- never the body and never the answer, so there is no
 * `data` and no `variables` here. A page that needs the answer takes it in
 * `onSuccess` (or from `mutateAsync`).
 *
 * `src/test/secretMutations.test.ts` lists the hooks that must use this, and
 * fails on a mutation of a secret-bearing `ApiClient` method that does not.
 */
import { useMutation, type MutateOptions, type UseMutationOptions } from "@tanstack/react-query";
import { useCallback, useState } from "react";

type Outcome<TError> = { status: "idle" | "pending" | "success" } | { status: "error"; error: TError };

export interface SecretMutation<TData, TError, TVariables, TContext> {
  mutate(variables: TVariables, options?: MutateOptions<TData, TError, TVariables, TContext>): void;
  mutateAsync(variables: TVariables, options?: MutateOptions<TData, TError, TVariables, TContext>): Promise<TData>;
  /** Forget the outcome as well. */
  reset(): void;
  isIdle: boolean;
  isPending: boolean;
  isSuccess: boolean;
  isError: boolean;
  error: TError | null;
}

export function useSecretMutation<TData = unknown, TError = Error, TVariables = void, TContext = unknown>(
  options: Omit<UseMutationOptions<TData, TError, TVariables, TContext>, "gcTime">,
): SecretMutation<TData, TError, TVariables, TContext> {
  const mutation = useMutation<TData, TError, TVariables, TContext>({ ...options, gcTime: 0 });
  const [outcome, setOutcome] = useState<Outcome<TError>>({ status: "idle" });
  const { mutate: send, mutateAsync: sendAsync, reset: detach } = mutation;

  const mutate = useCallback(
    (variables: TVariables, callbacks?: MutateOptions<TData, TError, TVariables, TContext>) => {
      setOutcome({ status: "pending" });
      send(variables, {
        ...callbacks,
        onSettled: (data, error, sent, context, functionContext) => {
          callbacks?.onSettled?.(data, error, sent, context, functionContext);
          setOutcome(error ? { status: "error", error } : { status: "success" });
          detach();
        },
      });
    },
    [send, detach],
  );

  const mutateAsync = useCallback(
    async (variables: TVariables, callbacks?: MutateOptions<TData, TError, TVariables, TContext>) => {
      setOutcome({ status: "pending" });
      try {
        const data = await sendAsync(variables, callbacks);
        setOutcome({ status: "success" });
        return data;
      } catch (error: unknown) {
        setOutcome({ status: "error", error: error as TError });
        throw error;
      } finally {
        detach();
      }
    },
    [sendAsync, detach],
  );

  const reset = useCallback(() => {
    detach();
    setOutcome({ status: "idle" });
  }, [detach]);

  return {
    mutate,
    mutateAsync,
    reset,
    isIdle: outcome.status === "idle",
    isPending: mutation.isPending || outcome.status === "pending",
    isSuccess: outcome.status === "success",
    isError: outcome.status === "error",
    error: outcome.status === "error" ? outcome.error : null,
  };
}
