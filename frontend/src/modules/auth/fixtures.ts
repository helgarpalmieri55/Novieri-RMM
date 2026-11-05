/**
 * Fixtures for the pages reached without a session (own-auth Task 11), for
 * the harmony gate and `?__fixtures` in development.
 *
 * Only `getInvite` and `getRecovery` are called when a page loads (`/invite`,
 * `/recover`, with any token -- harmony cannot add a fragment); the rest
 * answer a person clicking through in fixture mode, the way the server would:
 * a password step that asks for a factor, a factor that is accepted, a setup
 * key, ten codes. The codes and the key are INVENTED and follow the shapes the
 * server uses (`XXXXX-XXXXX`, base32), so a screenshot shows real line
 * lengths. Dev-only, like every `fixtures.ts`: nothing outside fixture mode
 * imports this file (`src/test/preview.test.ts`).
 */
import type { FixtureMap } from "../../fixtures/client";

const challenge = "q1mEu2YhQUi3_fixture_challenge_b64url";

export const fixtures: FixtureMap = {
  login: () => ({ state: "factor_required", methods: ["passkey", "totp", "recovery"], remember_device_days: 15 }),
  factorTotp: () => ({ state: "signed_in" }),
  factorPasskeyOptions: () => ({ challenge, rpId: "localhost", userVerification: "required", timeout: 60_000 }),
  factorPasskey: () => ({ state: "signed_in" }),
  factorRecovery: () => ({ state: "signed_in" }),
  enrollTotpStart: () => ({
    otpauth_uri: "otpauth://totp/ITMS:helgar.palmieri%40toeshee.example?secret=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP&issuer=ITMS",
  }),
  enrollTotpConfirm: () => undefined,
  enrollPasskeyOptions: () => ({
    rp: { id: "localhost", name: "ITMS" },
    user: { id: "dXNyLWhlbGdhcg", name: "helgar.palmieri@toeshee.example", displayName: "Helgar Palmieri" },
    challenge,
    pubKeyCredParams: [{ type: "public-key", alg: -7 }],
    authenticatorSelection: { residentKey: "preferred", userVerification: "required" },
    attestation: "none",
  }),
  enrollPasskey: () => undefined,
  enrollFinish: () => ({
    recovery_codes: [
      "7K3QD-9XM2P", "H4WTR-2N8VB", "Q6ZJC-5PF1K", "M9XED-3GH7A", "T2BVN-8KR4W",
      "C5YQP-1DJ6M", "R8HFW-4TZ2E", "N3KGA-7VX9Q", "W1PDM-6CB5T", "F7JRE-2QY8H",
    ],
  }),
  forgotPassword: () => undefined,
  resetPassword: () => undefined,
  getInvite: () => ({ email: "dana.okafor@toeshee.example", display_name: "Dana Okafor" }),
  acceptInvite: () => undefined,
  getRecovery: () => ({ email: "helgar.palmieri@toeshee.example" }),
  acceptRecovery: () => undefined,
  logoutEverywhere: () => undefined,
};
