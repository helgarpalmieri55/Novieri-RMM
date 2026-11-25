import { useEffect, useMemo, useState } from "react";
import { ClientField } from "../../components/ClientField";
import { NoValue } from "../../components/emptystate";
import { Link, useSearchParams } from "react-router-dom";
import { attemptProblems, describeSubmitError, Form, FormActions } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate, type MessageKey } from "../../i18n/core";
import { useClientScope } from "../../lib/clientScope";
import { parentClientOf, useNewRowClientField } from "../../lib/clientSelection";
import { humaniseEnum } from "../../lib/labels";
import { Icon } from "../../shell/icons";
import { useClientName } from "../settings/api";
import {
  useCreateIpamSubnetMutation,
  useIpamDrift,
  useIpamPermissions,
  useIpamSiteDirectory,
  useIpamSites,
  useIpamSubnetUtilization,
  useIpamSubnets,
  useIpamVlans,
  useIpamVrfs,
} from "./api";
import { IpamNav } from "./IpamNav";
import { IpamReadiness } from "./IpamReadiness";
import { clientRule } from "./NewRowClient";
import { UnassignedRefusal } from "../../components/UnassignedRefusal";
import {
  buildSubnetTree,
  flattenSubnetTree,
  formatAge,
  hasUtilization,
  isDriftFinding,
  lastSeenMeaning,
} from "./ipamState";
import { UtilizationBar } from "./UtilizationBar";
import type { SubnetFilters, SubnetKind, SubnetOut } from "./types";
import { IPAM_MANAGE, SUBNET_KINDS } from "./types";
import { useAdoptJourneyClient } from "../onboarding/journeyTarget";
import {
  Btn,
  ChipSelect,
  ClientFilter,
  FilterChips,
  ListEmpty,
  ListToolbar,
  PageHead,
  Panel,
  QueryState,
  SearchField,
  ShowingCount,
  StatCard,
  StatsGrid,
  StatusPill,
  UnsavedGuard,
  useNarrowedApi,
  useNewParam,
  useUrlFilters,
  withClient,
} from "../../shell/components";

// The subnet tree (spec §12): the DERIVED hierarchy, per-node utilisation,
// site/VLAN under the name, a drift badge, and filters by VRF, site and VLAN.
//
// ON THE v3 KIT (`P['ipam']` in docs/design/itms-prototype-v3.html): the head
// with its one primary action, the tile row, and one bare panel of rows led
// by the network glyph, the prefix over its site and VLAN, the "Used" cell as
// a mono count over a bar, the sweep state as a pill, and an Open action. The
// design's "Registry" and "Import" head buttons are the tab strip's second and
// sixth entries here, so they are not drawn twice.
//
// THE HIERARCHY IS DERIVED, NOT ENTERED. `parent_id` is a server-maintained
// cache of "the smallest other prefix in the same VRF that contains this one",
// recomputed on every write, which is why the create form has no parent
// picker and the edit form has no parent field. Offering one would let an
// operator assert a hierarchy that containment contradicts.
//
// CONTAINER PREFIXES RENDER WITHOUT A UTILISATION BAR. A container holds no
// addresses at all; a bar at 0% would say it is empty, which is a different
// claim from "this row exists so the tree reads".

function UtilizationCell({ subnet, chip }: { subnet: SubnetOut; chip: string }) {
  const t = useT();
  // Only assignable subnets have a utilisation to fetch. The query is disabled
  // for containers rather than fetched-and-discarded.
  const enabled = hasUtilization(subnet);
  const utilization = useIpamSubnetUtilization(subnet.id, enabled, chip);
  if (!enabled) {
    return (
      <span className="note" title={t("ipam.tree.containerTitle")}>
        {t("ipam.tree.containerCell")}
      </span>
    );
  }
  if (utilization.isPending) return <span className="note">…</span>;
  if (utilization.error) {
    return <span className="note">{describeSubmitError(utilization.error)}</span>;
  }
  if (!utilization.data) return null;
  return <UtilizationBar utilization={utilization.data} compact />;
}

