/**
 * The reads and writes of your security page (`SecurityPage.tsx`), through
 * `useApi()` like every module.
 *
 * EVERY WRITE GOES THROUGH THE PAGE'S `useStepUp().run`, which answers the
 * server's 403 `step_up_required` (own authentication design §2.5) by asking
 * for the second factor and sending the request once more. EVERY WRITE THAT
 * CHANGES WHAT THE PAGE SHOWS re-reads `GET /me/security`: the page shows what
 * the server holds after the write, never what was typed.
 *
 * EVERY MUTATION THAT CARRIES A SECRET SETS `gcTime: 0` -- the authenticator
 * key and the code that turns it on, new recovery codes, both passwords. The
 * page moves a value shown once into its own state and resets the mutation,
 * but `reset()` only detaches a mutation from the page: TanStack keeps a
 * detached one, its answer and the body it was sent, in the mutation cache
 * for its `gcTime`, five minutes by default (query-core 5.101.2,
 * `mutationObserver.ts` `reset`, `removable.ts`). With `gcTime: 0` it is
 * dropped as soon as it is reset (Task 12 review, Important 1).
 *
 * THE FOUR ENROL CALLS SAY `{ signedIn: true }` (Task 11's `EnrollFrom`): this
 * page has the full console session, so a 401 from them is that session
 * ending and must reach `onUnauthorized`, not stay a message on the page. The
 * passkey ceremony is Task 11's `createPasskey`, the one the first-factor page
 * uses.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { MY_SECURITY_KEY, type StepUpRun } from "../../components/stepUp";
import type { ApiError } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { useSecretMutation } from "../../lib/secretMutation";
import { createPasskey } from "../auth/passkey";
import type { RecoveryCodes } from "../auth/types";
import type { MySecurity, PasswordChange } from "./types";

export function useMySecurity() {
  const api = useApi();
  return useQuery<MySecurity, ApiError>({
    queryKey: [MY_SECURITY_KEY],
    queryFn: () => api.getMySecurity(),
    retry: false,
  });
}

function useRefetchSecurity(): () => void {
  const queryClient = useQueryClient();
  return () => void queryClient.invalidateQueries({ queryKey: [MY_SECURITY_KEY] });
}

/**
 * A password change also forgets the remembered devices and ends the other
 * sessions (design §2.6), so the page re-reads. Its body is both passwords:
 * `gcTime: 0`, and the page resets it once it settles.
 */
export function useChangePasswordMutation(run: StepUpRun) {
  const api = useApi();
  const refetch = useRefetchSecurity();
  return useMutation<void, Error, PasswordChange>({
    mutationFn: (body) => run(() => api.changePassword(body)),
    onSuccess: refetch,
    gcTime: 0,
  });
}

/**
 * The FIRST password, for someone who has none (own-auth phase 2): a recent
 * confirmation, like a change. Its body is the password: `useSecretMutation`
 * (A2-68; `test/secretMutations.test.ts`).
 */
export function useAddPasswordMutation(run: StepUpRun) {
  const api = useApi();
  const refetch = useRefetchSecurity();
  return useSecretMutation<void, Error, string>({
    mutationFn: (password) => run(() => api.addPassword(password)),
    onSuccess: refetch,
  });
}

/**
 * What came of "Add a passkey": `added`, or `confirmed` -- the second factor
 * had to be confirmed on the way, and the page asks for one more click.
 */
export type AddPasskeyOutcome = "added" | "confirmed";

/**
 * Add a passkey (design §2.2): the server's creation options -- user
 * verification required, the person's own passkeys excluded (Task 5) -- to
 * the browser, and the browser's answer back under the name given.
 *
 * A BROWSER CREATES A PASSKEY ONLY FROM A CLICK (Task 12 review, Important 2).
 * WebKit (https://webkit.org/blog/11312/meet-face-id-and-touch-id-for-the-web/):
 * "Face ID and Touch ID for the web require user gestures to function", and
 * the gesture is "not ... propagated to any async executors" except XHR and
 * the Fetch API. When the options request needed the recent confirmation,
 * the click that asked was spent on the confirmation dialog (a passkey there
 * waits on Touch ID, which is no fetch), so `createPasskey` is NOT called from
 * the confirmation's continuation: the mutation stops with `confirmed`, and
 * the page's "Create the passkey" button runs it again, from a fresh click,
 * where the options are fetched by that click (fetch carries the gesture) and
 * nothing else stands before `createPasskey`. `run` sends the action once and,
 * after a confirmation, once more: two calls are how the confirmation is seen.
 */
