import { describe, expect, it } from "vitest";

import {
  hasRoutableAddress,
  interfaceName,
  isVirtualInterface,
  orderHardware,
  presentAttributes,
  splitHardware,
} from "./hardware";

/** A NIC as the agent emits it: MAC in `identifier`, name in `attributes`. */
function nic(name: string, addresses?: unknown[]) {
  return {
    type: "nic",
    identifier: `mac-${name}`,
    attributes: addresses ? { name, addresses } : { name },
  };
}

const ROUTABLE = [{ family: "ipv4", origin: "static", address: "172.22.104.28/29" }];
const LINK_LOCAL = [
  { family: "ipv6", origin: "link_local", address: "fe80::3ea8:2aff:fe15:6b11%eno2/64" },
];

describe("interfaceName", () => {
  it("prefers the name the operator sees over the MAC the row is keyed by", () => {
    expect(interfaceName(nic("eno2"))).toBe("eno2");
  });

  it("falls back to the identifier when the agent sent no name", () => {
    // Windows reports a friendly name AS the identifier, and a disk has no
    // `name` at all — neither may render blank.
    expect(interfaceName({ type: "disk", identifier: "C:", attributes: {} })).toBe("C:");
    expect(interfaceName({ type: "nic", identifier: "eth9", attributes: null })).toBe("eth9");
  });
});

describe("isVirtualInterface", () => {
  it("recognises the audit's own three families of plumbing", () => {
    expect(isVirtualInterface(nic("veth7670993"))).toBe(true);
    expect(isVirtualInterface(nic("docker0"))).toBe(true);
    expect(isVirtualInterface(nic("br-1a2b3c"))).toBe(true);
  });

  it("leaves real interfaces alone", () => {
    for (const name of ["eno1", "eno2", "eth0", "en0", "wlan0", "lo", "bond0"]) {
      expect(isVirtualInterface(nic(name))).toBe(false);
    }
  });

  it("never hides something that is not a network interface", () => {
    // A disk called `dockerVolume` is still a disk.
    expect(
      isVirtualInterface({ type: "disk", identifier: "d1", attributes: { name: "docker0" } }),
    ).toBe(false);
  });

  it("matches on the name even when the identifier is a MAC", () => {
    // The whole reason this reads `attributes.name`: on Linux the identifier
    // is `3c:a8:2a:15:6b:11`, which matches no prefix at all.
    expect(isVirtualInterface({ type: "nic", identifier: "3c:a8:2a:15:6b:11", attributes: { name: "veth9" } })).toBe(true);
  });
});

describe("hasRoutableAddress", () => {
  it("is true for an interface holding a real address", () => {
    expect(hasRoutableAddress(nic("eno1", ROUTABLE))).toBe(true);
  });

  it("is false for one carrying only a link-local IPv6", () => {
    expect(hasRoutableAddress(nic("veth1", LINK_LOCAL))).toBe(false);
  });

  it("is false when there is no address block at all", () => {
    expect(hasRoutableAddress(nic("docker0"))).toBe(false);
  });

  it("ignores an fe80 address whose origin the agent did not label", () => {
    expect(hasRoutableAddress(nic("veth1", [{ family: "ipv6", address: "fe80::1" }]))).toBe(
      false,
    );
  });
});

describe("splitHardware", () => {
  const rows = [
    { type: "cpu", identifier: "cpu0", attributes: { cores: 12 } },
    nic("veth7670993", LINK_LOCAL),
    nic("eno1", ROUTABLE),
    nic("docker0"),
  ];

  it("keeps container plumbing out of the default view", () => {
    const { visible, virtual } = splitHardware(rows);
    expect(visible.map((row) => interfaceName(row))).toEqual(["cpu0", "eno1"]);
    expect(virtual.map((row) => interfaceName(row))).toEqual(["veth7670993", "docker0"]);
  });

  it("loses nothing — every row lands in exactly one half", () => {
    const { visible, virtual } = splitHardware(rows);
    expect(visible.length + virtual.length).toBe(rows.length);
  });

  it("does not reorder the caller's array in place", () => {
    const original = [...rows];
    splitHardware(rows);
    expect(rows).toEqual(original);
  });
});

describe("orderHardware", () => {
  it("puts NICs that are on a network above NICs that are not", () => {
    const ordered = orderHardware([nic("veth1", LINK_LOCAL), nic("eno1", ROUTABLE)]);
    expect(ordered.map((row) => interfaceName(row))).toEqual(["eno1", "veth1"]);
  });

  it("keeps non-NIC components above every interface", () => {
    const ordered = orderHardware([
      nic("eno1", ROUTABLE),
      { type: "memory", identifier: "dimm0", attributes: {} },
    ]);
    expect(ordered.map((row) => row.type)).toEqual(["memory", "nic"]);
  });

  it("is stable within a rank, so a sweep does not reshuffle itself", () => {
    const ordered = orderHardware([nic("eno2", ROUTABLE), nic("eno1", ROUTABLE)]);
    expect(ordered.map((row) => interfaceName(row))).toEqual(["eno2", "eno1"]);
  });
});

describe("presentAttributes", () => {
  it("names a unit once, and after the number", () => {
    // "Size bytes 512 GiB" is what the caption rule and the value rule produce
    // together on one key: the unit twice, in two different units, in the
    // wrong order.
    expect(presentAttributes({ size_bytes: 512 * 1024 ** 3 })).toEqual({ Size: "512 GiB" });
    expect(presentAttributes({ vram_bytes: 0 })).toEqual({ "Video memory": "0 B" });
  });

  it("reads a link speed as the port an operator calls it", () => {
    expect(presentAttributes({ speed_mbps: 1000 })).toEqual({ "Link speed": "1 Gbit/s" });
    expect(presentAttributes({ speed_mbps: 100 })).toEqual({ "Link speed": "100 Mbit/s" });
  });

  it("passes every other key through untouched", () => {
    // A field the agent starts reporting tomorrow must appear the moment it is
    // sent, not when this file is next edited.
    const attributes = { model: "Samsung PM9A1", encrypted: true, mtu: 1500 };
    expect(presentAttributes(attributes)).toEqual(attributes);
  });

  it("leaves a unit key alone when it does not hold a number", () => {
    // Formatting it would be a claim about a value this cannot read.
    expect(presentAttributes({ size_bytes: "unknown" })).toEqual({ size_bytes: "unknown" });
  });

  it("returns a non-object unchanged rather than inventing a row", () => {
    expect(presentAttributes(null)).toBeNull();
    expect(presentAttributes([1, 2])).toEqual([1, 2]);
  });
});
