/**
 * What a backup run actually did, in a sentence.
 *
 * A run that dispatched nothing is the case this module must not round up:
 * BackupPointsPage's docstring is explicit that `backup.run` has never
 * completed end to end, so "queued" with no job behind it has to read as the
 * non-event it is rather than as a started backup.
 *
 * LIVES IN ITS OWN FILE because two controls dispatch the same run —
 * BackupPointsPage's "Run" and DeviceBackupTab's "Run now" — and the device
 * tab used to report nothing at all on success. Two copies of this sentence
 * would drift into two different claims about one API response.
 */

import { translate } from "../../i18n/core";
import type { RunResponse } from "./types";

export function runQueuedMessage(data: RunResponse | undefined): string {
  const count = data?.job_ids.length ?? 0;
  if (count === 0) return translate("backup.runMessage.none");
  return translate("backup.runMessage.queued", { count });
}
