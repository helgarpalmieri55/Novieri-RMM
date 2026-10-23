import { useCallback, useMemo, useState } from "react";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { useApi } from "../../lib/ApiContext";
import { PATCH_ROLLUP_PAGE_SIZE, usePatchAppSources, usePatchUpdates } from "./api";
import type { PatchFamily } from "./patchTabs";
import { resolveMatchingUpdateIds } from "./selection";
import type { PatchAppSourceSummary, PatchUpdateFilters } from "./types";
import { UpdatesPanel, type RingOption, type UpdateFilters } from "./UpdatesPanel";

/** The package manager each platform's app updates come from. */
export const APP_MANAGER: Partial<Record<PatchFamily, string>> = {
  windows: "winget",
  macos: "Homebrew",
};

/**
 * One sentence per state the fleet's scans reported for the app manager, e.g.
 * "winget checked on 12 machines" or "Homebrew not available on 3 machines:
 * Homebrew is not installed…". Empty when no scan has said anything yet.
 */
export function appSourceSentences(manager: string, items: PatchAppSourceSummary[]): string[] {
  return items.map((row) => {
    if (row.state === "scanned") {
      return translate("patch.apps.checkedOn", { manager, count: row.devices });
    }
    // The server's own words, appended after a colon when it gave any.
    const detail = row.detail ? `: ${row.detail}` : "";
    return translate(row.state === "failed" ? "patch.apps.couldNotCheck" : "patch.apps.notAvailable", {
      manager,
      count: row.devices,
      detail,
    });
  });
}

/**
 * Third-party app updates on this platform, approved into rings exactly like
 * OS updates. Approving an app clears it by package, so later versions of the
 * same app install in the same ring without a second approval.
 */
export function AppsPanel({
  family,
  canApprove,
  ringOptions,
  fleetDeviceTotal,
}: {
  family: PatchFamily;
  canApprove: boolean;
  ringOptions: RingOption[];
  fleetDeviceTotal: number | null;
}) {
  const t = useT();
  const api = useApi();
  const manager = APP_MANAGER[family] ?? t("patch.apps.fallbackManager");
  const [filters, setFilters] = useState<UpdateFilters>({ search: "", severities: [], offset: 0 });
  const [selectedIds, setSelectedIds] = useState<string[]>([]);

  const criterion = useMemo<PatchUpdateFilters>(
    () => ({ kind: "apps", os_family: family, q: filters.search.trim() || undefined }),
    [family, filters.search],
  );
  const updates = usePatchUpdates({ ...criterion, offset: filters.offset || undefined });
  const sources = usePatchAppSources(family);
  const resolveMatching = useCallback(
    () => resolveMatchingUpdateIds((request) => api.listPatchUpdates(request), criterion),
    [api, criterion],
  );

  const sentences = appSourceSentences(manager, sources.data?.items ?? []);

  return (
    <div>
      <UpdatesPanel
        query={updates}
        filters={filters}
        onFilterChange={(next) => setFilters((prev) => ({ ...prev, offset: 0, ...next }))}
        pageSize={PATCH_ROLLUP_PAGE_SIZE}
        canApprove={canApprove}
        ringOptions={ringOptions}
        fleetDeviceTotal={fleetDeviceTotal}
        neverScanned={updates.data != null && (updates.data.devices_scanned ?? 0) === 0}
        selectedIds={selectedIds}
        onSelectionChange={setSelectedIds}
        resolveMatching={resolveMatching}
        title={t("patch.apps.title")}
        subtitle={t("patch.apps.subtitle", { manager })}
        severityNote={t("patch.apps.severityNote", { manager })}
        scopeName={t("patch.apps.scopeName")}
        clearLabel={t("patch.apps.clearLabel", { manager })}
      />
      {sentences.length > 0 ? (
        <ul className="note mt-2" aria-label={t("patch.apps.statusAria", { manager })}>
          {sentences.map((text) => (
            <li key={text}>{text}</li>
          ))}
        </ul>
      ) : (
        sources.data != null && (
          <p className="note mt-2">
            {t("patch.apps.noReport", { manager })}
          </p>
        )
      )}
    </div>
  );
}
