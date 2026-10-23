/**
 * Which hardware rows an operator came here to read, and which are plumbing.
 *
 * WHY THIS EXISTS (UX remediation cycle 12). Auditor A, on the Hardware tab:
 *
 *   "[MEDIUM] Hardware inventory is 91% network interfaces, 20 of which are
 *    Docker veth pairs … The signal an operator needs — 'what are this
 *    machine's addresses?' — is diluted 1:8 by container plumbing that changes
 *    on every container restart, making the inventory both unreadable and
 *    unstable (a diff between two sweeps would show dozens of spurious
 *    changes). The four real physical interfaces (eno1–eno4, the ones with
 *    actual IPv4 addresses) are buried among them in MAC-address sort order."
 *
 * HIDDEN, NEVER DROPPED. The rows are still collected, still counted and one
 * click away — a veth pair carrying an unexpected address is exactly the kind
 * of thing somebody eventually needs to see, and an inventory that silently
 * omits records is a worse defect than one that is noisy.
 *
 * THE NAME IS IN `attributes.name`, NOT IN `identifier`. The agent sets
 * `identifier` to the MAC where it has one and falls back to the interface
 * name (`agent/internal/capabilities/inventory/nic.go#nicComponentFrom`), so
 * matching on the identifier alone would classify nothing on Linux — which is
 * precisely the host the audit was looking at.
 */

import { translate, type MessageKey } from "../../i18n/core";
import { formatBytes } from "../../lib/format";

/** The shape this file needs from `DeviceInventory.hardware`. */
export interface HardwareRow {
  type: string;
  identifier: string;
  attributes?: Record<string, unknown> | null;
}

/**
 * Interface-name prefixes that belong to container and hypervisor plumbing.
 *
 * `veth`, `docker` and `br-` are the audit's own three. `virbr`, `vnet`,
 * `tap` and `cni` are their libvirt and Kubernetes equivalents, which behave
 * identically — ephemeral, link-local only, renamed on every restart.
 *
 * LOOPBACK IS NOT HERE ON PURPOSE. `lo` is neither ephemeral nor noisy (there
 * is one of it), and hiding it would make the interface list disagree with
 * what `ip addr` shows on the machine.
 */
const VIRTUAL_PREFIXES = ["veth", "docker", "br-", "virbr", "vnet", "tap", "cni"] as const;

/** The name a person calls this interface: `eno2`, not `3c:a8:2a:15:6b:11`. */
export function interfaceName(row: HardwareRow): string {
  const name = row.attributes?.name;
  return typeof name === "string" && name !== "" ? name : row.identifier;
}

/** True for a container/hypervisor interface. Non-NIC rows are never virtual. */
export function isVirtualInterface(row: HardwareRow): boolean {
  if (row.type !== "nic") return false;
  const name = interfaceName(row).toLowerCase();
  return VIRTUAL_PREFIXES.some((prefix) => name.startsWith(prefix));
}

/**
 * True when this NIC holds an address something could route to.
 *
 * A link-local IPv6 (`fe80::…`) is what every veth carries and nothing else
 * needs, so it is the discriminator between "this interface is on a network"
 * and "this interface exists". Reads `origin`/`family`/`address` because the
 * agent emits all three (§14.1) and any one of them can be absent.
 */
export function hasRoutableAddress(row: HardwareRow): boolean {
  const addresses = row.attributes?.addresses;
  if (!Array.isArray(addresses)) return false;
  return addresses.some((entry) => {
    if (entry == null || typeof entry !== "object") return false;
    const record = entry as Record<string, unknown>;
    if (record.origin === "link_local") return false;
    const address = record.address;
    if (typeof address !== "string") return false;
    return !address.toLowerCase().startsWith("fe80:");
  });
}

/**
 * The rows in reading order: everything that is not a NIC first (the backend
 * already orders by type), then NICs that are on a network, then the rest.
 *
 * A COPY, and a stable sort. Mutating the query cache's array in place is how
 * a list reorders itself on a re-render that changed nothing.
 */
export function orderHardware<T extends HardwareRow>(rows: readonly T[]): T[] {
  const rank = (row: T) => (row.type !== "nic" ? 0 : hasRoutableAddress(row) ? 1 : 2);
  return [...rows].sort((a, b) => rank(a) - rank(b));
}

/** The rows to show by default, and the plumbing behind the toggle. */
export function splitHardware<T extends HardwareRow>(
  rows: readonly T[],
): { visible: T[]; virtual: T[] } {
  return {
    visible: orderHardware(rows.filter((row) => !isVirtualInterface(row))),
    virtual: orderHardware(rows.filter(isVirtualInterface)),
  };
}

/**
 * A hardware component's attributes as a person reads them, not as the agent
 * keys them.
 *
 * THE DEFECT THIS CLOSES. `<Attributes>` derives its caption from the wire key
 * (`humaniseKey`) and formats a value whose key ends `_bytes` (`describeValue`).
 * Both rules are right; applied to the same key they print the unit twice and
 * in the wrong order -- "Size bytes 512 GiB", "Vram bytes 0 B",
 * "Speed mbps 1000". The convention that a key names its unit is the wire's,
 * and this is the one place that translates it, so nothing downstream has to
 * know that `vram_bytes` is a video-memory size rather than a field called
 * "vram bytes".
 *
 * IT PRESENTS, IT DOES NOT FILTER. Every key survives; a key this table does
 * not know passes through untouched, so a field the agent starts reporting
 * tomorrow appears the moment it is sent rather than when this file is next
 * edited. The exact object is still one disclosure away -- the caller renders
 * "View raw" from the ORIGINAL, never from this copy.
 */
const UNIT_ATTRIBUTES: Record<string, { label: MessageKey; format: (value: number) => string }> = {
  // Bytes, through the same formatter the Memory fact uses.
  size_bytes: { label: "inventory.hardware.size", format: formatBytes },
  total_bytes: { label: "inventory.hardware.total", format: formatBytes },
  free_bytes: { label: "inventory.hardware.free", format: formatBytes },
  used_bytes: { label: "inventory.hardware.used", format: formatBytes },
  ram_bytes: { label: "inventory.hardware.memory", format: formatBytes },
  vram_bytes: { label: "inventory.hardware.video", format: formatBytes },
  // Link speed. A NIC reports megabits per second; 1000 of them is the
  // gigabit port everybody calls a gigabit port, so that is what it says.
  speed_mbps: {
    label: "inventory.hardware.linkSpeed",
    format: (value) =>
      value >= 1000 && value % 1000 === 0 ? `${value / 1000} Gbit/s` : `${value} Mbit/s`,
  },
};

export function presentAttributes(value: unknown): unknown {
  if (value == null || typeof value !== "object" || Array.isArray(value)) return value;
  const out: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    const unit = UNIT_ATTRIBUTES[key];
    // A unit key holding something other than a number is left exactly as it
    // arrived: the formatter would be a claim about a value it cannot read.
    if (unit && typeof item === "number" && Number.isFinite(item)) {
      out[translate(unit.label)] = unit.format(item);
    } else {
      out[key] = item;
    }
  }
  return out;
}
