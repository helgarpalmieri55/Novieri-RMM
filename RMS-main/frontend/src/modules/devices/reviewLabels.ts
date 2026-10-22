/**
 * Waiting for review, in the operator's words (install-link spec §1.5).
 * `review_state` is a wire value and never reaches the screen as itself.
 */

import { translate } from "../../i18n/core";
import type { Device, DeviceReviewState } from "../../lib/api";
import { humaniseEnum } from "../../lib/labels";

/** A review state as a person reads it, from `enums.DeviceReviewState`. */
export function reviewStateLabel(state: NonNullable<Device["review_state"]>): string {
  return humaniseEnum(state satisfies DeviceReviewState, "DeviceReviewState");
}

export function isWaitingForReview(device: Device): boolean {
  return device.review_state === "pending";
}

/**
 * What is left to do after a rejection, or null when nothing is. The server
 * lists a rejected row only when its agent may still be on the computer; the
 * outcome is a wire value and reaches the screen as this sentence.
 */
export function manualRemovalStatus(device: Device): string | null {
  if (device.review_state !== "rejected") return null;
  switch (device.rejection_outcome) {
    case "timeout":
    case "agent_did_not_confirm":
      return translate("devices.review.didNotConfirm");
    case "agent_cannot_uninstall":
      return translate("devices.review.tooOld");
    default:
      return null;
  }
}

/** "Joined through the install link from 10.50.0.9", or null for any other row. */
export function joinedThroughLink(device: Device): string | null {
  if (!device.link_joined_at) return null;
  return device.link_joined_from
    ? translate("devices.review.joinedFrom", { from: device.link_joined_from })
    : translate("devices.review.joined");
}

/** The "Recorded by" cell: the install link names itself; everything else keeps its label. */
export function recordedByLabel(device: Device, fallback: string): string {
  return device.link_joined_at ? translate("devices.review.installLink") : fallback;
}
