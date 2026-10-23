/**
 * Fixtures for the enrollment module: the token envelope and the two revokes.
 * The Certificates tab reads `listDevices` from `fixtures/core.ts`.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only.
 */

import type { FixtureMap } from "../../fixtures/client";
import type { EnrollmentTokenListItem } from "./types";

const HOUR = 60 * 60_000;
const at = (hours: number): string => new Date(Date.now() + hours * HOUR).toISOString();

const TOKENS: EnrollmentTokenListItem[] = [
  { id: "tok-lis-ws", name: "Workstations · Lisbon", expires_at: at(23 * 24), max_uses: 40, use_count: 14, revoked: false },
  { id: "tok-build", name: "Build runners", expires_at: at(5 * 24), max_uses: 10, use_count: 2, revoked: false },
  { id: "tok-porto", name: "Porto rollout", expires_at: at(-2 * 24), max_uses: 25, use_count: 25, revoked: false },
  { id: "tok-pilot", name: "Pilot ring (2025)", expires_at: at(-90 * 24), max_uses: 5, use_count: 5, revoked: true },
];

const listEnrollmentTokens: FixtureMap["listEnrollmentTokens"] = (params = {}) => {
  const now = Date.now();
  const stillUnlimited = (row: EnrollmentTokenListItem) =>
    row.max_uses == null && !row.revoked && new Date(row.expires_at).getTime() > now;
  let matching = params.include_revoked ? TOKENS : TOKENS.filter((row) => !row.revoked);
  if (params.unlimited_only) matching = matching.filter(stillUnlimited);
  const offset = params.offset ?? 0;
  const limit = params.limit ?? matching.length;
  return {
    items: matching.slice(offset, offset + limit),
    total: matching.length,
    revoked_count: TOKENS.filter((row) => row.revoked).length,
    unlimited_count: TOKENS.filter(stillUnlimited).length,
  };
};

const createEnrollmentToken: FixtureMap["createEnrollmentToken"] = (body) => ({
  id: `tok-${Date.now()}`,
  token: "ENR-fixture-7Qm2xVb9Lk4pR8sT1wYz3nHc6dJf0gA5",
  name: body.name,
  expires_at: at(body.expires_in_hours),
  max_uses: body.max_uses,
});

const revokeEnrollmentToken: FixtureMap["revokeEnrollmentToken"] = () => ({ revoked: true });
const revokeAgent: FixtureMap["revokeAgent"] = () => ({ revoked: true });

export const fixtures: FixtureMap = {
  listEnrollmentTokens,
  createEnrollmentToken,
  revokeEnrollmentToken,
  revokeAgent,
};

export default fixtures;
