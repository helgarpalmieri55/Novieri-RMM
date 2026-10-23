/**
 * Fixtures for the inventory module: the fleet-wide software catalogue and
 * one device's change history. The per-device inventory snapshot itself is
 * `getDeviceInventory` in `fixtures/core.ts`; the software rows below name
 * the same titles that snapshot installs, so the two screens agree.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only.
 */

import type { FixtureMap } from "../../fixtures/client";
import { devices } from "../../fixtures/core";
import type { InventoryChange, SoftwareAggregate } from "../../lib/api";

const MINUTE = 60_000;
const ago = (minutes: number): string => new Date(Date.now() - minutes * MINUTE).toISOString();

const SOFTWARE: SoftwareAggregate[] = [
  { name: "Google Chrome", version: "129.0.6668.70", publisher: "Google LLC", device_count: 9 },
  { name: "Google Chrome", version: "128.0.6613.138", publisher: "Google LLC", device_count: 2 },
  { name: "7-Zip", version: "24.08", publisher: "Igor Pavlov", device_count: 6 },
  { name: "Microsoft 365 Apps", version: "16.0.17928", publisher: "Microsoft", device_count: 7 },
  { name: "Zoom Workplace", version: "6.2.0", publisher: "Zoom Video Communications", device_count: 8 },
  { name: "Visual Studio Code", version: "1.93.1", publisher: "Microsoft", device_count: 4 },
  { name: "PostgreSQL", version: "16.4", publisher: "PostgreSQL Global Development Group", device_count: 1 },
  { name: "OpenSSH", version: "9.8p1", publisher: "OpenBSD", device_count: 5 },
];

const searchSoftware: FixtureMap["searchSoftware"] = (name) => {
  const needle = name.trim().toLowerCase();
  const items = SOFTWARE.filter(
    (s) => !needle || `${s.name} ${s.publisher} ${s.version}`.toLowerCase().includes(needle),
  );
  return { items, total: items.length };
};

/** The managed machines, as many as the aggregate claims, oldest version last. */
const softwareDevices: FixtureMap["softwareDevices"] = (name) => {
  const versions = SOFTWARE.filter((s) => s.name === name);
  const managed = devices().filter((d) => d.agent_status !== null && d.hostname);
  const count = Math.min(
    managed.length,
    versions.reduce((n, s) => n + s.device_count, 0) || 0,
  );
  return managed.slice(0, count).map((d, at) => ({
    device_id: d.id,
    hostname: d.hostname ?? d.id,
    version: versions[at % Math.max(1, versions.length)]?.version ?? "",
  }));
};

const getDeviceChanges: FixtureMap["getDeviceChanges"] = () => {
  const items: InventoryChange[] = [
    { id: "chg-1", ts: ago(38), change_type: "software_added", subject: "Zoom Workplace", from_value: null, to_value: "6.2.0" },
    { id: "chg-2", ts: ago(38), change_type: "software_updated", subject: "Google Chrome", from_value: "128.0.6613.138", to_value: "129.0.6668.70" },
    { id: "chg-3", ts: ago(3 * 24 * 60), change_type: "software_removed", subject: "Adobe Acrobat Reader", from_value: "24.002", to_value: null },
    { id: "chg-4", ts: ago(9 * 24 * 60), change_type: "hardware_changed", subject: "nic eth0", from_value: "100 Mb/s", to_value: "1000 Mb/s" },
  ];
  return { items, total: items.length };
};

export const fixtures: FixtureMap = {
  searchSoftware,
  softwareDevices,
  getDeviceChanges,
};

export default fixtures;
