/**
 * The sign-in pages under a router, over a fake `ApiClient` whose every
 * method is a `vi.fn` (`calls`). Each test file mocks `lib/ApiContext` to hand
 * out `authApi()` -- `vi.mock` is hoisted per file, so it cannot live here --
 * and spies on `navigation` for the whole-document loads (`enterConsole`).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderResult } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation, type Location } from "react-router-dom";
import { vi } from "vitest";

import type { ApiClient } from "../../../lib/api";
import { EnrollPage } from "../EnrollPage";
import { FactorPage } from "../FactorPage";
import { ForgotPasswordPage } from "../ForgotPasswordPage";
import { InvitePage } from "../InvitePage";
import { RecoveryCodesPage } from "../RecoveryCodesPage";
import { ResetPasswordPage } from "../ResetPasswordPage";
import { SignInPage } from "../SignInPage";

export const calls = {
  login: vi.fn(),
  factorTotp: vi.fn(),
  factorPasskeyOptions: vi.fn(),
  factorPasskey: vi.fn(),
  factorRecovery: vi.fn(),
  enrollTotpStart: vi.fn(),
  enrollTotpConfirm: vi.fn(),
  enrollPasskeyOptions: vi.fn(),
  enrollPasskey: vi.fn(),
  enrollFinish: vi.fn(),
  forgotPassword: vi.fn(),
  resetPassword: vi.fn(),
  getInvite: vi.fn(),
  acceptInvite: vi.fn(),
};

export const TEN_CODES = [
  "7K3QD-9XM2P", "H4WTR-2N8VB", "Q6ZJC-5PF1K", "M9XED-3GH7A", "T2BVN-8KR4W",
  "C5YQP-1DJ6M", "R8HFW-4TZ2E", "N3KGA-7VX9Q", "W1PDM-6CB5T", "F7JRE-2QY8H",
];

export const OTPAUTH =
  "otpauth://totp/ITMS:dana%40example.test?secret=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP&issuer=ITMS";

/** Every method back to a successful answer, as the server gives it. */
export function installDefaults(): void {
  for (const fn of Object.values(calls)) fn.mockReset();
  calls.login.mockResolvedValue({ state: "factor_required", methods: ["passkey", "totp", "recovery"], remember_device_days: 15 });
  calls.factorTotp.mockResolvedValue({ state: "signed_in" });
  calls.factorPasskeyOptions.mockResolvedValue({ challenge: "c2lnbi1pbg", userVerification: "required" });
  calls.factorPasskey.mockResolvedValue({ state: "signed_in" });
  calls.factorRecovery.mockResolvedValue({ state: "signed_in" });
  calls.enrollTotpStart.mockResolvedValue({ otpauth_uri: OTPAUTH });
  calls.enrollTotpConfirm.mockResolvedValue(undefined);
  calls.enrollPasskeyOptions.mockResolvedValue({ challenge: "cmVnaXN0ZXI", rp: { name: "ITMS" } });
  calls.enrollPasskey.mockResolvedValue(undefined);
  calls.enrollFinish.mockResolvedValue({ recovery_codes: TEN_CODES });
  calls.forgotPassword.mockResolvedValue(undefined);
  calls.resetPassword.mockResolvedValue(undefined);
  calls.getInvite.mockResolvedValue({ email: "dana@example.test", display_name: "Dana Okafor" });
  calls.acceptInvite.mockResolvedValue(undefined);
}

export function authApi(): ApiClient {
  return calls as unknown as ApiClient;
}

/** Where the router is now. Set by `LocationProbe`, which renders nothing. */
export const where: { location: Location | null } = { location: null };

function LocationProbe() {
  where.location = useLocation();
  return null;
}

/** The seven pages, at `path` (fragment included), with router `state` when a step hands one over. */
export function renderAuth(path: string, state?: unknown): RenderResult {
  const url = new URL(path, "https://itms.example");
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[{ pathname: url.pathname, search: url.search, hash: url.hash, state }]}>
        <Routes>
          <Route path="/sign-in" element={<SignInPage />} />
          <Route path="/sign-in/factor" element={<FactorPage />} />
          <Route path="/sign-in/enroll" element={<EnrollPage />} />
          <Route path="/sign-in/recovery-codes" element={<RecoveryCodesPage />} />
          <Route path="/forgot-password" element={<ForgotPasswordPage />} />
          <Route path="/reset-password" element={<ResetPasswordPage />} />
          <Route path="/invite" element={<InvitePage />} />
          <Route path="*" element={<p data-testid="elsewhere" />} />
        </Routes>
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
