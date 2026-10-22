/**
 * The two lists the "Start remote session" dialog points at, as options.
 *
 * WHY THIS FILE (UX remediation cycles 6, 7 and 8). One audit finding is the
 * whole reason the device list needs a sublabel at all:
 *
 *   Auditor A, [HIGH], /remote → Start remote session: "The stat card reads
 *   'Online devices 0', yet the Device dropdown offers `Toeshee-sentinel` —
 *   the fleet's only device, which has been offline since 8/18. Nothing marks
 *   it as offline in the dropdown ... Offering an offline device for a *live
 *   interactive* session guarantees a failed or indefinitely hanging
 *   connection with no explanation."
 *
 * and a second is why the credential list may not render as a lone "None":
 *
 *   Auditor A, [MEDIUM], same dialog: "The select contains a single option,
 *   'None' ... no vaulted credential is selectable and nothing explains the
 *   absence or links to /credentials to add one."
 *
 * Pure functions, out of the component, because the SUBLABEL is the part that
 * carries the safety here — `components/picker.tsx`'s own header says the
 * sublabel "is also what stops somebody dispatching to a machine that cannot
 * answer" — and it deserves tests rather than an inspection of a JSX tree.
 *
 * Deliberately this module's own copy rather than an import from
 * `deploy/pickerOptions.ts`: remote is SYNCHRONOUS. A deployment aimed at an
 * offline agent sits in a queue and runs later; a remote session aimed at one
 * cannot ever complete, so this file's sublabel says "cannot connect" where
 * deploy's says only "offline". If a third module needs this exact wording,
 * that is the moment it moves to `lib/`.
 */

import type { PickerOption } from "../../components/picker";
import { translate } from "../../i18n/core";
import type { Credential, Device } from "../../lib/api";
import { credentialTypeLabel } from "../credentials/credentialTypes";

/**
 * WHAT WOULD FILL THE LIST. A picker that is empty on a fresh install and
 * says nothing is the blocked flow wearing a nicer control. Both hints name
 * the console surface that populates the list AND the other live reason it
 * can be empty — these reads carry their own permissions (`devices.read`,
 * `credentials.read`), so "there are none" and "you may not see them" are
 * both possible and the hint must not assert only the first.
 */
export function devicePickerEmptyHint(): string {
  return translate("remote.picker.deviceEmptyHint");
}

export function credentialPickerEmptyHint(): string {
  return translate("remote.picker.credentialEmptyHint");
}

// The two constants below are the hints as worded when this module loaded
// (English, in the tests that read them). The interface does NOT read them: a
// constant holds one language for good, so the dialog calls the functions above
// at render and follows a language switch.
export const DEVICE_PICKER_EMPTY_HINT = devicePickerEmptyHint();

export const CREDENTIAL_PICKER_EMPTY_HINT = credentialPickerEmptyHint();

/**
 * The device's agent predates screen share: `screen_capable` is an explicit
 * `false` (`GET /devices`, derived by the rule the server refuses by, 409
 * "agent too old to share its screen"). Absent -- an older server -- or null
 * -- no agent -- is NOT too old; the first is unknown, the second has its own
 * sentence above.
 */
export function agentTooOld(device: Device | undefined): boolean {
  return device?.screen_capable === false;
}

/** What a device that cannot answer a live connection is, and why. */
export function unreachableReason(device: Device | undefined): string | null {
  if (!device) return null;
  if (device.agent_status === null) {
    return translate("remote.picker.unreachableNoAgent");
  }
  if (device.agent_status !== "online") {
    return translate("remote.picker.unreachableOffline");
  }
  if (agentTooOld(device)) {
    return translate("remote.picker.unreachableTooOld");
  }
  return null;
}

/**
 * An endpoint, with the fact that decides whether a session can open at all.
 *
 * `os_family · agent status` — the picker header's own example, with the
 * consequence spelled out rather than left to be inferred from the word
 * "offline". The audit's fix line reads: "Disable offline devices in the
 * picker (or annotate them 'offline — cannot connect') and show agent state
 * inline." The picker takes no per-option disabled flag (and adding one is a
 * change to a shared component), so this is the annotation half; the dialog
 * pairs it with a confirmation that names the same fact before it dispatches.
 */
export function devicePickerOptions(devices: readonly Device[]): PickerOption[] {
  return devices.map((device) => {
    const status =
      device.agent_status === "online"
        ? agentTooOld(device)
          ? translate("remote.picker.statusOnlineTooOld")
          : translate("remote.picker.statusOnline")
        : device.agent_status === null
          ? translate("remote.picker.statusNoAgent")
          : translate("remote.picker.statusOffline");
    const parts = [device.os_family, status].filter((part): part is string => Boolean(part));
    return {
      id: device.id,
      label: device.hostname ?? device.fqdn ?? device.id,
      sublabel: parts.join(" · "),
    };
  });
}

/**
 * A vaulted credential, with the fact that decides whether it is the right one.
 *
 * TYPE, because "Domain support admin" and "Domain support key" are one
 * keystroke apart in a list and authenticate against entirely different
 * things; and whether the vault actually holds bytes for it — a profile with
 * `has_secret: false` is a reference to a secret that was never stored, and
 * selecting it starts a session that cannot authenticate.
 */
export function credentialPickerOptions(credentials: readonly Credential[]): PickerOption[] {
  return credentials.map((credential) => ({
    id: credential.id,
    label: credential.name,
    sublabel: credential.has_secret
      ? credentialTypeLabel(credential.type)
      : translate("remote.picker.noSecret", { type: credentialTypeLabel(credential.type) }),
  }));
}