/**
 * THE VIEW THE TILES LINK TO, in the URL.
 *
 * "Assignable" and "Never swept" are two subsets of this tree that an operator
 * previously had a number for and no way to see. `state` is the param name
 * /devices already filters on, so the key is the same one everywhere in the
 * console; the value narrows the rows, never the tile counts: a tile whose
 * number changes when you click it is a tile that was lying before or is lying
 * now.
 *
 * Filtering the INPUT to `buildSubnetTree` rather than its output is what keeps
 * the indent honest: a node whose parent is not in the filtered set is promoted
 * to a root (see `buildSubnetTree`), so "assignable only" renders a flat list
 * of assignable prefixes rather than children indented under absent parents.
 *
 * ON THE LIST KIT (Task 10, 5.5) the view is a chip row: "" is every subnet.
 */
const VIEWS: ReadonlyArray<{ key: string; label: MessageKey }> = [
  { key: "", label: "ipam.tree.viewAll" },
  { key: "assignable", label: "ipam.tree.viewAssignable" },
  { key: "never-swept", label: "ipam.tree.viewNeverSwept" },
];

/** The filters the server applies, from the URL: an empty value is no filter. */
const SERVER_FILTERS = ["q", "vrf", "site", "vlan", "contains"] as const;

export function SubnetTreePage() {
  // Opened from the add-a-computer journey: its client (design §4.4), from
  // `?journey_client=` (Task 5), so `client` and `site` are free for the list.
  useAdoptJourneyClient();
  const t = useT();
  const permissions = useIpamPermissions();
  // ON THE LIST KIT (redesign plan, Receta L; Task 10, 5.5): every filter is in
  // the URL, so a filtered tree is a link and the client 360's
  // "/ipam?client=<id>" opens this client's networks. `?client=` only narrows
  // the list: it never changes the console's client (decision D20).
  const filters = useUrlFilters([...SERVER_FILTERS, "state", "client"] as const);
  const newSubnet = useNewParam("subnet");
  // AN OLD LINK STILL OPENS THE DIALOG. `?register=1` was how the drift page's
  // orphans and the site servers' prerequisites opened it, and saved links
  // carry it: it becomes `?new=subnet` once, in place.
  const [params, setParams] = useSearchParams();
  useEffect(() => {
    if (params.get("register") !== "1") return;
    const next = new URLSearchParams(params);
    next.delete("register");
    next.set("new", "subnet");
    setParams(next, { replace: true });
  }, [params, setParams]);
  const view = filters.values.state;

  const serverFilters = useMemo<SubnetFilters>(() => {
    const out: SubnetFilters = {};
    for (const key of SERVER_FILTERS) if (filters.values[key] !== "") out[key] = filters.values[key];
    return out;
  }, [filters.values]);
  const chip = filters.values.client;
  // The client the chip narrowed to: a tile counted for it opens its rows
  // with the same `client=` (coordinator's ruling on the wave reviews).
  const { client: narrowedTo } = useNarrowedApi(chip);
  const subnets = useIpamSubnets(serverFilters, chip);
  const vrfs = useIpamVrfs();
  const { selected } = useClientScope();
  const clientName = useClientName();
  const directory = useIpamSiteDirectory();
  const showClient = selected === null;
  const vlans = useIpamVlans();
  // The default drift list already excludes `dormant` and resolved items
  // server-side; `isDriftFinding` is applied anyway so the badge cannot start
  // counting a dormant row if a future filter change puts one in this list.
  // Narrowed with the tree, so its tile counts the chip's client's subnets.
  const drift = useIpamDrift({}, chip);
  // The With drift tile's one condition (the coordinator's ruling 10a): no
  // drift read, pending or failed, is absent, never zero.
  const driftNoData = !drift.data;

  const driftBySubnet = useMemo(() => {
    const counts = new Map<string, number>();
    for (const item of drift.data ?? []) {
      if (!isDriftFinding(item) || item.subnet_id === null) continue;
      counts.set(item.subnet_id, (counts.get(item.subnet_id) ?? 0) + 1);
    }
    return counts;
  }, [drift.data]);

  const inView = useMemo(
    () =>
      (subnets.data ?? []).filter((subnet) =>
        view === "assignable"
          ? subnet.kind === "assignable"
          : view === "never-swept"
            ? subnet.last_swept_at === null
            : true,
      ),
    [subnets.data, view],
  );

  const rows = useMemo(() => flattenSubnetTree(buildSubnetTree(inView)), [inView]);

  const siteName = directory.siteName;
  const vlanLabel = (id: string | null): string => {
    if (id === null) return "";
    const vlan = vlans.data?.find((row) => row.id === id);
    return vlan
      ? vlan.name
        ? t("ipam.vlan.named", { vid: vlan.vid, name: vlan.name })
        : t("ipam.vlan.label", { vid: vlan.vid })
      : id;
  };

  const allSubnets = subnets.data ?? [];
  const assignableCount = allSubnets.filter((s) => s.kind === "assignable").length;
  const neverSweptCount = allSubnets.filter((s) => s.last_swept_at === null).length;
  // THE SERVER-SIDE FILTERS AND THE CHIP: this flag explains an empty *fetch*,
  // and the view below narrows what was fetched. Folding the view in would
  // blame the VRF picker for a "never swept" view that legitimately holds
  // nothing.
  const hasFilters = Object.keys(serverFilters).length > 0 || chip !== "";
  // The view's counts are the fetched list's own: `GET /ipam/subnets` answers
  // every subnet the filters match (no page), so these are totals.
  const viewCount: Readonly<Record<string, number>> = {
    "": allSubnets.length,
    assignable: assignableCount,
    "never-swept": neverSweptCount,
  };

  const primary = permissions.manage ? (
    <Btn label={t("ipam.tree.newSubnet")} icon="plus" variant="primary" onClick={newSubnet.start} />
  ) : undefined;

  /** A filter with many values: the kit's chip that opens a choice (`ChipSelect`). */
  const chipSelect = (
    key: "vrf" | "site" | "vlan",
    caption: string,
    aria: string,
    all: string,
    options: readonly { id: string; label: string }[],
  ) => (
    <ChipSelect
      label={caption}
      ariaLabel={aria}
      options={[{ value: "", label: all }, ...options.map((option) => ({ value: option.id, label: option.label }))]}
      value={filters.values[key]}
      onChange={(value) => filters.set(key, value)}
    />
  );

  return (
    <div>
      <PageHead title={t("ipam.tree.title")} description={t("ipam.tree.description")} actions={primary} />
      <IpamNav />

      <IpamReadiness chip={chip} />

      {/* AUDITOR B [MEDIUM]: "With zero subnets the tiles read `Managed
          subnets 0`, `Assignable 0`, `With drift 0`, `Never swept 0` ...
          'With drift 0' and 'Never swept 0' read as healthy findings when
          nothing has been measured. In a drift-detection module this is the
          most misleading possible zero."

          The tile row is replaced with the first-run guidance until at least
          one subnet exists. `StatCard.value` is typed `string`, so a dash
          form would put a bare em dash in the tile, and the console's own
          `NoValue` note says that dash alone is the bug. A sentence says
          which of the two zeros it is.

          Not shown while the request is still in flight or has failed: a
          sentence claiming nothing is registered is its own false statement
          when the truth is that nothing has been fetched. The list below owns
          both of those. */}
      {allSubnets.length > 0 ? (
        /* EVERY TILE OPENS THE ROWS IT COUNTED. Three of the four are views
           of the tree below, carried in the URL (`?state=`); "With drift" is
           the reconciliation list, which is a different page because a drift
           finding is about an ADDRESS, not about the subnet that contains it.
           Under the Client chip each carries its `client=`, so it opens the
           rows it counted.

           "With drift" and "Never swept" are both `urgent`: amber IS the
           "worth fixing, not an incident" treatment here -- crit red is the
           incident, and neither of these takes it. `urgent` still cannot shout
           about a measured zero: `StatCard` drops the wash when the value is
           "0". */
        <StatsGrid columns={4}>
          <StatCard
            label={t("ipam.tree.managed")}
            value={String(allSubnets.length)}
            icon="network"
            to={withClient("/ipam", narrowedTo)}
            tone="neutral"
          />
          <StatCard
            label={t("ipam.tree.assignable")}
            value={String(assignableCount)}
            icon="database"
            to={withClient("/ipam?state=assignable", narrowedTo)}
            tone="blue"
            zeroLabel={t("ipam.tree.assignableZero")}
          />
          <StatCard
            label={t("ipam.tree.withDrift")}
            value={String(driftBySubnet.size)}
            icon="arrows-exchange"
            to={withClient("/ipam/drift", narrowedTo)}
            tone="gold"
            urgent
            /* No data (pending, narrowing undecided or failed): the kit's
               absent mark, "n/a" / "n/d", and no line under it, never a zero it
               did not count (the coordinator's ruling 10a, the Absent Zero
               Rule); the drift page's list says why a read failed. */
            pending={driftNoData}
            /* The zero this module most needs to disambiguate: the drift list
               answered and no subnet is in it. The "nothing has been measured"
               zero never reaches a tile at all; the whole row is replaced by
               the first-run sentence below. */
            zeroLabel={t("ipam.tree.withDriftZero")}
            foot={driftNoData ? undefined : <span>{t("ipam.tree.withDriftFoot")}</span>}
          />
          <StatCard
            label={t("ipam.tree.neverSwept")}
            value={String(neverSweptCount)}
            icon="warning"
            to={withClient("/ipam?state=never-swept", narrowedTo)}
            tone="orange"
            urgent
            zeroLabel={t("ipam.tree.neverSweptZero")}
            foot={<span>{t("ipam.tree.neverSweptFoot")}</span>}
          />
        </StatsGrid>
      ) : subnets.isPending || subnets.error ? null : (
        <div className="notice" style={{ marginBottom: 16 }}>
          <Icon name="info" size={16} />
          <p>{hasFilters ? t("ipam.tree.clearFilters") : t("ipam.tree.noneRegistered")}</p>
        </div>
      )}

      <Panel bare>
        <ListToolbar
          search={
            <>
              <SearchField
                placeholder={t("ipam.tree.searchPlaceholder")}
                value={filters.values.q}
                onChange={(e) => filters.set("q", e.target.value)}
                aria-label={t("ipam.tree.searchAria")}
              />
              {/* NO GEOMETRY OF ITS OWN (design review I5): the kit's
                  `.srch-wrap` decides the floor and the fold for both search
                  boxes, so the stack cannot go ragged at 390. It writes its
                  key like any filter (a new history entry per change). */}
              <SearchField
                placeholder={t("ipam.tree.containsPlaceholder")}
                value={filters.values.contains}
                onChange={(e) => filters.set("contains", e.target.value, { replace: true })}
                aria-label={t("ipam.tree.containsAria")}
              />
            </>
          }
          chips={
            <>
              <FilterChips
                label={t("ipam.tree.viewAria")}
                options={VIEWS.map((item) => ({
                  value: item.key,
                  label: t(item.label),
                  count: subnets.data ? viewCount[item.key] : undefined,
                }))}
                value={view}
                onChange={(value) => filters.set("state", value)}
              />
              {chipSelect(
                "vrf",
                t("ipam.fields.vrf"),
                t("ipam.tree.vrfAria"),
                t("ipam.tree.allVrfs"),
                (vrfs.data ?? []).map((vrf) => ({ id: vrf.id, label: vrf.name })),
              )}
              {chipSelect(
                "site",
                t("ipam.fields.site"),
                t("ipam.tree.siteAria"),
                t("ipam.tree.allSites"),
                directory.sites.map((site) => ({
                  id: site.id,
                  label: showClient
                    ? t("ipam.fields.siteOfClient", { site: site.name, client: clientName(site.client_id) })
                    : site.name,
                })),
              )}
              {chipSelect(
                "vlan",
                t("ipam.fields.vlan"),
                t("ipam.tree.vlanAria"),
                t("ipam.tree.allVlans"),
                (vlans.data ?? []).map((vlan) => ({ id: vlan.id, label: t("ipam.vlan.named", { vid: vlan.vid, name: vlan.name }) })),
              )}
              <ClientFilter filters={filters} />
            </>
          }
          end={<ShowingCount shown={rows.length} total={subnets.data ? allSubnets.length : null} />}
        />

        <QueryState loading={subnets.isPending} error={subnets.error} empty={false} emptyLabel="">
          {rows.length === 0 ? (
            /* Nothing fetched under the filters or the chip: the kit's sentence
               and its way out. Nothing in the chosen view of an unfiltered
               plan: that view's own sentence. Nothing at all: the first
               subnet's action. */
            <ListEmpty
              filtered={hasFilters}
              onClear={filters.clear}
              nothingYet={
                view === ""
                  ? t("ipam.tree.emptyAll")
                  : view === "assignable"
                    ? t("ipam.tree.emptyAssignable")
                    : t("ipam.tree.emptyUnswept")
              }
              action={view === "" ? primary : undefined}
              icon="network"
            />
          ) : (
          <Table label={t("ipam.tree.title")}>
            <thead>
              <tr>
                <Th>{t("ipam.tree.colName")}</Th>
                {showClient ? <Th>{t("ipam.fields.client")}</Th> : null}
                <Th>{t("ipam.tree.colUsed")}</Th>
                <Th>{t("ipam.tree.colDrift")}</Th>
                <Th>{t("ipam.tree.colSweep")}</Th>
                <Th>{t("ipam.tree.colLastSwept")}</Th>
                <Th>{t("ipam.tree.colOpen")}</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((node) => {
                const driftCount = driftBySubnet.get(node.subnet.id) ?? 0;
                const subline = [
                  node.subnet.name,
                  siteName(node.subnet.site_id),
                  vlanLabel(node.subnet.vlan_id),
                ].filter((part) => part !== "");
                return (
                  <tr key={node.subnet.id}>
                    {/* The indent is on the span AROUND the link, in pixels, so
                        the tree reads and `ipam.test.tsx` can measure it. */}
                    <Td
                      lead={<Icon name="network" size={18} />}
                      gutter={driftCount > 0 ? "warn" : undefined}
                    >
                      <div className="cell-title">
                        <span style={{ paddingLeft: `${node.depth * 16}px` }}>
                          <Link className="mono" to={`/ipam/subnets/${node.subnet.id}`}>
                            {node.subnet.prefix}
                          </Link>
                        </span>
                        {node.subnet.kind === "container" && (
                          <span style={{ marginLeft: 8 }}>
                            <StatusPill value="container" kind="mut" label={t("ipam.tree.container")} />
                          </span>
                        )}
                      </div>
                      <div className="cell-sub">
                        {subline.length > 0 ? subline.join(" · ") : <NoValue label={t("ipam.tree.noSubline")} />}
                      </div>
                    </Td>
                    {showClient ? <Td>{clientName(node.subnet.client_id)}</Td> : null}
                    <Td>
                      <UtilizationCell subnet={node.subnet} chip={chip} />
                    </Td>
                    <Td>
                      {driftCount > 0 ? (
                        <Link to={`/ipam/drift?subnet=${node.subnet.id}`}>
                          <StatusPill value="drift" kind="warn" label={t("ipam.tree.driftPill", { count: driftCount })} />
                        </Link>
                      ) : (
                        <NoValue label={t("ipam.tree.noDrift")} />
                      )}
                    </Td>
                    <Td>
                      {node.subnet.last_swept_at === null ? (
                        <StatusPill value="never-swept" kind="warn" label={t("ipam.tree.notSwept")} />
                      ) : (
                        <StatusPill value="swept" kind="ok" label={t("ipam.tree.swept")} />
                      )}
                    </Td>
                    <Td>
                      <span className="mono" title={lastSeenMeaning()}>
                        {formatAge(node.subnet.last_swept_at)}
                      </span>
                    </Td>
                    <Td>
                      <Link className="btn small" to={`/ipam/subnets/${node.subnet.id}`}>
                        <Icon name="arrow-right" size={14} />
                        <span>{t("ipam.tree.open")}</span>
                      </Link>
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
          )}
        </QueryState>
      </Panel>

      {/* Mounted only while open (`?new=subnet`): each opening starts empty.
          Keyed by the top bar's client, so a switch of client drops what was
          typed instead of filing it under the new one (the coordinator's
          ruling 12). */}
      {permissions.manage && newSubnet.open ? (
        <CreateSubnetForm key={selected ?? ""} onClose={newSubnet.close} />
      ) : null}
    </div>
  );
}


// Auditor B (HIGH): "An impossible CIDR (999.999.999.999/99) is accepted by the
// field and Register is a silent no-op" — post-click DOM read `{live: 0, err:
// [], "No subnets registered yet": true}`. Two silences wearing one face: the
// control was disabled on an empty prefix and named no field, and a prefix that
// was merely nonsense went past that guard into a rejection the page rendered
// nowhere. Absence and shape share `requiredProblems`' single channel, so
// neither can be the one nobody wired up.
//
// NO STRICTER THAN THE SERVER. `modules/ipam/subnets.py` parses with
// `ip_network(literal, strict=False)`, which accepts host bits (`10.4.7.9/24`
// documents `10.4.7.0/24`) and a bare address (a host prefix). A check that
// refused either would replace the old silent wall with a new one that at least
// explains itself — still a wall in front of input the server would have taken.
// Built when asked for, not at load: the label is worded in the language active then.
const prefixRules = (): readonly FieldRule[] => [
  { name: "prefix", label: translate("ipam.fields.prefix"), validate: invalidPrefix },
];

// The gateway is optional, so it becomes a rule only once something has been
// typed into it — one mechanism for "missing" and "malformed", never a second
// check bolted on beside the first. A malformed gateway failed exactly the same
// way the prefix did: a 422 with nowhere to land.
function subnetRules(gateway: string): readonly FieldRule[] {
  return gateway.trim() ? [...prefixRules(), gatewayRule()] : prefixRules();
}

// Exported so the edit dialog validates a gateway exactly as this form does —
// one rule for "missing or malformed", not a second check that drifts from it.
export const gatewayRule = (): FieldRule => ({
  name: "gateway",
  label: translate("ipam.fields.gateway"),
  validate: invalidAddress,
});

function isIpv4(value: string): boolean {
  const octets = value.split(".");
  return (
    octets.length === 4 && octets.every((o) => /^\d{1,3}$/.test(o) && Number(o) <= 255)
  );
}

// Structural, and deliberately generous about what it cannot decide: at most one
// `::`, hex groups of at most four digits, and the embedded-IPv4 tail form.
// Rejecting a valid v6 literal here would be the worse defect of the two.
function isIpv6(value: string): boolean {
  const halves = value.split("::");
  if (halves.length > 2) return false;
  const groups = halves.flatMap((half) => (half === "" ? [] : half.split(":")));
  const last = groups[groups.length - 1];
  // `::ffff:10.4.7.1` — the embedded IPv4 occupies two of the eight groups.
  const embedded = last !== undefined && last.includes(".");
  if (embedded && !isIpv4(last)) return false;
  const rest = embedded ? groups.slice(0, -1) : groups;
  if (!rest.every((group) => /^[0-9A-Fa-f]{1,4}$/.test(group))) return false;
  const count = rest.length + (embedded ? 2 : 0);
  // `::` stands for at least one omitted group, so a compressed literal can
  // never carry all eight explicitly.
  return halves.length === 2 ? count <= 7 : count === 8;
}

function invalidAddress(value: string): string | null {
  if (isIpv4(value) || isIpv6(value)) return null;
  return translate("ipam.tree.gatewayInvalid", { value });
}

function invalidPrefix(value: string): string | null {
  const notCidr = translate("ipam.tree.prefixInvalid", { value });
  const [address = "", length, ...extra] = value.split("/");
  const v4 = isIpv4(address);
  if (extra.length > 0 || (!v4 && !isIpv6(address))) return notCidr;
  // A bare address is a host prefix, which is what `ip_network` makes of it.
  if (length === undefined) return null;
  if (!/^\d{1,3}$/.test(length)) return notCidr;
  const bits = Number(length);
  const ceiling = v4 ? 32 : 128;
  if (bits > ceiling) {
    return translate("ipam.tree.prefixLength", { ceiling, family: v4 ? "IPv4" : "IPv6", bits });
  }
  return null;
}

function CreateSubnetForm({ onClose }: { onClose: () => void }) {
  const t = useT();
  const create = useCreateIpamSubnetMutation();
  const vrfs = useIpamVrfs();
  const clientName = useClientName();
  const vlans = useIpamVlans();
  const [prefix, setPrefix] = useState("");
  const [name, setName] = useState("");
  const [kind, setKind] = useState<SubnetKind>("assignable");
  const [vrfId, setVrfId] = useState("");
  const [siteId, setSiteId] = useState("");
  const [vlanId, setVlanId] = useState("");
  // THE VRF DECIDES THE SUBNET'S CLIENT; with none, the client chosen at the
  // top or the Client field's, whose `global` VRF it goes into. The sites and
  // VLANs offered are that client's -- the server refuses any other's (final
  // review E). `null` while the Client field is empty, or while the chosen VRF
  // is not in the list yet (`parentClientOf`).
  const newRow = useNewRowClientField(IPAM_MANAGE, parentClientOf(vrfId, vrfs.data), {
    onPick: () => {
      setSiteId("");
      setVlanId("");
    },
  });
  const client = newRow.clientId;
  const sites = useIpamSites(client);
  const clientVlans = (vlans.data ?? []).filter((vlan) => vlan.client_id === client);
  const [gateway, setGateway] = useState("");
  // A PREFIX, NAME OR GATEWAY TYPED AND NOT REGISTERED ASKS BEFORE IT IS LEFT
  // (coordinator's ruling on the wave reviews): `?new=subnet` holds the form.
  // The VRF, site, VLAN and kind are where the next subnet goes, and stay set
  // after a registration (the dialog stays open for the next one), so they are
  // not what is lost. The page mounts the dialog only while open, so every
  // opening starts empty.
  const dirty = prefix !== "" || name !== "" || gateway !== "";
  // Populated ON SUBMIT, never while typing — "10.4" is not yet wrong, it is
  // unfinished, and a form that says so mid-keystroke is its own kind of noise.
  // Held as the rules and the values they refused, and worded at render, so a
  // language switch re-words a refusal that is already on screen.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  function submit() {
    // A rule reads only the snapshot: the gateway as it was when Register was pressed.
    // The Client first, as the form shows it (with every client and no VRF).
    const values = { prefix, gateway, client };
    const next: Attempt = { rules: () => [...clientRule(newRow), ...subnetRules(values.gateway)], values };
    setAttempt(next);
    if (attemptProblems(next).length > 0) {
      // A stale server error (or saved note) must not hide this attempt's
      // field problems (the coordinator's ruling 7).
      create.reset();
      return;
    }
    create.mutate(
      {
        // Trimmed to match what was validated: `requiredProblems` trims before
        // it tests, so an untrimmed send could carry a prefix the checks never
        // actually saw.
        prefix: prefix.trim(),
        kind,
        name: name.trim(),
        vrf_id: vrfId || null,
        client_id: client,
        site_id: siteId || null,
        vlan_id: vlanId || null,
        gateway: gateway.trim() || null,
      },
      {
        onSuccess: () => {
          setPrefix("");
          setName("");
          setGateway("");
        },
      },
    );
  }

  return (
    <>
      <Dialog open onClose={onClose} title={t("ipam.tree.registerTitle")}>
        <p className="note" style={{ marginBottom: 14 }}>
          {t("ipam.tree.registerNote")}
        </p>
        {/* A real `<form>`, so Enter submits from any field. The audit found the
            product had six of them in total and this was not one. */}
        <Form onSubmit={submit}>
          <div className="form-grid">
            <Field label={t("ipam.fields.prefix")}>
              <input
                className={inputClass}
                placeholder="10.4.7.0/24"
                value={prefix}
                onChange={(e) => setPrefix(e.target.value)}
              />
            </Field>
            <Field label={t("ipam.fields.name")}>
              <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
            </Field>
            <Field label={t("ipam.fields.kind")}>
              <select
                className={inputClass}
                value={kind}
                onChange={(e) => setKind(e.target.value as SubnetKind)}
              >
                {/* The wire value stays the value; only the LABEL is humanised.
                    `assignable` and `container` reached the screen as the
                    identifiers they are: the raw-enum defect lib/labels.ts was
                    written to end. */}
                {SUBNET_KINDS.map((value) => (
                  <option key={value} value={value}>
                    {humaniseEnum(value, "SubnetKind")}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={t("ipam.fields.vrf")}>
              <select
                className={inputClass}
                value={vrfId}
                onChange={(e) => {
                  setVrfId(e.target.value);
                  // Another VRF can be another client: its sites and VLANs differ.
                  setSiteId("");
                  setVlanId("");
                }}
              >
                <option value="">{t("ipam.fields.defaultGlobal")}</option>
                {vrfs.data?.map((vrf) => (
                  <option key={vrf.id} value={vrf.id}>
                    {t("ipam.fields.vrfOfClient", { vrf: vrf.name, client: clientName(vrf.client_id) })}
                  </option>
                ))}
              </select>
            </Field>
            {newRow.needsField ? <ClientField field={newRow} /> : null}
            <Field label={t("ipam.fields.site")}>
              <select
                className={inputClass}
                value={siteId}
                onChange={(e) => setSiteId(e.target.value)}
              >
                <option value="">{t("ipam.fields.none")}</option>
                {sites.data?.map((site) => (
                  <option key={site.id} value={site.id}>
                    {site.name}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={t("ipam.fields.vlan")}>
              <select
                className={inputClass}
                value={vlanId}
                onChange={(e) => setVlanId(e.target.value)}
              >
                <option value="">{t("ipam.fields.none")}</option>
                {clientVlans.map((vlan) => (
                  <option key={vlan.id} value={vlan.id}>
                    {t("ipam.vlan.named", { vid: vlan.vid, name: vlan.name })}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={t("ipam.fields.gateway")}>
              <input
                className={inputClass}
                placeholder="10.4.7.1"
                value={gateway}
                onChange={(e) => setGateway(e.target.value)}
              />
            </Field>
          </div>
          {/* Enabled with an empty or impossible Prefix ON PURPOSE. The disabled
              button WAS the silent no-op the auditor hit: the click moved
              nothing and named no field. Now the click produces the reason.
              With Unassigned chosen at the top and no VRF to decide, nothing
              new can be filed: the form says so instead (a VRF still can). */}
          {newRow.refused === "unassigned" ? (
            <UnassignedRefusal />
          ) : (
            <FormActions
              label={t("ipam.tree.register")}
              pending={create.isPending}
              error={create.error}
              saved={create.isSuccess}
              problems={attemptProblems(attempt)}
              savedMessage={t("ipam.tree.registered")}
            />
          )}
        </Form>
      </Dialog>
      {/* After the dialog, never inside it: both are `.modal`s at one z-index, so
          the later one paints on top, and inside the dialog's transformed box the
          question would be placed and clipped by it (coordinator's ruling 3a). */}
      <UnsavedGuard dirty={dirty} search={["new"]} />
    </>
  );
}
