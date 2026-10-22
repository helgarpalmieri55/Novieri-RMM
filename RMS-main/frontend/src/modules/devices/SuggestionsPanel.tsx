/**
 * Records the platform could not safely join (asset-inventory spec §4).
 *
 * WHY THIS EXISTS AS A SCREEN AT ALL. An enrolling agent links to an existing
 * record only when a physical MAC matches. On weaker evidence — a shared
 * hostname, a shared address, two records at once — it creates the device and
 * files a question, because auto-linking on a hostname merges two real
 * machines that share a name across sites, and afterwards nothing says which
 * inventory data belonged to which. A suggestion queue is mildly annoying; a
 * bad merge is data loss discovered months later.
 *
 * SO THE SCREEN'S JOB IS COMPARISON. The platform has already decided it
 * cannot be sure. The only useful thing this panel can do is put both records
 * in front of the operator with what matched — a list of ids would ask for the
 * decision without giving them what it takes to make it.
 *
 * IT RENDERS NOTHING WHEN THERE IS NOTHING TO DECIDE. A permanent "no
 * suggestions" card is furniture, and furniture teaches an operator to stop
 * looking at that part of the page — which is exactly the wrong habit for the
 * one panel that only ever appears when something needs a human.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { NoValue } from "../../components/emptystate";
import { describeSubmitError } from "../../components/form";
import { Button } from "../../components/ui";
import type { SuggestionDevice } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDateTime } from "../../lib/datetime";
import { humaniseEnum } from "../../lib/labels";
import { useT } from "../../i18n/context";
import { Trans } from "../../i18n/Trans";
import { Panel, QueryState, StatusPill } from "../../shell/components";
import { KV } from "../../shell/viz";

/**
 * One of the two records, as v3's `kv` inside a plain panel. The caption
 * above the list is the one thing the operator reads first: which of the
 * two rows is the one that just arrived.
 */
function DeviceCard({
  label,
  device,
  zone,
}: {
  label: string;
  device: SuggestionDevice;
  zone?: string | null;
}) {
  const t = useT();
  return (
    <div className="panel pad" style={{ flex: 1, marginBottom: 0 }}>
      {/* `.k`, the caption rung. It was `.note`, which is body-sized small
          text, because the kit's caption was scoped to `.tile .k` and
          `.facts .k` and a plain card had nothing. It is unscoped now. */}
      <div className="k">{label}</div>
      <div className="cell-title mt-1">{device.hostname || t("devices.suggestions.unnamed")}</div>
      {device.description ? <div className="cell-sub">{device.description}</div> : null}
      <div className="mt-2">
        <KV
          items={[
            [t("devices.suggestions.ip"), device.primary_ip ? <span className="mono">{device.primary_ip}</span> : <NoValue label={t("devices.suggestions.noAddress")} />],
            [t("devices.suggestions.source"), humaniseEnum(device.source, "DeviceSource")],
            [t("devices.suggestions.agent"), <StatusPill key="agent" value={device.managed ? "enrolled" : "not-enrolled"} label={device.managed ? t("devices.suggestions.enrolled") : t("devices.suggestions.noAgent")} />],
            // The two cards sit side by side and the operator is COMPARING
            // them, which is the case where two date formats -- or two zones
            // -- do the most damage: "which of these was seen more recently"
            // is the whole question this screen asks.
            [t("devices.suggestions.lastSeen"), formatDateTime(device.last_seen_at, zone)],
          ]}
        />
      </div>
    </div>
  );
}

/**
 * `zone` is passed down rather than fetched here: this panel is a child of
 * `CandidatesPage`, which already holds the profile. One query, one zone, and
 * the two records below are guaranteed to be read in the same one.
 */
export function SuggestionsPanel({ zone }: { zone?: string | null } = {}) {
  const t = useT();
  const api = useApi();
  const queryClient = useQueryClient();

  const suggestions = useQuery({
    queryKey: ["asset-suggestions"],
    queryFn: () => api.listAssetSuggestions(),
  });

  const resolve = useMutation({
    mutationFn: ({ id, resolution }: { id: string; resolution: "linked" | "rejected" }) =>
      api.resolveAssetSuggestion(id, resolution),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["asset-suggestions"] });
      void queryClient.invalidateQueries({ queryKey: ["devices"] });
    },
  });

  const items = suggestions.data?.items ?? [];

  // Nothing to decide, and no error to report: render nothing. See the header.
  if (!suggestions.isLoading && !suggestions.error && items.length === 0) return null;

  return (
    <Panel>
      <QueryState
        loading={suggestions.isLoading}
        error={suggestions.error}
        empty={false}
        emptyLabel=""
      >
        {/* AN INLINE HEADING RATHER THAN `Panel`'s `title`: the sentence
            counts the records, and the count is zero while the query is still
            loading -- a title of "0 records might be the same machine" over a
            loading placeholder is a claim the screen has not been told. */}
        <h2 className="panel-title">
          {t("devices.suggestions.title", { count: items.length })}
        </h2>
        <p className="note mt-1">
          {t("devices.suggestions.note")}
        </p>
        {items.map((item) => (
          <div key={item.id} className="mt-4">
            <p className="mb-2">
              <Trans
                k="devices.suggestions.matched"
                vars={{ basis: t(`devices.suggestions.basis.${item.basis}`) }}
                components={{ strong: <strong /> }}
              />
            </p>
            <div className="flex flex-col gap-3 sm:flex-row">
              <DeviceCard label={t("devices.suggestions.justEnrolled")} device={item.device} zone={zone} />
              <DeviceCard
                label={t("devices.suggestions.alreadyInInventory")}
                device={item.candidate}
                zone={zone}
              />
            </div>
            <div className="mt-3 flex flex-wrap justify-end gap-2">
              <Button
                variant="ghost"
                disabled={resolve.isPending}
                onClick={() => resolve.mutate({ id: item.id, resolution: "rejected" })}
              >
                {t("devices.suggestions.different")}
              </Button>
              <Button
                disabled={resolve.isPending}
                onClick={() => resolve.mutate({ id: item.id, resolution: "linked" })}
              >
                {/* The consequence is on the button, not in a paragraph above
                    it: an operator clicking "Link" has not been told that one
                    of these two rows is about to be retired. */}
                {t("devices.suggestions.same")} <span className="note">{t("devices.suggestions.retires")}</span>
              </Button>
            </div>
          </div>
        ))}

        {/* LINKING RETIRES A ROW, so a failure here must be announced rather
            than left to `String(error)` -- which renders a bodyless 503 as the
            bare word "Error" and a linking that did not happen as one that
            did. `role="alert"` because the operator's attention is on the two
            cards, not on the bottom of the panel. */}
        {resolve.isError && (
          <p role="alert" className="form-error mt-3">
            {describeSubmitError(resolve.error)}
          </p>
        )}
      </QueryState>
    </Panel>
  );
}
