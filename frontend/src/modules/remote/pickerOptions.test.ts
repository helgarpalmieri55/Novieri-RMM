/**
 * The sublabel is the safety, so it is tested as data rather than inspected in
 * a rendered tree.
 *
 * `components/picker.tsx`'s header: the sublabel "is also what stops somebody
 * dispatching to a machine that cannot answer". On /remote that is literal —
 * Auditor A found the console offering a device that had been offline for
 * three days as a plain selectable option for a LIVE session.
 *
 * EVERY TEST BELOW WAS FALSIFIED: each expectation was checked against a
 * version of the function that dropped the fact it asserts (sublabel removed,
 * "cannot connect" removed, `has_secret` ignored) and confirmed RED first.
 */

import { describe, expect, it } from "vitest";

import type { Credential, Device } from "../../lib/api";
import {
  CREDENTIAL_PICKER_EMPTY_HINT,
  DEVICE_PICKER_EMPTY_HINT,
  credentialPickerOptions,
  agentTooOld,
  devicePickerOptions,
  unreachableReason,
} from "./pickerOptions";

function device(overrides: Partial<Device> = {}): Device {
  return {
    id: "d1",
    hostname: "workstation-1",
    fqdn: null,
    os_family: "windows",
    os_version: "11",
    architecture: "x64",
    device_group_id: null,
    status: "active",
    agent_status: "online",
    last_seen_at: null,
    cert_not_after: null,
    cert_state: null,
    cert_revoked: false,
    lifecycle: "in_service",
    asset_type: "computer",
    source: "agent",
    // An enrolled machine proved its own identity at enrolment; nobody
    // had to vouch for the row (asset-inventory spec §3).
    provenance: "agent",
    description: null,
    primary_ip: null,
    owner: null,
    location: null,
    ...overrides,
  };
}

function credential(overrides: Partial<Credential> = {}): Credential {
  return {
    id: "c1",
    name: "Domain support admin",
    type: "windows_admin",
    device_group_id: null,
    client_id: null,
    created_at: null,
    last_used_at: null,
    has_secret: true,
    ...overrides,
  };
}

describe("devicePickerOptions", () => {
  it("carries the agent state that decides whether a session can open", () => {
    expect(devicePickerOptions([device()])).toEqual([
      { id: "d1", label: "workstation-1", sublabel: "windows · online" },
    ]);
  });

  it("says what offline MEANS here, because remote control is synchronous", () => {
    // Deploy's picker says "linux · offline" and that is honest there — the
    // job waits in a queue. A remote session aimed at an offline agent cannot
    // ever complete, which is a different fact and gets different words.
    expect(
      devicePickerOptions([device({ os_family: "linux", agent_status: "offline" })]),
    ).toEqual([{ id: "d1", label: "workstation-1", sublabel: "linux · offline · cannot connect" }]);
  });

  it("distinguishes a device with no agent at all from one that is merely offline", () => {
    // A switch, or hardware still in a box (`Device.agent_status: null`).
    // Nothing to connect to, ever — not a device that might come back.
    expect(
      devicePickerOptions([device({ os_family: null, agent_status: null, hostname: "core-sw-1" })]),
    ).toEqual([{ id: "d1", label: "core-sw-1", sublabel: "no agent · cannot connect" }]);
  });

  it("falls back to the FQDN, and only then to the id, for a device with no hostname", () => {
    expect(devicePickerOptions([device({ hostname: null, fqdn: "x.example.com" })])).toEqual([
      { id: "d1", label: "x.example.com", sublabel: "windows · online" },
    ]);
    expect(devicePickerOptions([device({ hostname: null, fqdn: null })])).toEqual([
      { id: "d1", label: "d1", sublabel: "windows · online" },
    ]);
  });
});

describe("unreachableReason", () => {
  it("is null for a device that can answer", () => {
    expect(unreachableReason(device())).toBeNull();
  });

  it("explains an offline agent, and an absent one, differently", () => {
    expect(unreachableReason(device({ agent_status: "offline" }))).toMatch(/is offline/);
    expect(unreachableReason(device({ agent_status: null }))).toMatch(/has no agent/);
  });

  it("is null when nothing is chosen yet, rather than warning about a device that is not there", () => {
    expect(unreachableReason(undefined)).toBeNull();
  });

  // THE 409 BEFORE IT HAPPENS (cross-module-guidance §2 Remote #2): the
  // server refuses a screen session with "agent too old to share its screen"
  // on the same fact `screen_capable: false` carries.
  it("names an online agent too old to share its screen", () => {
    expect(unreachableReason(device({ screen_capable: false }))).toMatch(/too old to share its screen/);
  });

  it("reads an absent field as unknown, never as too old", () => {
    // A server older than the field omits it; a warning on every device
    // would be the console inventing a refusal.
    expect(unreachableReason(device({ screen_capable: undefined }))).toBeNull();
    expect(unreachableReason(device({ screen_capable: true }))).toBeNull();
  });
});

describe("agentTooOld", () => {
  it("is true only for an explicit false", () => {
    expect(agentTooOld(device({ screen_capable: false }))).toBe(true);
    expect(agentTooOld(device({ screen_capable: true }))).toBe(false);
    expect(agentTooOld(device({ screen_capable: null }))).toBe(false);
    expect(agentTooOld(device())).toBe(false);
    expect(agentTooOld(undefined)).toBe(false);
  });
});

describe("credentialPickerOptions", () => {
  it("labels the type as a person reads it, never as the enum", () => {
    expect(credentialPickerOptions([credential()])).toEqual([
      { id: "c1", label: "Domain support admin", sublabel: "Windows administrator" },
    ]);
  });

  it("flags a profile the vault holds no secret for", () => {
    // Selecting it starts a session that cannot authenticate — the one fact
    // that tells two similarly named profiles apart.
    expect(
      credentialPickerOptions([
        credential({ type: "ssh_key", name: "Domain support key", has_secret: false }),
      ]),
    ).toEqual([
      { id: "c1", label: "Domain support key", sublabel: "SSH key · no secret stored" },
    ]);
  });
});

describe("the empty hints", () => {
  // AUDIT [MEDIUM]: "nothing explains the absence or links to /credentials to
  // add one". A hint that only said "none" would be the dead end wearing a
  // nicer control.
  it("name the console surface that would fill the list", () => {
    expect(DEVICE_PICKER_EMPTY_HINT).toMatch(/Devices → Add a computer/);
    expect(CREDENTIAL_PICKER_EMPTY_HINT).toMatch(/Fleet → Credentials/);
  });

  it("admit the other live reason a list can be empty", () => {
    // These reads carry their own permissions, so "there are none" is not the
    // only explanation and the hint must not assert it as though it were.
    expect(DEVICE_PICKER_EMPTY_HINT).toMatch(/permission/);
    expect(CREDENTIAL_PICKER_EMPTY_HINT).toMatch(/permission/);
  });
});
