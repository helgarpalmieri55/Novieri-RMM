import type { UseQueryResult } from "@tanstack/react-query";

import { useT } from "../../i18n/context";
import { Trans } from "../../i18n/Trans";
import { formatDateTime } from "../../lib/datetime";
import { Panel, StatusPill } from "../../shell/components";
import type { PatchRunListResponse } from "./types";

/**
 * THE ANSWER TO "I APPLIED THE UPDATES — DID THEY INSTALL?"
 *
 * That question had no answer on this module's own pages. `patch_runs` is
 * written on every dispatch and finalized on every result, and until now NO
 * route read it: the outcome of an apply was visible only in the job engine's
 * raw result JSON, on another page, if you knew the job id. The per-patch
 * states below this card are the durable truth and a different fact — they say
 * where the machine STANDS, not what the run the operator is waiting on did.
 *
 * THREE STATES, AND THE FIRST IS THE ONE THAT GETS MISREAD.
 *
 *   1. Dispatched, no result yet (`finished_at === null`). Zero installed,
 *      zero failed — the same numbers as a finished run that installed
 *      nothing, and the opposite meaning. It says "still running", never "0
 *      installed".
 *   2. Finished, nothing failed. Green, with the count and the time.
 *   3. Finished with failures. Every failure NAMED with the agent's own error
 *      string, because a count alone only tells the operator to go looking
 *      somewhere else — which is exactly where this module left them.
 *
 * No run at all renders nothing. An empty card saying "no applies" on a
 * machine that has never needed one is noise on the tab, and the standing
 * counts below already carry that story.
 */
export function LastApplyCard({
  runs,
  timeZone = null,
}: {
  runs: UseQueryResult<PatchRunListResponse>;
  timeZone?: string | null;
}) {
  const t = useT();
  const run = runs.data?.items?.[0];
  if (!run) return null;

  const running = run.finished_at == null;
  const failed = run.failed > 0;

  const state = running ? "running" : failed ? "failed" : "succeeded";
  return (
    <div className="mb-4">
      <Panel
        title={t("patch.lastApply.title")}
        actions={
          <StatusPill
            value={state}
            kind={running ? "run" : failed ? "crit" : "ok"}
            label={
              running
                ? t("patch.lastApply.running")
                : failed
                  ? t("patch.lastApply.withFailures")
                  : t("patch.lastApply.succeeded")
            }
          />
        }
      >
        <p>
          {running ? (
            /* A run with no `finished_at` is not a run with zero installs. The
               counts on it are what the agent has reported so far, which is
               nothing yet, and printing them as one is the misreading this
               panel exists to prevent. */
            <Trans
              k="patch.lastApply.sent"
              vars={{ when: formatDateTime(run.started_at, timeZone) }}
              components={{ when: <span /> }}
            />
          ) : (
            <>
              <Trans k="patch.lastApply.installed" vars={{ count: run.installed }} components={{ n: <span className="mono" /> }} />
              {run.failed > 0 && (
                <Trans k="patch.lastApply.failed" vars={{ count: run.failed }} components={{ n: <span className="mono" /> }} />
              )}
              {" · "}
              <span>{formatDateTime(run.finished_at, timeZone)}</span>
            </>
          )}
        </p>
        {!running && run.reboot_required && (
          <p className="note mt-1">{t("patch.lastApply.restartNeeded")}</p>
        )}
        {run.failures.length > 0 && (
          <ul className="mt-2">
            {run.failures.map((failure) => (
              <li key={`${failure.source}:${failure.identifier}`} className="cell-sub">
                <span className="mono">{failure.identifier}</span>
                {/* The agent's own words, as TEXT. This is the one string on the
                    panel that comes off the endpoint. */}
                {failure.error && <span className="break-words">: {failure.error}</span>}
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
