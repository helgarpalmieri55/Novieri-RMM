import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import {
  useAcknowledgeIpamDriftMutation,
  useIpamDeviceNames,
  useIpamDrift,
  useIpamOrphans,
  useIpamPermissions,
  useIpamSubnets,
  useSuppressIpamDriftMutation,
  useUserTimezone,
} from "./api";
import { DetailFacts } from "./DetailFacts";
import { ErrorLine } from "./ErrorLine";
import { IpamNav } from "./IpamNav";
import type { EvidenceNames } from "./ipamState";
import {
  describeDriftDetail,
  driftClassPresentation,
  driftCounts,
  formatAge,
  isCorroborated,
  isSuppressed,
  lastSeenMeaning,
  severityVariant,
} from "./ipamState";
import { variantKind } from "./kinds";
import type { DriftOut } from "./types";
import { DORMANT_CLASS, DRIFT_CLASSES } from "./types";
import { formatDate, toInstant } from "../../lib/datetime";
import { Icon } from "../../shell/icons";
import { Chips } from "../../shell/viz";
import {
  Btn,
  PageHead,
  Panel,
  QueryState,
  StatCard,
  StatsGrid,
  StatusPill,
} from "../../shell/components";

// The drift page.
//
// ON THE v3 KIT (`P['ipam/drift']` in docs/design/itms-prototype-v3.html):
// one bare panel of rows led by the arrows glyph, each with the severity as
// its edge, the address over its class, the evidence beside it and the
// action on the right. The console keeps the three tiles and the class
// filter it already had over that list; the filter is v3's chip row, since
// it narrows one list rather than switching routes (the tab strip above it
// does that).
//
// THE HEADLINE NUMBER IS DRIFT ONLY. `dormant` and orphans are fetched
// alongside it, shown beside it, and are NOT IN IT, because they are not the
// same information and folding them in is precisely how this module becomes the
// list nobody opens twice:
//
//   * A DORMANT ADDRESS IS A SWITCHED-OFF LAPTOP. Documented, unobserved, and
//     unverifiable right now: the segment had no evidence source in the window
//     or the claimant's agent has been offline past it. Nothing is wrong.
//     "37 addresses we cannot currently verify" is worth knowing and is not the
//     same sentence as "37 addresses are wrong".
//   * AN ORPHAN IS AN ABSENT PLAN, NOT A WRONG ONE. Addresses observed inside
//     no registered subnet. The action is "register a subnet", not "fix a
//     record", so it is a different tab with a different button.
//
// EACH CLASS CARRIES ITS OWN REMEDY, and a conflict and a mismatch never share
// one: a `mac_mismatch` is a documentation correction, a `conflict` is two
// machines on one address and needs an operational fix before any record is
// touched.
const CONFIDENCE_LINE: Readonly<Record<string, MessageKey>> = {
  low: "ipam.drift.confidence.low",
  medium: "ipam.drift.confidence.medium",
  high: "ipam.drift.confidence.high",
};

