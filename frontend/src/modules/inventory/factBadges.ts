import { translate, type MessageKey } from "../../i18n/core";
import type { BadgeVariant } from "../jobs/jobState";

export interface FactBadge {
  label: string;
  variant: BadgeVariant;
}

// The words are catalogue keys, read when a badge is made, so they follow the language.
const KNOWN: Record<string, { on: MessageKey; off: MessageKey; offVariant: BadgeVariant }> = {
  disk_encryption: { on: "inventory.badges.encrypted", off: "inventory.badges.notEncrypted", offVariant: "warning" },
  firewall_enabled: { on: "inventory.badges.firewallOn", off: "inventory.badges.firewallOff", offVariant: "warning" },
  domain_joined: { on: "inventory.badges.domainJoined", off: "inventory.badges.notDomainJoined", offVariant: "muted" },
};

export function factBadge(key: string, value: unknown): FactBadge | null {
  const spec = KNOWN[key];
  if (!spec || typeof value !== "boolean") return null;
  return value
    ? { label: translate(spec.on), variant: "success" }
    : { label: translate(spec.off), variant: spec.offVariant };
}
