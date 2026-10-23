/**
 * AUDIT [MEDIUM], /credentials → Type: "The options are `windows_admin`,
 * `ssh_key`, `domain_join` — unmapped database enum values with underscores
 * ... an operator choosing between 'ssh_key' and 'domain_join' gets no
 * explanation of what each type implies for how the secret is used or where it
 * can be checked out."
 *
 * EVERY TEST BELOW WAS FALSIFIED: the map was reduced to a bare
 * `value.replace("_", " ")` pretty-printer with no descriptions, each test
 * confirmed RED, then the map restored.
 */

import { describe, expect, it } from "vitest";

import {
  CREDENTIAL_TYPES,
  credentialTypeDescription,
  credentialTypeLabel,
  windowsDomainRealm,
} from "./credentialTypes";

describe("credentialTypeLabel", () => {
  it("renders the three stored types as a person reads them", () => {
    expect(credentialTypeLabel("windows_admin")).toBe("Windows administrator");
    expect(credentialTypeLabel("ssh_key")).toBe("SSH key");
    expect(credentialTypeLabel("domain_join")).toBe("Domain join account");
  });

  it("humanises a type the console has not been taught, rather than hiding it", () => {
    // The server owns this enum and may grow it. A blank badge would hide
    // WHICH credential a row is, which is worse than the snake_case it
    // replaces.
    expect(credentialTypeLabel("smart_card")).toBe("Smart card");
    expect(credentialTypeLabel("")).toBe("");
  });
});

describe("credentialTypeDescription", () => {
  it("says what choosing a type means for how its secret is used", () => {
    // The half of the finding the auditor called the one that matters.
    expect(credentialTypeDescription("ssh_key")).toMatch(/SSH sessions to Linux and macOS/);
    expect(credentialTypeDescription("domain_join")).toMatch(/not by interactive remote sessions/);
  });

  it("is null for an unknown type, so nothing is invented about it", () => {
    expect(credentialTypeDescription("smart_card")).toBeNull();
  });

  it("gives every offered type a description, so the dropdown never explains only some of itself", () => {
    for (const type of CREDENTIAL_TYPES) {
      expect(credentialTypeDescription(type.value)).toBeTruthy();
    }
  });

  it("labels windows_domain and carries the standing Domain-Admin warning", () => {
    expect(credentialTypeLabel("windows_domain")).toBe("Windows domain account");
    expect(credentialTypeDescription("windows_domain")).toMatch(/NOT Domain Admin/);
  });
});

describe("windowsDomainRealm", () => {
  // Design §2.5: "The realm is derived from the domain and shown for
  // confirmation." The agent's `RealmFor` is the rule this mirrors: a UPN
  // names its own realm; `DOMAIN\user` does NOT -- a NetBIOS name is not a
  // Kerberos realm, so the site server takes it from each target's DNS name.
  it("reads the realm off a UPN, upper-cased", () => {
    expect(windowsDomainRealm("svc-push@corp.example.com")).toBe("CORP.EXAMPLE.COM");
    expect(windowsDomainRealm(" svc-push@Corp.Example.com ")).toBe("CORP.EXAMPLE.COM");
  });

  it("does not pass a NetBIOS domain off as the realm", () => {
    expect(windowsDomainRealm("CORP\\svc-push")).toBeNull();
    expect(windowsDomainRealm("")).toBeNull();
    expect(windowsDomainRealm("svc-push@")).toBeNull();
  });
});
