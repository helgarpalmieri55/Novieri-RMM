import { useState } from "react";
import { NoValue } from "../../components/emptystate";
import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { ApiError } from "../../lib/api";
import { useIpamSubnetAddresses } from "./api";
import { AddressDialog } from "./AddressDialog";
import { ErrorLine } from "./ErrorLine";
import {
  allocationMethodPresentation,
  bandLabel,
  formatAge,
  lastSeenMeaning,
  planDisagreesWithReality,
  statePresentation,
  statusPresentation,
} from "./ipamState";
import { variantKind } from "./kinds";
import type { GridEntryOut, UtilizationOut } from "./types";
import { Icon } from "../../shell/icons";
import { Facts, Heat, type Kind } from "../../shell/viz";
import { Btn, Panel, StatusPill } from "../../shell/components";

const PAGE_SIZE = 256;
/** Rows drawn under the heat strip at once. The strip covers the whole fetched
 * page, as v3 draws it; the table walks that page in slices so the panel is a
 * strip with rows beneath it, not a second rendering of all 256 cells. */
const TABLE_ROWS = 64;

// The classic address grid, and the panel that REPLACES it above the
// enumeration ceiling.
//
// ON THE v3 KIT (`P['ipam/subnets/:subnetId']` in
// docs/design/itms-prototype-v3.html): the heat strip at the top of the
// panel, one square per address on this page, then the rows. The design's
// legend says blue is registered, amber was seen but is not registered, grey
// is free; the strip's accessible name carries the same counts in words.
//
// TWO SEPARATE COLUMNS, `Status` AND `State`. `status` is derived at read time
// from the plan, the covering ranges, the current observations and three
// evidence gates; `state` is the four-valued column a human wrote. They are
// routinely different: an address whose state is `assigned` reads `in_use`,
// `stale`, `assigned` or `dormant` depending on what the fleet can currently
// see, and NONE of those mean the plan changed. One column showing "whichever
// we have" is how an operator concludes somebody edited their reservation.
//
// THE ALLOCATION COLUMN CARRIES ITS SOURCE, for the same reason. §3.2's method
// is answered at read time from two places, what an operator documented and
// what the endpoint reported about its own NIC (§14.1's `origin`), and the
// operator wins when both spoke. A cell that showed only the winning value would
// be indistinguishable in the two cases, and "DHCP" that an operator never typed
// reads as the IPAM having edited the plan.
//
// LAST SEEN IS AN AGE. Never a timestamp: this is documentation currency, and a
// clock time in this cell is the one place the monitoring boundary could still
// be crossed.

/** One heat cell per address: the derived status, as a kind. */
function heatKind(status: string): Kind {
  switch (status) {
    case "conflicting":
      return "crit";
    case "stale":
    case "undocumented":
      return "warn";
    case "in_use":
    case "assigned":
    case "reserved":
    case "dormant":
      return "run";
    case "dhcp":
      return "info";
    default:
      return "mut";
  }
}