export function useAddPasskeyMutation(run: StepUpRun) {
  const api = useApi();
  const refetch = useRefetchSecurity();
  return useMutation<AddPasskeyOutcome, Error, { name: string }>({
    mutationFn: async ({ name }) => {
      let asked = 0;
      const optionsJSON = await run(() => {
        asked += 1;
        return api.enrollPasskeyOptions({ signedIn: true });
      });
      if (asked > 1) return "confirmed";
      const credential = await createPasskey(optionsJSON);
      await run(() => api.enrollPasskey(credential, name, { signedIn: true }));
      return "added";
    },
    onSuccess: (outcome) => {
      if (outcome === "added") refetch();
    },
  });
}

export function useRemovePasskeyMutation(run: StepUpRun) {
  const api = useApi();
  const refetch = useRefetchSecurity();
  return useMutation<void, Error, string>({
    mutationFn: (passkeyId) => run(() => api.removePasskey(passkeyId)),
    onSuccess: refetch,
  });
}

/** The first half of setting up the app: the server makes a key and answers with its `otpauth://` address. */
export function useStartTotpMutation(run: StepUpRun) {
  const api = useApi();
  return useMutation<{ otpauth_uri: string }, Error, void>({
    mutationFn: () => run(() => api.enrollTotpStart({ signedIn: true })),
    // Its answer is the authenticator secret.
    gcTime: 0,
  });
}

/** The second half: a code the app shows proves the key arrived; with the app already set up, it replaces it. */
export function useConfirmTotpMutation(run: StepUpRun) {
  const api = useApi();
  const refetch = useRefetchSecurity();
  return useMutation<void, Error, string>({
    mutationFn: async (code) => {
      await run(() => api.enrollTotpConfirm(code, { signedIn: true }));
    },
    onSuccess: refetch,
    // Its body is a code from the app.
    gcTime: 0,
  });
}

export function useRemoveTotpMutation(run: StepUpRun) {
  const api = useApi();
  const refetch = useRefetchSecurity();
  return useMutation<void, Error, void>({
    mutationFn: () => run(() => api.removeTotp()),
    onSuccess: refetch,
  });
}

export function useRegenerateRecoveryCodesMutation(run: StepUpRun) {
  const api = useApi();
  const refetch = useRefetchSecurity();
  return useMutation<RecoveryCodes, Error, void>({
    mutationFn: () => run(() => api.regenerateRecoveryCodes()),
    onSuccess: refetch,
    // Its answer is the ten codes.
    gcTime: 0,
  });
}

export function useRevokeTrustedDeviceMutation(run: StepUpRun) {
  const api = useApi();
  const refetch = useRefetchSecurity();
  return useMutation<void, Error, string>({
    mutationFn: (deviceId) => run(() => api.revokeTrustedDevice(deviceId)),
    onSuccess: refetch,
  });
}

export function useEndMySessionMutation(run: StepUpRun) {
  const api = useApi();
  const refetch = useRefetchSecurity();
  return useMutation<void, Error, string>({
    mutationFn: (sessionId) => run(() => api.endMySession(sessionId)),
    onSuccess: refetch,
  });
}

/**
 * Unlink the Microsoft account (own-auth phase 2): a recent confirmation,
 * never the last way in (409 `last_method`). Linking is no mutation: it
 * leaves for Microsoft (`modules/auth/MicrosoftButton.tsx`).
 */
export function useUnlinkMicrosoftMutation(run: StepUpRun) {
  const api = useApi();
  const refetch = useRefetchSecurity();
  return useMutation<void, Error, void>({
    mutationFn: () => run(() => api.unlinkMicrosoft()),
    onSuccess: refetch,
  });
}
