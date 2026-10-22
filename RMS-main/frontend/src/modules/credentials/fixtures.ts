/**
 * Fixtures for the credential vault: the profiles `/credentials` lists and
 * the three mutations the page can send.
 *
 * NO SECRET IS EVER LISTED. `Credential` carries `has_secret` and nothing
 * else about the value, exactly as the API does (invariant 3), and the
 * checkout below hands back a value that is obviously a fixture. The four
 * profiles cover every type the console knows, one scoped to a core group,
 * one used today, one never checked out.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import type { Credential } from "../../lib/api";
import { ApiError } from "../../lib/api";

const MINUTE = 60_000;
const ago = (minutes: number): string => new Date(Date.now() - minutes * MINUTE).toISOString();

interface Seed {
  id: string;
  name: string;
  type: string;
  group: string | null;
  /** Minutes ago it was stored. */
  created: number;
  /** Minutes ago it was last checked out, or null if never. */
  used: number | null;
}

const SEEDS: Seed[] = [
  { id: "cred-1", name: "Local administrator · workstations", type: "windows_admin", group: "grp-workstations", created: 26 * 24 * 60, used: 3 * 60 + 2 },
  { id: "cred-2", name: "Push install · rack B4", type: "push_install", group: "grp-servers", created: 6 * 24 * 60, used: null },
  { id: "cred-3", name: "Deploy key · Linux servers", type: "ssh_key", group: "grp-servers", created: 90 * 24 * 60, used: 2 * 24 * 60 + 40 },
  { id: "cred-4", name: "Domain join · corp.toeshee.example", type: "domain_join", group: null, created: 210 * 24 * 60, used: 11 * 24 * 60 },
];

function credential(seed: Seed): Credential {
  return {
    id: seed.id,
    name: seed.name,
    type: seed.type,
    device_group_id: seed.group,
    created_at: ago(seed.created),
    last_used_at: seed.used === null ? null : ago(seed.used),
    has_secret: true,
  };
}

const credentials = (): Credential[] => SEEDS.map(credential);

let minted = 0;

const fixtures: FixtureMap = {
  listCredentials: () => credentials(),

  createCredential: (body) => {
    minted += 1;
    return credential({
      id: `cred-new-${minted}`,
      name: body.name,
      type: body.type,
      group: null,
      created: 0,
      used: null,
    });
  },

  checkoutCredential: (id) => {
    if (!SEEDS.some((s) => s.id === id)) {
      throw new ApiError(404, JSON.stringify({ detail: "credential not found" }));
    }
    return { secret: "fixture-secret-not-a-real-value" };
  },

  deleteCredential: () => ({ deleted: true }),
};

export default fixtures;