export function DriftPage() {
  const t = useT();
  const permissions = useIpamPermissions();
  /**
   * THE TAB IS IN THE URL, and that is what makes the tiles clickable.
   *
   * `subnet` was already read from here (the tree's per-row drift badge links
   * with it). `class` joins it, named after the API's own `?class=` filter and
   * carrying the same values: a drift class, `dormant`, or `orphans`. The
   * tiles above then point at the view their number was counted from instead
   * of being three numbers with nowhere to go, and a dormant view survives
   * being pasted to the colleague who asked which addresses could not be
   * verified.
   */
  const [params, setParams] = useSearchParams();
  const subnetFilter = params.get("subnet") ?? undefined;
  const tab = params.get("class") ?? "all";
  const setTab = (key: string) => {
    const next = new URLSearchParams(params);
    if (key === "all") next.delete("class");
    else next.set("class", key);
    setParams(next);
  };

  // Three separate reads, because they are three separate kinds of fact. The
  // default `/drift` list already excludes `dormant`; it is fetched by name so
  // its tab can exist at all.
  const drift = useIpamDrift(subnetFilter ? { subnet: subnetFilter } : {});
  const dormant = useIpamDrift(
    subnetFilter ? { class: DORMANT_CLASS, subnet: subnetFilter } : { class: DORMANT_CLASS },
  );
  const orphans = useIpamOrphans();
  // WHICH SUBNET, BY NAME. Every row's second line linked to its subnet under
  // the bare word "subnet", so six rows offered six identical links that said
  // nothing about where they went (and a screen reader's link list read
  // "subnet" six times). The registry is already fetched on the other pages of
  // this module and is cheap; the prefix is what an operator recognises.
  const subnets = useIpamSubnets();
  const subnetLabels = useMemo(() => {
    const byId = new Map<string, string>();
    for (const row of subnets.data ?? []) {
      byId.set(row.id, row.name ? `${row.prefix} ${row.name}` : row.prefix);
    }
    return byId;
  }, [subnets.data]);
  // AND THE SAME FOR THE EVIDENCE. `reconcile._evaluate` writes device and
  // subnet ids into `detail`, so the evidence column read "Observed by dev-5"
  // and "Observed in subnet subnet-1" beside columns that named everything
  // else in the operator's own words. The device list is the console's shared
  // `["devices"]` read, so this is a cache hit on most navigations; an id
  // neither map knows still renders as itself.
  const devices = useIpamDeviceNames();
  const deviceLabels = useMemo(() => {
    const byId = new Map<string, string>();
    for (const device of devices.data?.items ?? []) {
      const name = device.hostname ?? device.fqdn;
      if (name) byId.set(device.id, name);
    }
    return byId;
  }, [devices.data]);
  const evidenceNames = useMemo<EvidenceNames>(
    () => ({ devices: deviceLabels, subnets: subnetLabels }),
    [deviceLabels, subnetLabels],
  );

  // Counted over BOTH lists together, on purpose: the page holds dormant rows
  // in hand and must still report them apart. `driftCounts` is the one place
  // that separation is made.
  const counts = useMemo(
    () =>
      driftCounts(
        [...(drift.data ?? []), ...(dormant.data ?? [])],
        (orphans.data ?? []).reduce((total, group) => total + group.count, 0),
      ),
    [drift.data, dormant.data, orphans.data],
  );

  const visible: DriftOut[] =
    tab === "all"
      ? (drift.data ?? [])
      : tab === DORMANT_CLASS
        ? (dormant.data ?? [])
        : (drift.data ?? []).filter((item) => item.drift_class === tab);

  // The chip row: every drift class, then the two lists that are not drift.
  // The labels carry the counts, as the buttons always have.
  const chips = [
    { key: "all", label: t("ipam.drift.chipAll", { count: counts.drift }) },
    ...DRIFT_CLASSES.map((driftClass) => ({
      key: driftClass,
      label: t("ipam.drift.chip", {
        label: driftClassPresentation(driftClass).label,
        count: counts.byClass[driftClass] ?? 0,
      }),
    })),
    { key: DORMANT_CLASS, label: t("ipam.drift.chip", { label: t("ipam.fields.dormant"), count: counts.dormant }) },
    { key: "orphans", label: t("ipam.drift.chip", { label: t("ipam.fields.orphans"), count: counts.orphans }) },
  ];
  const activeChip = Math.max(
    0,
    chips.findIndex((chip) => chip.key === tab),
  );

  return (
    <div>
      <PageHead
        title={t("ipam.drift.title")}
        description={t("ipam.drift.description")}
      />
      <IpamNav />

      {/* EACH TILE OPENS ITS OWN TAB, because each counts a different list and
          the tabs are in the URL. The hues say the same thing the page says in
          words: drift is work (warn, and the only `urgent` one: it is the
          only queue here), dormant is inert (nothing to do, by definition),
          orphans are an observation waiting for a plan. */}
      <StatsGrid columns={3}>
        <StatCard
          label={t("ipam.fields.drift")}
          value={String(counts.drift)}
          icon="arrows-exchange"
          to="/ipam/drift"
          tone="gold"
          urgent
          foot={<span>{t("ipam.drift.tileDriftFoot")}</span>}
          /* A MEASURED zero, and it says so: the drift list answered. This
             page never renders the other zero: "nothing has been looked at"
             produces no findings and no tile row on /ipam either. */
          zeroLabel={t("ipam.drift.tileDriftZero")}
        />
        <StatCard
          label={t("ipam.fields.dormant")}
          value={String(counts.dormant)}
          icon="clock"
          to="/ipam/drift?class=dormant"
          tone="neutral"
          foot={<span>{t("ipam.drift.tileDormantFoot")}</span>}
          zeroLabel={t("ipam.drift.tileDormantZero")}
        />
        <StatCard
          label={t("ipam.fields.orphans")}
          value={String(counts.orphans)}
          icon="eye"
          to="/ipam/drift?class=orphans"
          tone="blue"
          foot={<span>{t("ipam.drift.tileOrphansFoot")}</span>}
          zeroLabel={t("ipam.drift.tileOrphansZero")}
        />
      </StatsGrid>

      <div style={{ marginBottom: 14 }}>
        <Chips
          items={chips.map((chip) => chip.label)}
          active={activeChip}
          onSelect={(at) => setTab(chips[at]?.key ?? "all")}
          label={t("ipam.drift.chipsLabel")}
        />
      </div>

      {tab === "orphans" ? (
        <OrphansTab />
      ) : (
        <>
          {tab !== "all" && (
            <div className="notice" style={{ marginBottom: 14 }}>
              <Icon name="info" size={16} />
              <div>
                <p>{driftClassPresentation(tab).meaning}</p>
                <p style={{ marginTop: 4 }}>
                  <b>{t("ipam.drift.remedy")}</b>
                  {driftClassPresentation(tab).remedy}
                </p>
              </div>
            </div>
          )}
          <Panel bare>
            <QueryState
              loading={tab === DORMANT_CLASS ? dormant.isLoading : drift.isLoading}
              error={drift.error ?? dormant.error}
              empty={visible.length === 0}
              emptyLabel={t("ipam.drift.empty")}
              emptyIcon="arrows-exchange"
            >
              <DriftTable
                items={visible}
                canAssign={permissions.assign}
                subnetLabels={subnetLabels}
                evidenceNames={evidenceNames}
              />
            </QueryState>
          </Panel>
        </>
      )}
    </div>
  );
}

