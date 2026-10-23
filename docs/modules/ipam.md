# Module — IP Address Management (IPAM)

> Scoping brief. Phase 14 by default — **low-dependency** (Inventory, Phase 1, is
> the only hard dependency; site servers are optional accelerators), so like
> documentation and the financial trio it can be pulled earlier. For an
> operation living off IP spreadsheets, this is one of the strongest candidates
> to pull forward. Expand into a full spec via the brainstorming skill before
> implementation.

## Purpose

The address plan of record: full subnet and IP inventory — the thing usually
managed in Excel — with assignment workflows, so IPs and subnets are requested,
assigned, and released from the platform instead of edited in a shared sheet.
The phpIPAM/NetBox category, with this platform's unfair advantage: the agent
fleet already reports every managed device's actual NICs/IPs/MACs through
inventory, so the documented plan is **continuously reconciled against
reality** — the same trick the licenses module plays with installed software.
Excel can hold an address plan; it cannot tell you the plan is wrong.

## Boundary vs the monitoring platform

This module is the **plan of record and its reconciliation** — documentation
with drift detection. It is **not** live network monitoring: up/down status,
latency, SNMP performance polling, and topology belong to the separate
monitoring product. SNMP-based *enrichment* (switch ARP/MAC tables) is a later
phase here and must respect that boundary when specced.

## Scope — v1

- **Subnet registry:** hierarchical (supernet → subnet), per **site/location**,
  with VLAN association, gateway, DNS servers, purpose/description. Native
  PostgreSQL `cidr`/`inet` types for correctness and indexed containment
  queries.
- **IP records:** per-address lifecycle — available / **assigned** / reserved /
  DHCP-pool / discovered-unknown — linked either to a managed `Device` (the
  strong case) or to a free-text asset label for non-agent things (printers,
  phones, cameras, network gear), plus MAC, hostname, notes.
- **Assignment workflow:** next-free suggestion within a subnet; assign/release
  with who/when captured as **audit events**; DHCP ranges blocked from static
  assignment; reservations honored.
- **Reconciliation & drift (the differentiator):** compare registry vs observed
  state (agent inventory NICs + sweep results) and surface actionable lists —
  **undocumented** (seen on the network, not in the plan), **stale** (in the
  plan, unseen for N days → reclaim candidates), **mismatched** (device
  documented at one address, reporting another), and **conflicts** (same IP,
  two claimants).
- **Discovery sweeps:** ICMP + ARP sweep of a subnet as a signed job — run from
  a **site server** where present, else a **designated agent** on that segment —
  scheduled or on-demand, to observe the non-agent population. ARP on the local
  segment yields MACs that ping alone cannot.
- **Utilization:** per-subnet usage %, free counts, and the hierarchy rolled up.
- **Import/export:** **CSV/Excel import with column mapping** — the migration
  path off the existing sheets is a v1 requirement, not a convenience — plus
  CSV / Power BI export.
- **UI:** subnet tree + the classic per-subnet address grid; global search by
  IP, MAC, or hostname.

## Later phases

- **Windows DHCP lease import** via an agent job on the DHCP server (the agent
  is already there), and scope synchronization; AD **DNS** record awareness.
- **SNMP enrichment** from switches (ARP/MAC tables, port mapping) — specced
  carefully against the monitoring-product boundary above.
- Full **IPv6** UI/workflows (v1 stores `inet`/`cidr` natively, so nothing is
  designed out; IPv4-first is a UI scope choice, not a schema one).
- IP **request/approval** workflow for non-IT requesters.
- Subnet planning tools (split/allocate calculators, growth projections).

## Agent capability

- `net.sweep` — job-scoped ICMP/ARP sweep of the local segment; **dormant
  otherwise** (idle-budget compliant: it is a scheduled/on-demand signed job,
  not a resident scanner). Raw-socket ARP is available since the agent already
  runs as SYSTEM/root. Preferentially dispatched to site servers; designated
  per-subnet agents are the fallback.
- Later: `dhcp.export` on Windows DHCP servers.
- Passive reconciliation costs **no new agent work** — inventory already ships
  NIC data.

## Server side

- Registry + hierarchy management; the **reconciliation engine** merging
  inventory NIC data and sweep observations against records to compute drift
  and conflicts; next-free allocator; import mapper; utilization roll-ups;
  audit on every assignment change.

## Data model additions

- `subnet` (org_id, cidr, name, site, vlan_id, gateway, dns jsonb,
  parent_subnet_id, description)
- `vlan` (org_id, number, name, site)
- `dhcp_range` (org_id, subnet_id, start_ip, end_ip)
- `ip_address` (org_id, subnet_id, address inet, status, device_id nullable,
  asset_label, mac, hostname, description, assigned_by, assigned_at,
  released_at, last_seen_at)
- `ip_observation` (org_id, address inet, mac, source: inventory / sweep /
  dhcp, seen_at) — the observation history that feeds `last_seen_at` and drift.

## Integration points

- **Inventory** (hard dependency): NIC data is the passive observation stream;
  assigning an IP can bind directly to a managed `Device`.
- **Site servers**: preferred sweep origin (they are already the LAN-local
  role).
- **Sentinel** (two-way): discovered hosts enrich IPAM; IPAM subnets can define
  Sentinel's **authorized scan scopes** — one address plan feeding both
  platforms.
- **Documentation**: this module **absorbs** the network/IP typed records
  sketched in the documentation brief — doc records link to IPAM entities
  instead of duplicating them.
- **Provisioning**: static IP assignment at deploy time; PXE subnet awareness.
- **Audit / SOC 2**: assignment history and drift reports as evidence of
  network-inventory control.
- **Power BI** / CSV export.

## Open questions (resolve in spec)

- **Sweep governance:** whether active sweeps require explicit authorized-scope
  lists per org, consistent with customers' scanning policies (and, internally,
  with the same governance that scopes Sentinel). Recommended: yes — sweeps are
  authorized per subnet, and unauthorized segments are observe-passive only.
- IPv4-first UI vs any IPv6 workflows in v1 (recommend IPv4-first, schema
  IPv6-ready as noted).
- Whether non-managed entries stay free-text labels in v1 or become a
  lightweight shared "unmanaged asset" record with documentation (recommend
  free-text now; promote later if the need proves out).
- Stale-threshold default (days unseen before an address is flagged for
  reclaim) and whether reclaim is ever automatic (recommend: never automatic —
  flag only).
- Where DHCP-lease import lands (recommended later phase; it needs the
  `dhcp.export` agent job and Windows DHCP servers enrolled).
