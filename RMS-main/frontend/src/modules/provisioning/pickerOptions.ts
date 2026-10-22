/**
 * The two lists this module makes somebody choose from, as picker options.
 *
 * WHY THIS FILE (UX remediation cycles 6 and 7). Auditor A's BLOCKER on this
 * module: "Provisioning cannot be started — the Profile dropdown is empty ...
 * With no profile selectable, the 'Start' action cannot be completed ...
 * Nothing on the page or in the dialog explains that profiles must be created
 * first, and there is no link to the 'Profiles' tab that sits just above."
 *
 * So the empty case is the load-bearing one here, not the populated one, and
 * it is why these hints live beside the options rather than inline in JSX:
 * the sentence a fresh install shows is the whole fix, and a sentence in a
 * file of its own can be asserted by a test.
 *
 * Pure functions, no fetching — `components/picker.tsx` explains why the data
 * stays at the call site.
 */

import type { PickerOption } from "../../components/picker";
import { translate, type MessageKey } from "../../i18n/core";
import { humaniseEnum, humaniseStatus } from "../../lib/labels";
import type { OsImage, ProvisionProfile } from "./types";

/**
 * WHAT WOULD FILL THESE LISTS. Both name the console surface that populates
 * the list, and both admit the second reason a list can be empty: these calls
 * carry `provisioning.read`/`provisioning.manage` independently of
 * `provisioning.run`, so "there are none" and "you may not see them" are both
 * live and the hint must not assert only the first.
 */
export const profilePickerEmptyHint = (): string => translate("provisioning.picker.profileEmptyHint");

export const imagePickerEmptyHint = (): string => translate("provisioning.picker.imageEmptyHint");

/**
 * The same two sentences as a load-time snapshot, kept only because the
 * module's unit test imports the names. Components call the functions above,
 * which follow the language.
 */
export const PROFILE_PICKER_EMPTY_HINT = profilePickerEmptyHint();
export const IMAGE_PICKER_EMPTY_HINT = imagePickerEmptyHint();

/** A wire word as the sublabel prints it (lower case), or as it came when the catalogue has none. */
function sublabelWord(group: "kind" | "state", value: string): string {
  const key = `provisioning.picker.${group}.${value}` as MessageKey;
  const word = translate(key);
  return word === key ? value : word;
}

/** The name an operator recognises, or the id if a profile somehow has none. */
export function profileLabel(profile: ProvisionProfile): string {
  return profile.name || profile.id;
}

/**
 * A profile, with the two facts that decide whether it is the right one.
 *
 * THE IMAGE IS THE DISTINGUISHING FACT. Profiles are named for their intent
 * ("standard-workstation", "kiosk") and two of them differ by which OS image
 * they lay down — which is the difference between a reimaged laptop running
 * Windows 11 and one running the Linux build. `enabled` is the second: a
 * disabled profile is one an operator deliberately took out of service, and
 * starting a run from it is a mistake worth seeing before the click, not
 * after the 422.
 */
export function profilePickerOptions(
  profiles: readonly ProvisionProfile[],
  images: readonly OsImage[],
): PickerOption[] {
  return profiles.map((profile) => {
    const image = images.find((candidate) => candidate.id === profile.image_id);
    // Falls back to the raw image id, never to blank: an id whose row has not
    // loaded (or that the caller may not read) is still more than nothing, and
    // printing nothing would make two profiles look identical.
    const imageName = image?.name || profile.image_id;
    return {
      id: profile.id,
      label: profileLabel(profile),
      sublabel: `${imageName} · ${profile.enabled ? translate("provisioning.picker.enabled") : translate("provisioning.picker.disabled")}`,
    };
  });
}

/**
 * An OS image, with what tells two builds apart.
 *
 * `os_family · kind · state` — a "windows · install · active" is not the same
 * choice as a "windows · answer_file · draft", and binding a profile to a
 * draft image is a build that fails at PXE time rather than at edit time.
 */
export function imagePickerOptions(images: readonly OsImage[]): PickerOption[] {
  return images.map((image) => ({
    id: image.id,
    label: image.name || image.id,
    sublabel: [image.os_family, sublabelWord("kind", image.kind), sublabelWord("state", image.state)]
      .filter(Boolean)
      .join(" · "),
  }));
}

/**
 * The name behind an id that is already stored on a row.
 *
 * Auditor A on this module's own runs table: every run identifies its profile
 * by raw UUID. Falls back to the id rather than to "—": a run that HAS a
 * profile must not render as one that does not.
 */
export function nameForOption(
  options: readonly PickerOption[],
  id: string | null | undefined,
): string | null {
  if (!id) return null;
  return options.find((option) => option.id === id)?.label ?? id;
}

/**
 * The platform as a person writes it. `humaniseEnum("macos")` gives "Macos",
 * which nobody has ever written; the closed set the server accepts is small
 * enough to spell out, and anything else is shown as it came.
 */
const OS_FAMILY_LABELS: Record<string, string> = {
  windows: "Windows",
  linux: "Linux",
  macos: "macOS",
};

export function osFamilyLabel(osFamily: string): string {
  return OS_FAMILY_LABELS[osFamily.toLowerCase()] ?? osFamily;
}

/**
 * An image's state as a pill reads it. The two states the console knows agree
 * with "imagen"; any other falls back to the shared status vocabulary, then to
 * the derived label, so no wire word is ever printed raw.
 */
export function osImageStateLabel(state: string): string {
  return state === "active" || state === "draft"
    ? humaniseEnum(state, "OsImageState")
    : humaniseStatus(state);
}