function DriftTable({
  items,
  canAssign,
  subnetLabels,
  evidenceNames,
}: {
  items: DriftOut[];
  canAssign: boolean;
  /** Subnet id to the prefix (and name) an operator recognises. A row whose
   *  subnet is not in the registry list still links, under its id. */
  subnetLabels: ReadonlyMap<string, string>;
  /** The same two registries, for the ids inside a finding's `detail`. */
  evidenceNames: EvidenceNames;
}) {
  const t = useT();
  const acknowledge = useAcknowledgeIpamDriftMutation();
  const suppress = useSuppressIpamDriftMutation();
  const zone = useUserTimezone();
  const [openId, setOpenId] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [until, setUntil] = useState("");

  return (
    <>
      <Table>
        <thead>
          <tr>
            <Th>{t("ipam.fields.address")}</Th>
            <Th>{t("ipam.fields.class")}</Th>
            <Th>{t("ipam.fields.severity")}</Th>
            {/* The evidence facts are the tallest thing in the row and had the
                least width to be tall in, which is what put drift rows at
                100-150px for two visible lines at 390. A floor, not a clip:
                these are the facts §4.3 exists to show. */}
            <Th width={280}>{t("ipam.drift.colEvidence")}</Th>
            <Th>{t("ipam.fields.firstDetected")}</Th>
            <Th>{t("ipam.fields.status")}</Th>
            {canAssign && <Th>{t("ipam.fields.actions")}</Th>}
          </tr>
        </thead>
        <tbody>
          {items.map((item) => {
            const presentation = driftClassPresentation(item.drift_class);
            const severity = variantKind(severityVariant(item.severity));
            return (
              <tr key={item.id}>
                {/* v3's row: the arrows glyph, the severity as the edge, the
                    address over its subnet. The edge is drawn for high and
                    medium severity; a low finding draws none, as the design
                    draws none for its info rows. */}
                <Td lead={<Icon name="arrows-exchange" size={18} />} gutter={severity} clip={220}>
                  <div className="cell-title mono">{item.address}</div>
                  {item.subnet_id && (
                    <div className="cell-sub">
                      {/* Not a <Trans>: the cell is clipped, and `Td` reads its hover title off the
                          text of its children, which a rich-text element does not expose. */}
                      {t("ipam.drift.inSubnet")}{" "}
                      <Link to={`/ipam/subnets/${item.subnet_id}`}>
                        {subnetLabels.get(item.subnet_id) ?? item.subnet_id}
                      </Link>
                    </div>
                  )}
                </Td>
                <Td>
                  {/* The pill carries the remedy as its title; the class notice
                      above the table spells out meaning and remedy when a class
                      chip is selected. A per-row remedy paragraph pushed the
                      Actions column past the panel edge at 1440. */}
                  <span title={presentation.remedy}>
                    <StatusPill
                      value={item.drift_class}
                      kind={variantKind(presentation.variant)}
                      label={presentation.label}
                    />
                  </span>
                </Td>
                <Td>
                  <StatusPill value={item.severity} kind={severity} />
                  <div className="note" style={{ marginTop: 4 }}>
                    {Object.hasOwn(CONFIDENCE_LINE, item.confidence)
                      ? t(CONFIDENCE_LINE[item.confidence] as MessageKey)
                      : t("ipam.drift.confidenceOther", { confidence: item.confidence })}
                  </div>
                </Td>
                <Td>
                  {/*
                    THE EVIDENCE, AS FACTS. A finding whose evidence is a blob of
                    JSON is a finding nobody acts on: "which MACs", "which
                    claimant", "how many VRFs" and "which gate failed" are the
                    questions §4.3 answers, and an operator should not have to
                    parse braces to read the answer.

                    `describeDriftDetail` is the address-history panel's renderer
                    with a second rule set: same engine, same value formatter,
                    same unknown-key fallback, because two renderers of one
                    free-form shape drift apart.
                  */}
                  {/* A FLOOR, AND NO CEILING. The table's auto layout hands a
                      cell its min-content, and without the floor the evidence
                      values wrapped a word to a line. A `maxWidth` here is
                      worse than none: it caps the cell's min-content while the
                      `.kv` grid inside (a max-content label track plus the
                      longest value word) stays wider, so the values overflowed
                      the cell and ran into the next column. */}
                  <div style={{ minWidth: "24ch" }}>
                    <DetailFacts
                      facts={describeDriftDetail(item, evidenceNames)}
                      empty={t("ipam.drift.evidenceEmpty")}
                      narrow
                    />
                  </div>
                </Td>
                <Td>
                  <span className="mono" title={lastSeenMeaning()}>
                    {formatAge(item.first_detected_at)}
                  </span>
                  <div className="note" style={{ marginTop: 4 }}>
                    {isCorroborated(item)
                      ? t("ipam.drift.seenPasses", { count: item.consecutive_detections })
                      : t("ipam.drift.seenOnce")}
                  </div>
                </Td>
                <Td>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                    {item.resolved_at !== null && <StatusPill value="resolved" />}
                    {item.acknowledged_at !== null && (
                      <StatusPill value="acknowledged" kind="mut" label={t("ipam.drift.acknowledged")} />
                    )}
                    {isSuppressed(item) && (
                      <StatusPill value="suppressed" kind="mut" label={t("ipam.drift.suppressed")} />
                    )}
                    {item.resolved_at === null &&
                      item.acknowledged_at === null &&
                      !isSuppressed(item) && <StatusPill value="open" kind="run" label={t("ipam.drift.open")} />}
                  </div>
                  {item.note && (
                    <div className="note" style={{ marginTop: 4 }}>
                      {item.note}
                    </div>
                  )}
                </Td>
                {canAssign && (
                  <Td>
                    <Btn label={t("ipam.drift.handle")} icon="edit" small onClick={() => setOpenId(item.id)} />
                  </Td>
                )}
              </tr>
            );
          })}
        </tbody>
      </Table>

      {openId !== null && canAssign && (
        <div className="panel-b" style={{ borderTop: "1px solid var(--line)" }}>
          <h3 className="panel-title">{t("ipam.drift.handleTitle")}</h3>
          <p className="note" style={{ margin: "4px 0 12px" }}>
            {t("ipam.drift.handleNote")}
          </p>
          <div className="form-grid">
            <Field label={t("ipam.fields.note")}>
              <input className={inputClass} value={note} onChange={(e) => setNote(e.target.value)} />
            </Field>
            <Field label={t("ipam.drift.suppressUntil")}>
              <input
                className={inputClass}
                type="date"
                value={until}
                onChange={(e) => setUntil(e.target.value)}
              />
              {/* THE DATE, READ BACK IN THE CONSOLE'S OWN FORMAT. A native
                date input draws the USER AGENT'S mask, and no CSS reorders
                it: this field reads `09/22/2026` to a US-locale browser on a
                console that renders every other date `22 Sept 2026` and whose
                /settings says day/month/year. The control stays -- it is the
                one date widget every keyboard, screen reader and locale
                already agrees on -- and says which date it holds beside
                itself (design review M1, the idiom
                `agent-push/PushAuthorizationsPage` already carried). */}
            </Field>
          </div>
          {until !== "" ? (
            <p className="note" style={{ marginTop: 14 }}>
              {t("ipam.drift.silenced", { date: formatDate(toInstant(`${until}T00:00`, zone), zone) })}
            </p>
          ) : null}
          <div className="form-actions">
            <Btn
              label={t("ipam.drift.acknowledge")}
              icon="check"
              variant="primary"
              disabled={acknowledge.isPending}
              onClick={() =>
                acknowledge.mutate(
                  { id: openId, note },
                  {
                    onSuccess: () => {
                      setOpenId(null);
                      setNote("");
                    },
                  },
                )
              }
            />
            <Btn
              label={t("ipam.drift.suppress")}
              icon="clock"
              disabled={suppress.isPending || note.trim() === "" || until === ""}
              onClick={() =>
                suppress.mutate(
                  {
                    id: openId,
                    // `new Date("2026-08-25").toISOString()` READ THE FIELD AS
                    // UTC MIDNIGHT. For an operator at UTC-5 that is 19:00 on
                    // the 24th local: the suppression lapses five hours
                    // before the day they picked has even started. The field
                    // is a wall clock with no zone; `toInstant` attaches the
                    // operator's before it becomes an instant, so "until the
                    // 25th" means midnight on the 25th where they are.
                    until: toInstant(`${until}T00:00`, zone),
                    note,
                  },
                  {
                    onSuccess: () => {
                      setOpenId(null);
                      setNote("");
                      setUntil("");
                    },
                  },
                )
              }
            />
            <Btn label={t("common.actions.cancel")} variant="ghost" onClick={() => setOpenId(null)} />
          </div>
          {(acknowledge.isError || suppress.isError) && (
            <ErrorLine error={acknowledge.error ?? suppress.error} />
          )}
        </div>
      )}
    </>
  );
}