export function AddressGrid({
  subnetId,
  subnetPrefix,
  vrfId,
  utilization,
  canAssign,
}: {
  subnetId: string;
  subnetPrefix: string;
  /** The subnet's VRF: an address written from this grid is filed in it. */
  vrfId: string;
  utilization: UtilizationOut | undefined;
  canAssign: boolean;
}) {
  const t = useT();
  const [offset, setOffset] = useState(0);
  // Where the table's slice starts inside the fetched page. Previous/Next
  // page the table; only when the slice reaches the end of the fetched page
  // does Next fetch the next one (and Previous the one before, landing on
  // its last slice).
  const [tableOffset, setTableOffset] = useState(0);
  const [selected, setSelected] = useState<GridEntryOut | null>(null);
  const grid = useIpamSubnetAddresses(subnetId, offset, PAGE_SIZE);

  // 422 IS THE CEILING, not a failure. §0.4 and §0.5 are one rule with two
  // applications, a prefix above the org's enumeration ceiling and every IPv6
  // prefix, and the honest response is a panel of the counts that ARE known,
  // not an empty grid or a retry.
  const tooLarge = grid.error instanceof ApiError && grid.error.status === 422;
  if (tooLarge) {
    return (
      <Panel
        title={t("ipam.grid.tooLargeTitle")}
        subtitle={t("ipam.grid.tooLargeSubtitle", { prefix: subnetPrefix })}
      >
        <p className="note" style={{ marginBottom: 14 }}>
          {t("ipam.grid.tooLargeNote")}
        </p>
        {utilization && (
          <Facts
            items={[
              { k: t("ipam.fields.assigned"), v: <span className="mono">{utilization.assigned}</span> },
              { k: t("ipam.fields.reserved"), v: <span className="mono">{utilization.reserved}</span> },
              { k: t("ipam.fields.deprecated"), v: <span className="mono">{utilization.deprecated}</span> },
              { k: t("ipam.fields.observed"), v: <span className="mono">{utilization.observed}</span> },
            ]}
          />
        )}
        <p className="note">{t("ipam.grid.countsNote")}</p>
      </Panel>
    );
  }

  const entries = grid.data ?? [];
  const cells = entries.map((entry) => heatKind(entry.status));
  const rows = entries.slice(tableOffset, tableOffset + TABLE_ROWS);
  const atLastSlice = tableOffset + TABLE_ROWS >= entries.length;
  const fetchedPageIsFull = entries.length >= PAGE_SIZE;
  const rangeStart = offset + tableOffset + 1;
  const rangeEnd = offset + tableOffset + rows.length;

  const previous = () => {
    if (tableOffset > 0) {
      setTableOffset(Math.max(0, tableOffset - TABLE_ROWS));
      return;
    }
    if (offset > 0) {
      setOffset(Math.max(0, offset - PAGE_SIZE));
      setTableOffset(PAGE_SIZE - TABLE_ROWS);
    }
  };
  const next = () => {
    if (!atLastSlice) {
      setTableOffset(tableOffset + TABLE_ROWS);
      return;
    }
    if (fetchedPageIsFull) {
      setOffset(offset + PAGE_SIZE);
      setTableOffset(0);
    }
  };

  return (
    <Panel
      title={t("ipam.grid.title")}
      subtitle={t("ipam.grid.subtitle")}
      actions={
        <>
          <span className="note mono">
            {rows.length === 0 ? offset + 1 : rangeStart}–{rows.length === 0 ? offset : rangeEnd}
          </span>
          <Btn
            label={t("ipam.grid.previous")}
            icon="chevron-left"
            small
            disabled={offset === 0 && tableOffset === 0}
            onClick={previous}
          />
          <Btn
            label={t("ipam.grid.next")}
            icon="chevron-right"
            small
            disabled={atLastSlice && !fetchedPageIsFull}
            onClick={next}
          />
        </>
      }
      bare
    >
      {grid.error && !tooLarge && (
        <div className="panel-b">
          <ErrorLine error={grid.error} />
        </div>
      )}

      {cells.length > 0 && (
        <div className="panel-b" style={{ borderBottom: "1px solid var(--line)" }}>
          <Heat cells={cells} label={t("ipam.grid.heatLabel")} />
        </div>
      )}

      {grid.data && grid.data.length === 0 ? (
        <div className="empty">
          <Icon name="network" size={26} />
          <b>{t("ipam.grid.empty")}</b>
        </div>
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>{t("ipam.fields.address")}</Th>
              <Th>{t("ipam.fields.statusDerived")}</Th>
              <Th>{t("ipam.fields.stateDocumented")}</Th>
              <Th>{t("ipam.grid.colClaimant")}</Th>
              <Th>{t("ipam.grid.colAllocation")}</Th>
              <Th>{t("ipam.fields.mac")}</Th>
              {/* NINE COLUMNS AND NO MEASURE ANYWHERE, so at 1440 the two
                  narrowest values in the table were the two that broke: every
                  row read `ws-marta-` / `02` and `2 hours` / `ago`. Neither is
                  long -- a hostname is one token and a relative time is three
                  short words -- they were simply given less width than the two
                  columns of pills beside them. A floor on each is enough; both
                  keep wrapping if a value ever needs it. */}
              <Th width={150}>{t("ipam.fields.hostname")}</Th>
              <Th width={110}>{t("ipam.fields.lastSeen")}</Th>
              <Th>{t("ipam.fields.actions")}</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((entry) => {
              const status = statusPresentation(entry.status);
              const state = statePresentation(entry.state);
              const band = bandLabel(entry);
              const allocation = allocationMethodPresentation(entry);
              const kind = heatKind(entry.status);
              return (
                <tr key={entry.address}>
                  <Td
                    lead={<Icon name={entry.device_id ? "device-desktop" : "network"} size={18} />}
                    gutter={kind === "crit" || kind === "warn" ? kind : undefined}
                    clip={200}
                  >
                    <div className="cell-title mono">{entry.address}</div>
                    {band !== null && <div className="cell-sub">{band}</div>}
                  </Td>
                  <Td>
                    <span title={status.meaning}>
                      <StatusPill
                        value={entry.status}
                        kind={variantKind(status.variant)}
                        label={status.label}
                      />
                    </span>
                    {planDisagreesWithReality(entry) && (
                      <span
                        className="note"
                        style={{ marginLeft: 6, color: "var(--sev-warn)" }}
                        title={t("ipam.grid.disagree")}
                      >
                        ≠
                      </span>
                    )}
                  </Td>
                  <Td>
                    <span title={state.meaning}>{state.label}</span>
                  </Td>
                  <Td>
                    {entry.device_id ? (
                      <span className="mono">{entry.device_id}</span>
                    ) : entry.asset_label ? (
                      entry.asset_label
                    ) : (
                      <NoValue label={t("ipam.grid.noClaimant")} />
                    )}
                  </Td>
                  <Td>
                    {/* `allocationMethodPresentation` answers "nobody has said" with a
                        bare dash label; on screen that is the NoValue defect, so the
                        absence is drawn as one and its meaning stays on the cell. */}
                    {allocation.label === "—" ? (
                      <span title={allocation.meaning}>
                        <NoValue label={t("ipam.grid.allocationNotStated")} />
                      </span>
                    ) : (
                      <span title={allocation.meaning}>{allocation.label}</span>
                    )}
                    {/* WHO SAID IT, IN A WORD THAT IS A WORD. The marker read
                        "obs", whose meaning lived only in the `title` above it:
                        a hover-only carrier, and nothing at all on a phone or
                        to a screen reader that does not voice titles. The
                        distinction this cell exists to draw (§0.6) is not worth
                        three letters. */}
                    {entry.allocation_method_source === "observed" && (
                      <span
                        className="note"
                        style={{ marginLeft: 6 }}
                        title={t("ipam.grid.observedTitle")}
                      >
                        {t("ipam.grid.observed")}
                      </span>
                    )}
                  </Td>
                  <Td>
                    {entry.mac ? (
                      <span className="mono">{entry.mac}</span>
                    ) : (
                      <NoValue label={t("ipam.grid.noMac")} />
                    )}
                  </Td>
                  <Td>{entry.hostname || <NoValue label={t("ipam.grid.noHostname")} />}</Td>
                  <Td>
                    <span className="mono" title={lastSeenMeaning()}>
                      {formatAge(entry.last_seen_at)}
                    </span>
                  </Td>
                  <Td>
                    <Btn label={t("ipam.grid.manage")} icon="edit" small onClick={() => setSelected(entry)} />
                  </Td>
                </tr>
              );
            })}
          </tbody>
        </Table>
      )}

      {selected !== null && (
        <AddressDialog
          entry={selected}
          subnetPrefix={subnetPrefix}
          vrfId={vrfId}
          canAssign={canAssign}
          onClose={() => setSelected(null)}
        />
      )}
    </Panel>
  );
}
