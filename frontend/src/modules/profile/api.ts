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
 * The answers that carry a value shown once (the authenticator key, new
 * recovery codes) are not cached here: the page moves them into its own state
 * and empties the mutation.
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

/** A password change also forgets the remembered devices (design §2.6), so the page re-reads. */
export function useChangePasswordMutation(run: StepUpRun) {
  const api = useApi();
  const refetch = useRefetchSecurity();
  return useMutation<void, Error, PasswordChange>({
    mutationFn: (body) => run(() => api.changePassword(body)),
    onSuccess: refetch,
  });
}

/**
 * Add a passkey (design §2.2): the server's creation options -- user
 * verification required, the person's own passkeys excluded (Task 5) -- to
 * the browser, and the browser's answer back under the name given. Both
 * requests may need the recent confirmation; the browser's prompt between them
 * is the person's.
 */
export function useAddPasskeyMutation(run: StepUpRun) {
  const api = useApi();
  const refetch = useRefetchSecurity();
  return useMutation<void, Error, { name: string }>({
    mutationFn: async ({ name }) => {
      const optionsJSON = await run(() => api.enrollPasskeyOptions({ signedIn: true }));
      const credential = await createPasskey(optionsJSON);
      await run(() => api.enrollPasskey(credential, name, { signedIn: true }));
    },
    onSuccess: refetch,
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