// Orphans: their own tab, their own action. Registering the subnet is what
// turns these observations into something the plan can be wrong about.
function OrphansTab() {
  const t = useT();
  const orphans = useIpamOrphans();
  return (
    <Panel
      title={t("ipam.fields.orphans")}
      subtitle={t("ipam.drift.orphansSubtitle")}
      bare
    >
      <QueryState
        loading={orphans.isLoading}
        error={orphans.error}
        empty={(orphans.data ?? []).length === 0}
        emptyLabel={t("ipam.drift.orphansEmpty")}
        emptyIcon="eye"
      >
        <Table>
          <thead>
            <tr>
              <Th>{t("ipam.drift.colInferred")}</Th>
              <Th>{t("ipam.drift.colSeen")}</Th>
              <Th>{t("ipam.fields.lastSeen")}</Th>
              <Th>{t("ipam.fields.actions")}</Th>
            </tr>
          </thead>
          <tbody>
            {orphans.data?.map((group) => (
              <tr key={group.prefix}>
                <Td lead={<Icon name="network" size={18} />}>
                  <div className="cell-title mono">{group.prefix}</div>
                </Td>
                <Td>
                  <span className="mono">{group.count}</span>
                </Td>
                <Td>
                  <span className="mono" title={lastSeenMeaning()}>
                    {formatAge(group.last_seen_at)}
                  </span>
                </Td>
                <Td>
                  {/* A link, not a button: it has an href (middle-click, copy
                      link) and `?register=1` lands on the tree with the register
                      dialog already open, as the in-page form once was. */}
                  <Link className="btn small" to="/ipam?register=1">
                    <Icon name="plus" size={14} />
                    <span>{t("ipam.drift.registerThis")}</span>
                  </Link>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </QueryState>
    </Panel>
  );
}
