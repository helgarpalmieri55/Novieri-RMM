/**
 * Wire vocabularies the device page names. They are unions here, not
 * `string`, so the enum-label coverage test can check that every value has a
 * label in both languages (`enums.<TypeName>.<value>`).
 */

/** `inventory_changes.change_type`: the backend writes the first five, the
 *  fixtures draw the other two. */
export type InventoryChangeType =
  | "os_updated"
  | "software_installed"
  | "software_added"
  | "software_updated"
  | "software_removed"
  | "fact_changed"
  | "hardware_changed";

/** `HardwareComponent.type`: the pattern `backend/app/modules/inventory/schema.py` accepts. */
export type HardwareComponentType =
  | "cpu"
  | "memory"
  | "disk"
  | "nic"
  | "gpu"
  | "display"
  | "battery"
  | "bios"
  | "motherboard";
