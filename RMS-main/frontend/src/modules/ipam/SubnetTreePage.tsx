import { useMemo, useState } from "react";
import { NoValue } from "../../components/emptystate";
import { Link, useSearchParams } from "react-router-dom";
import { attemptProblems, describeSubmitError, Form, FormActions } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate, type MessageKey } from "../../i18n/core";
import { humaniseEnum } from "../../lib/labels";
import { Icon } from "../../shell/icons";
import {
  useCreateIpamSubnetMutation,
  useIpamDrift,
  useIpamPermissions,
  useIpamSites,
  useIpamSubnetUtilization,
  useIpamSubnets,
  useIpamVlans,
  useIpamVrfs,
} from "./api";
import { IpamNav } from "./IpamNav";
import { IpamReadiness } from "./IpamReadiness";
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
import { SUBNET_KINDS } from "./types";
import { Btn, FilterBar, PageHead, Panel, QueryState, SearchField, StatCard, StatsGrid, StatusPill } from "../../shell/components";

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

function UtilizationCell({ subnet }: { subnet: SubnetOut }) {
  const t = useT();
  // Only assignable subnets have a utilisation to fetch. The query is disabled
  // for containers rather than fetched-and-discarded.
  const enabled = hasUtilization(subnet);
  const utilization = useIpamSubnetUtilization(subnet.id, enabled);
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
 */
const VIEWS: ReadonlyArray<{ key: string; label: MessageKey }> = [
  { key: "all", label: "ipam.tree.viewAll" },
  { key: "assignable", label: "ipam.tree.viewAssignable" },
  { key: "never-swept", label: "ipam.tree.viewNeverSwept" },
];

export function SubnetTreePage() {
  const t = useT();
  const permissions = useIpamPermissions();
  const [filters, setFilters] = useState<SubnetFilters>({});
  const [params, setParams] = useSearchParams();
  // `?register=1` opens the register dialog on arrival: the drift page's
  // orphans link lands here with the form already open, as it did when the
  // form lived in the page body. Closing clears the param so a reload does
  // not reopen it.
  const [registering, setRegistering] = useState(params.get("register") === "1");
  const closeRegister = () => {
    setRegistering(false);
    if (params.get("register") !== null) {
      const next = new URLSearchParams(params);
      next.delete("register");
      setParams(next, { replace: true });
    }
  };
  const view = params.get("state") ?? "all";
  const setView = (key: string) => {
    const next = new URLSearchParams(params);
    if (key === "all") next.delete("state");
    else next.set("state", key);
    setParams(next);
  };
  const subnets = useIpamSubnets(filters);
  const vrfs = useIpamVrfs();
  const sites = useIpamSites();
  const vlans = useIpamVlans();
  // The default drift list already excludes `dormant` and resolved items
  // server-side; `isDriftFinding` is applied anyway so the badge cannot start
  // counting a dormant row if a future filter change puts one in this list.
  const drift = useIpamDrift();

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

  const siteName = (id: string | null): string =>
    id === null ? "" : (sites.data?.find((site) => site.id === id)?.name ?? id);
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
  // The SERVER-side filters only: this flag explains an empty *fetch*, and the
  // view below narrows what was fetched. Folding them together would blame the
  // VRF picker for a "never swept" view that legitimately holds nothing.
  const hasFilters = Object.values(filters).some((value) => value !== undefined && value !== "");

  return (
    <div>
      <PageHead
        title={t("ipam.tree.title")}
        description={t("ipam.tree.description")}
        actions={
          permissions.manage ? (
            <Btn
              label={t("ipam.tree.newSubnet")}
              icon="plus"
              variant="primary"
              onClick={() => setRegistering(true)}
            />
          ) : undefined
        }
      />
      <IpamNav />

      <IpamReadiness />

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
          when the truth is that nothing has been fetched. `QueryState` below
          owns both of those. */}
      {allSubnets.length > 0 ? (
        /* EVERY TILE OPENS THE ROWS IT COUNTED. Three of the four are views
           of the tree below, carried in the URL (`?state=`); "With drift" is
           the reconciliation list, which is a different page because a drift
           finding is about an ADDRESS, not about the subnet that contains it.

           "With drift" and "Never swept" are both `urgent`, and the second one
           had to become so. It read `tone="orange"` alone, which draws NOTHING:
           `tileKind` (`shell/components.tsx`) only returns a kind when `loud`
           is also set, so the tile that carries the warn alert-triangle sat on
           a plain white card immediately beside "With drift", tinted amber for
           the same class of problem. v3's own /ipam tints its UNSWEPT tile
           amber, and amber IS the "worth fixing, not an incident" treatment
           here -- crit red is the incident, and neither of these takes it. A
           tone that draws nothing is worse than either choice, because the next
           reader believes the hue was considered.

           `urgent` still cannot shout about a measured zero: `StatCard` drops
           the wash when the value is "0", so a fleet with every subnet swept
           gets the plain card and `zeroLabel`, which is the whole point of the
           tile row above. */
        <StatsGrid columns={4}>
          <StatCard
            label={t("ipam.tree.managed")}
            value={String(allSubnets.length)}
            icon="network"
            to="/ipam"
            tone="neutral"
          />
          <StatCard
            label={t("ipam.tree.assignable")}
            value={String(assignableCount)}
            icon="database"
            to="/ipam?state=assignable"
            tone="blue"
            zeroLabel={t("ipam.tree.assignableZero")}
          />
          <StatCard
            label={t("ipam.tree.withDrift")}
            value={String(driftBySubnet.size)}
            icon="arrows-exchange"
            to="/ipam/drift"
            tone="gold"
            urgent
            /* The zero this module most needs to disambiguate: the drift list
               answered and no subnet is in it. The "nothing has been measured"
               zero never reaches a tile at all; the whole row is replaced by
               the first-run sentence below. */
            zeroLabel={t("ipam.tree.withDriftZero")}
            foot={<span>{t("ipam.tree.withDriftFoot")}</span>}
          />
          <StatCard
            label={t("ipam.tree.neverSwept")}
            value={String(neverSweptCount)}
            icon="warning"
            to="/ipam?state=never-swept"
            tone="orange"
            urgent
            zeroLabel={t("ipam.tree.neverSweptZero")}
            foot={<span>{t("ipam.tree.neverSweptFoot")}</span>}
          />
        </StatsGrid>
      ) : subnets.isLoading || subnets.error ? null : (
        <div className="notice" style={{ marginBottom: 16 }}>
          <Icon name="info" size={16} />
          <p>
            {hasFilters ? t("ipam.tree.clearFilters") : t("ipam.tree.noneRegistered")}
          </p>
        </div>
      )}

      <Panel bare>
        <FilterBar>
          <SearchField
            placeholder={t("ipam.tree.searchPlaceholder")}
            value={filters.q ?? ""}
            onChange={(e) => setFilters({ ...filters, q: e.target.value || undefined })}
            aria-label={t("ipam.tree.searchAria")}
          />
          <select
            className="select-field"
            aria-label={t("ipam.tree.vrfAria")}
            value={filters.vrf ?? ""}
            onChange={(e) => setFilters({ ...filters, vrf: e.target.value || undefined })}
          >
            <option value="">{t("ipam.tree.allVrfs")}</option>
            {vrfs.data?.map((vrf) => (
              <option key={vrf.id} value={vrf.id}>
                {vrf.name}
              </option>
            ))}
          </select>
          <select
            className="select-field"
            aria-label={t("ipam.tree.siteAria")}
            value={filters.site ?? ""}
            onChange={(e) => setFilters({ ...filters, site: e.target.value || undefined })}
          >
            <option value="">{t("ipam.tree.allSites")}</option>
            {sites.data?.map((site) => (
              <option key={site.id} value={site.id}>
                {site.name}
              </option>
            ))}
          </select>
          <select
            className="select-field"
            aria-label={t("ipam.tree.vlanAria")}
            value={filters.vlan ?? ""}
            onChange={(e) => setFilters({ ...filters, vlan: e.target.value || undefined })}
          >
            <option value="">{t("ipam.tree.allVlans")}</option>
            {vlans.data?.map((vlan) => (
              <option key={vlan.id} value={vlan.id}>
                {t("ipam.vlan.named", { vid: vlan.vid, name: vlan.name })}
              </option>
            ))}
          </select>
          <SearchField
            /* NO GEOMETRY OF ITS OWN (design review I5). This field carried
               `wrapperStyle={{ flex: "0 1 280px" }}` -- a bound meant to stop
               the second search box claiming a wrap line beside the first on a
               desktop toolbar. It was the ONLY `wrapperStyle` in the console,
               and at 390 it was the bug it was written to prevent: below the
               kit's 640px fold every control in a `.filterbar` takes the whole
               line, and a box that cannot grow stops at its 280px basis while
               its five siblings run to the panel's edge. One ragged control in
               a stack of six.

               `flex-grow: 0` is what does that, and no width can fix it: the
               floor and the fold are decided by the kit's `.srch-wrap`
               (`flex: 1 1 230px`), which is what the other ~20 search boxes in
               this console use and what the first field on this very row uses.
               An exception here was a fork of the kit's one growing control. */
            placeholder={t("ipam.tree.containsPlaceholder")}
            value={filters.contains ?? ""}
            onChange={(e) => setFilters({ ...filters, contains: e.target.value || undefined })}
            aria-label={t("ipam.tree.containsAria")}
          />
          {/* The view the tiles link to. A `<select>` rather than a second tab
              strip: the strip at the top of this page switches ROUTES, and two
              tab strips on one screen switching different things is how an
              operator learns to trust neither. */}
          <select
            className="select-field"
            aria-label={t("ipam.tree.viewAria")}
            value={view}
            onChange={(e) => setView(e.target.value)}
          >
            {VIEWS.map((item) => (
              <option key={item.key} value={item.key}>
                {t(item.label)}
              </option>
            ))}
          </select>
          <span className="filter-spacer" />
          <Btn
            label={t("ipam.tree.clear")}
            onClick={() => {
              setFilters({});
              setView("all");
            }}
          />
        </FilterBar>

        <QueryState
          loading={subnets.isLoading}
          error={subnets.error}
          empty={rows.length === 0}
          emptyLabel={
            hasFilters
              ? t("ipam.tree.emptyFiltered")
              : view === "all"
                ? t("ipam.tree.emptyAll")
                : view === "assignable"
                  ? t("ipam.tree.emptyAssignable")
                  : t("ipam.tree.emptyUnswept")
          }
          emptyIcon="network"
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("ipam.tree.colName")}</Th>
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
                    <Td>
                      <UtilizationCell subnet={node.subnet} />
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
        </QueryState>
      </Panel>

      {permissions.manage && (
        <Dialog open={registering} onClose={closeRegister} title={t("ipam.tree.registerTitle")}>
          <CreateSubnetForm />
        </Dialog>
      )}
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

function CreateSubnetForm() {
  const t = useT();
  const create = useCreateIpamSubnetMutation();
  const vrfs = useIpamVrfs();
  const sites = useIpamSites();
  const vlans = useIpamVlans();
  const [prefix, setPrefix] = useState("");
  const [name, setName] = useState("");
  const [kind, setKind] = useState<SubnetKind>("assignable");
  const [vrfId, setVrfId] = useState("");
  const [siteId, setSiteId] = useState("");
  const [vlanId, setVlanId] = useState("");
  const [gateway, setGateway] = useState("");
  // Populated ON SUBMIT, never while typing — "10.4" is not yet wrong, it is
  // unfinished, and a form that says so mid-keystroke is its own kind of noise.
  // Held as the rules and the values they refused, and worded at render, so a
  // language switch re-words a refusal that is already on screen.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  function submit() {
    // A rule reads only the snapshot: the gateway as it was when Register was pressed.
    const values = { prefix, gateway };
    const next: Attempt = { rules: () => subnetRules(values.gateway), values };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    create.mutate(
      {
        // Trimmed to match what was validated: `requiredProblems` trims before
        // it tests, so an untrimmed send could carry a prefix the checks never
        // actually saw.
        prefix: prefix.trim(),
        kind,
        name: name.trim(),
        vrf_id: vrfId || null,
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
            <select className={inputClass} value={vrfId} onChange={(e) => setVrfId(e.target.value)}>
              <option value="">{t("ipam.fields.defaultGlobal")}</option>
              {vrfs.data?.map((vrf) => (
                <option key={vrf.id} value={vrf.id}>
                  {vrf.name}
                </option>
              ))}
            </select>
          </Field>
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
              {vlans.data?.map((vlan) => (
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
            nothing and named no field. Now the click produces the reason. */}
        <FormActions
          label={t("ipam.tree.register")}
          pending={create.isPending}
          error={create.error}
          saved={create.isSuccess}
          problems={attemptProblems(attempt)}
          savedMessage={t("ipam.tree.registered")}
        />
      </Form>
    </>
  );
}
