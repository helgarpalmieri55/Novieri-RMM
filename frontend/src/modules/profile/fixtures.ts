/**
 * Fixtures for the profile page: the one write it makes.
 *
 * `me` itself is the shell's, in `fixtures/core.ts` -- every route reads it
 * -- so this file answers only `PATCH /me`, and answers it from that same
 * record so the name in the top bar and the name in the form agree. The
 * server writes the four profile fields and nothing else (spec §7.3), and so
 * does this: permissions, roles and the address are untouched by the body.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import { fixtures as core } from "../../fixtures/core";
import type { Me } from "../../lib/api";

/** The stored profile fields, once a save has replaced the seed's. */
let stored: Partial<Pick<Me, "display_name" | "phone" | "timezone" | "locale">> = {};

const updateMe: FixtureMap["updateMe"] = async (body) => {
  const current = await core.me!();
  stored = {
    display_name: body.display_name,
    phone: body.phone ?? undefined,
    timezone: body.timezone ?? undefined,
    locale: body.locale ?? undefined,
  };
  return { ...current, ...stored };
};

const fixtures: FixtureMap = { updateMe };

export default fixtures;
