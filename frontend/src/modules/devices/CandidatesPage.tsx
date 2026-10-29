/**
 * Discovery candidates — what the fleet has seen and the inventory cannot
 * account for (`docs/modules/asset-inventory/spec.md` §3).
 *
 * THE EMPTY STATE IS THE HARD PART, and it is why this page is not just a
 * table. "Nothing was found" and "nothing could look" render identically as a
 * blank list and mean opposite things: the first says the segment is clean,
 * the second says the platform has no idea what is on it. IPAM records
 * `state='no_agent'` on a sweep it could not run precisely so a screen can
 * tell them apart, and the response carries that count.
 *
 * WHAT THIS PAGE IS FOR NOW THAT THE SWEEP RECORDS EVERYTHING IT FINDS.
 * Until the operator's ruling of 2026-08-26 this screen was the gate: a sweep
 * produced candidates and nothing became an asset until somebody selected it
 * here. It is not a gate any more — a completed sweep writes a `devices` row
 * for every host that answered, with `provenance = "sweep"` — so this table is
 * normally EMPTY, and that is the healthy state rather than a broken one.
 *
 * THREE JOBS SURVIVE, AND EACH IS ONE NOTHING ELSE DOES.
 *
 *   1. "Nothing could look." A subnet whose most recent sweep found no agent
 *      able to run it reports `no_agent`, and an empty table and an unswept
 *      segment are the same pixels meaning opposite things. Nowhere else in
 *      the console says which one an operator is looking at.
 *   2. The residue. An address the fleet has seen that is still not on a
 *      record — an observation older than the ruling, or one whose creation
 *      the inventory declined because something already accounted for it —
 *      belongs somewhere visible, with the promote button that fixes it.
 *   3. The way through to what discovery actually recorded, which is a filter
 *      on the fleet list rather than a copy of it kept here.
 *
 * THE OLD ARGUMENT, since this file is where it was made: a ping sweep answers
 * for printers, phones, a colleague's laptop on the wrong VLAN and the coffee
 * machine, and rows built from that used to be indistinguishable from rows an
 * operator entered. `provenance` is what distinguishes them now — promoting is
 * how a person says they vouch for a row, and it still sets the asset type,
 * lifecycle, owner and location that a sweep cannot know.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { NoValue } from "../../components/emptystate";
import { describeSubmitError } from "../../components/form";
import { Button, Td } from "../../components/ui";
import type { AssetCandidate, Me, PromoteResult } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDateTime } from "../../lib/datetime";
import { useClientName } from "../settings/api";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { Trans } from "../../i18n/Trans";
import { ScanSubnetDialog } from "./ScanSubnetDialog";
import { SuggestionsPanel } from "./SuggestionsPanel";
import {
  FilterBar,
  PageHead,
  Panel,
  QueryState,
  StatCard,
  StatsGrid,
  StatusPill,
  TableWrap,
  type Kind,
} from "../../shell/components";
import { Icon } from "../../shell/icons";

/**
 * What the platform actually knows about a sighting, in the operator's words,
 * as v3's evidence pill.
 *
 * `low` is the one worth translating: it means an ICMP echo came back, which
 * establishes that an address answers and nothing else — no MAC, no identity,
 * no vendor. Rendering the raw word "low" invites it to be read as "probably
 * fine", which is the opposite of what it says. It is the muted pill, "weak"
 * in the design's word; the muted kind is what "unclassified" looks like.
 */
function evidence(candidate: AssetCandidate): { label: string; kind: Kind } {
  if (candidate.confidence === "medium") return { label: translate("devices.candidates.evidenceMedium"), kind: "info" };
  if (candidate.confidence === "high") return { label: translate("devices.candidates.evidenceHigh"), kind: "ok" };
  return { label: translate("devices.candidates.evidenceLow"), kind: "mut" };
}

/**
 * What the last promote actually did, in the operator's words.
 *
 * "already an asset" is NOT a failure -- two operators working the same list is
 * the normal case, and the server says so deliberately. It is still reported,
 * because an operator who selected ten rows and got seven assets needs to know
 * which three, and why, without going to look.
 */
function PromoteOutcome({ results }: { results: readonly PromoteResult[] }) {
  const t = useT();
  const created = results.filter((result) => result.created);
  const refused = results.filter((result) => !result.created);

  if (refused.length === 0) {
    return (
      <p role="status" className="form-saved mt-3">
        {t("devices.candidates.promoted", { count: created.length })}
      </p>
    );
  }
  return (
    <p role="alert" className="form-error mt-3">
      {t("devices.candidates.partial", {
        count: created.length,
        created: created.length,
        total: results.length,
        list: refused.map((result) => `${result.address} (${result.reason})`).join(", "),
      })}
    </p>
  );
}

/** The permission the fleet list is read under; the sweep tile counts that list. */
const DEVICES_READ = "devices.read";

/**
 * A row's identity: its client AND its address. Two clients can run the same
 * 10.0.0.9 on unrelated networks, and each is its own candidate -- selected,
 * keyed and promoted apart (MSP clients phase 2).
 */
function rowKey(candidate: AssetCandidate): string {
  return `${candidate.client_id}/${candidate.address}`;
}

/** What one click did: every client's per-address results, and the requests that failed. */
interface PromoteOutcomes {
  results: PromoteResult[];
  failures: unknown[];
}

/** A selected row, as it was when it was ticked: whose it is, and its address. */
interface Picked {
  client_id: string;
  address: string;
}

/** Every request of one click failed: each one's reason, not only the first. */
class PromoteFailed extends Error {
  constructor(readonly failures: readonly unknown[]) {
    super("every promote request failed");
    this.name = "PromoteFailed";
  }
}

export function CandidatesPage() {
  const t = useT();
  const api = useApi();
  const queryClient = useQueryClient();
  // THE SELECTION IS A SNAPSHOT of each ticked row (its client and address),
  // keyed by `rowKey`, not a set of keys read back against the list: a row a
  // refetch drops between the tick and the click is still sent, and the
  // server answers it per address ("not a candidate") instead of the page
  // silently dropping it.
  const [selected, setSelected] = useState<Map<string, Picked>>(new Map());
  const [scanning, setScanning] = useState(false);

  const candidates = useQuery({
    queryKey: ["asset-candidates"],
    queryFn: () => api.listAssetCandidates(),
  });
  // Same `["me"]` key the shell already holds, so this is a cache read. The
  // zone is the profile's, not the browser's (cycle 5).
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  // Whose a row is, where two clients show one address: the name for someone
  // holding `clients.read`, the id otherwise.
  const clientName = useClientName();
  const zone = me.data?.timezone;
  /**
   * HOW MANY ROWS THE SWEEP HAS WRITTEN ON ITS OWN EVIDENCE. v3's second
   * tile, "Recorded by sweep"; the rows themselves live on the fleet list
   * under `?provenance=sweep`, which is where the tile goes. One request for
   * one number (`limit: 1`), counted in SQL like every other count on the
   * fleet list -- never derived here.
   *
   * GATED ON THE READ THE FLEET LIST NEEDS, as `/enrollment` gates the same
   * count. This is not a security gate (the route refuses the read
   * server-side either way, invariant 6): it is what turns a silent,
   * permanent "—" for an operator without `devices.read` into a sentence
   * that says which permission the number needs.
   */
  const canReadDevices = (me.data?.permissions ?? []).includes(DEVICES_READ);
  const swept = useQuery({
    queryKey: ["devices", { provenance: "sweep", limit: 1 }],
    queryFn: () => api.listDevices({ provenance: "sweep", limit: 1 }),
    enabled: canReadDevices,
  });

  const promote = useMutation<PromoteOutcomes>({
    // THE CLIENT COMES FROM THE ROW (operator decision 2026-10-28). A
    // promotion names ONE client and each machine is filed under it, so the
    // selection goes as one request per client the selected rows are of.
    // Settled, not raced: one client's refusal must not hide the others'
    // results -- the page reports both.
    mutationFn: async () => {
      const byClient = new Map<string, string[]>();
      for (const picked of selected.values()) {
        byClient.set(picked.client_id, [...(byClient.get(picked.client_id) ?? []), picked.address]);
      }
      const settled = await Promise.allSettled(
        [...byClient].map(([client_id, addresses]) =>
          api.promoteAssetCandidates({ client_id, addresses }),
        ),
      );
      const results = settled.flatMap((s) => (s.status === "fulfilled" ? s.value.results : []));
      const failures = settled.flatMap((s) => (s.status === "rejected" ? [s.reason] : []));
      // Nothing came back at all: the selection stays, and the error lines
      // say why -- every request's, not only the first.
      if (results.length === 0 && failures.length > 0) throw new PromoteFailed(failures);
      return { results, failures };
    },
    onSuccess: () => {
      setSelected(new Map());
      void queryClient.invalidateQueries({ queryKey: ["asset-candidates"] });
      void queryClient.invalidateQueries({ queryKey: ["devices"] });
    },
  });

  const data = candidates.data;
  const items = data?.items ?? [];
  const unsweepable = data?.unsweepable_subnets ?? 0;
  // An address more than one client's rows show: those rows say whose they are.
  const seenAt = new Map<string, number>();
  for (const candidate of items) {
    seenAt.set(candidate.address, (seenAt.get(candidate.address) ?? 0) + 1);
  }
  const shared = (candidate: AssetCandidate) => (seenAt.get(candidate.address) ?? 0) > 1;

  function toggle(candidate: AssetCandidate) {
    // The outcome below describes ONE promote of ONE selection. Leaving it on
    // screen while the operator builds the next selection makes it read as a
    // report on rows it never saw.
    promote.reset();
    const key = rowKey(candidate);
    setSelected((current) => {
      const next = new Map(current);
      if (next.has(key)) next.delete(key);
      else next.set(key, { client_id: candidate.client_id, address: candidate.address });
      return next;
    });
  }

  return (
    <>
      <PageHead
        title={t("devices.candidates.title")}
        description={t("devices.candidates.description")}
        actions={
          // THE OPERATOR ASKED FOR THIS HERE, and it is the right place: the
          // page that shows what discovery found is the page from which to ask
          // it to look again. Ungated in the console -- the sweep route is
          // gated `ipam.sweep.run` server-side, and hiding the button would
          // only decide WHERE a caller without it learns they cannot, never
          // whether they may (invariant 6).
          <Button onClick={() => setScanning(true)}>
            <Icon name="search" size={14} />
            <span>{t("devices.candidates.scan")}</span>
          </Button>
        }
      />

      <ScanSubnetDialog open={scanning} onClose={() => setScanning(false)} />

      {/* Above the candidate list on purpose: a suggestion is a question the
          platform has already decided it cannot answer, and it is waiting on a
          person. Candidates are a list to work through when there is time. */}
      <SuggestionsPanel zone={zone} />

      {/* TWO TILES, AND THE THIRD THE DESIGN DRAWS IS THE TABLE'S OWN COUNT.
          `StatCard` requires `to`, and "Awaiting review" counted the table
          directly beneath it -- a tile whose only possible destination is the
          page it sits on is the dead tile the operator asked us to stop
          shipping, so that number stays on the toolbar where a result count
          belongs.

          "Recorded by a sweep" goes to the fleet list's own filter. "Subnets
          nobody can sweep" goes to IPAM: a subnet is counted because its most
          recent sweep run came back `no_agent`
          (backend/app/modules/ipam/candidates.py), and the subnets -- with
          their sweep history -- live there. It is urgent because it is the
          number that says the platform has NO IDEA what is on a segment,
          which is the opposite of the reassuring empty list beneath it.

          FOUR COLUMNS FOR TWO TILES, and that is not a typo. `columns` is the
          track count, not the child count: at 2 the pair stretched to half the
          content width each (~570px) while every other tile in the console is
          a quarter (~285px), which made two ordinary counters read as the
          page's headline and left a band of empty tile under the shorter
          figure. The design's own Discovered screen draws its tiles at the
          quarter width in a four-track grid (`docs/design/
          itms-prototype-v3.html`, `P['discovered']`), and the folding steps in
          `components.css` still take it to three, two and one column as the
          viewport narrows. */}
      <StatsGrid columns={4} keepTrackWidth>
        <StatCard
          label={t("devices.candidates.swept")}
          value={swept.data ? String(swept.data.total) : "—"}
          icon="network"
          to="/devices?provenance=sweep"
          tone="neutral"
          zeroLabel={t("devices.candidates.sweptZero")}
          foot={
            me.data && !canReadDevices
              ? <Trans k="devices.candidates.sweptNeedsRead" components={{ code: <code /> }} />
              : swept.data && swept.data.total > 0
                ? t("devices.candidates.sweptFoot")
                : undefined
          }
        />
        <StatCard
          label={t("devices.candidates.unsweepable")}
          value={String(unsweepable)}
          icon="warning"
          to="/ipam"
          tone="gold"
          urgent
          /* NOT "every subnet is covered". Zero also happens when no sweep has
             ever run, and the whole reason this figure exists is that "clean"
             and "nothing looked" are indistinguishable on screen. */
          zeroLabel={t("devices.candidates.unsweepableZero")}
          /* THE SUB-LINE ITS NEIGHBOUR HAS. Only the zero case said anything
             under the figure, so a non-zero count drew a visible empty band
             where the tile beside it carries a line -- two tiles of the same
             height, one of them half filled. It says what the number MEANS
             rather than repeating the label, and it is kept to the one line its
             neighbour takes at the tile width: a subnet lands here because
             its most recent sweep run came back `no_agent`
             (backend/app/modules/ipam/candidates.py), which is a fact about
             the segment, not about the hosts on it. Omitted at zero, where
             `zeroLabel` is already the line and two would stack. */
          foot={unsweepable > 0 ? t("devices.candidates.unsweepableFoot") : undefined}
        />
      </StatsGrid>

      {unsweepable > 0 && (
        <p className="note mb-4">
          <Trans
            k="devices.candidates.unsweepableNote"
            vars={{ count: unsweepable }}
            components={{ strong: <strong /> }}
          />
        </p>
      )}

      <Panel bare>
        <FilterBar>
          <span className="note mono">
            {data?.truncated
              ? t("devices.candidates.truncated", { shown: items.length, total: data.total })
              : t("devices.candidates.count", { count: items.length })}
          </span>
          <span className="filter-spacer" />
          <Button
            disabled={selected.size === 0 || promote.isPending}
            onClick={() => promote.mutate()}
          >
            <Icon name="plus" size={14} />
            <span>
              {promote.isPending
                ? t("devices.candidates.promoting")
                : selected.size
                  ? t("devices.candidates.promoteN", { count: selected.size })
                  : t("devices.candidates.promote")}
            </span>
          </Button>
        </FilterBar>

        <QueryState
          loading={candidates.isLoading}
          error={candidates.error}
          empty={items.length === 0}
          emptyLabel={
            unsweepable > 0
              ? t("devices.candidates.emptySwept")
              : t("devices.candidates.emptyAll")
          }
          emptyIcon="network"
        >
          <TableWrap>
            <table>
              <thead>
                <tr>
                  {/* NAMED "Select", NOT "Actions". This is the LEADING column
                      and it holds a per-row selection checkbox, not the
                      trailing controls every other table in the product puts
                      here -- `Th`'s default would have announced the wrong
                      thing, which is as much a defect as announcing nothing. */}
                  <th scope="col">
                    <span className="sr-only">{t("devices.candidates.select")}</span>
                  </th>
                  <th scope="col">{t("devices.candidates.address")}</th>
                  <th scope="col">{t("devices.candidates.mac")}</th>
                  <th scope="col">{t("devices.candidates.vendor")}</th>
                  <th scope="col">{t("devices.candidates.evidenceHeader")}</th>
                  <th scope="col">{t("devices.candidates.seen")}</th>
                </tr>
              </thead>
              <tbody>
                {items.map((candidate) => {
                  const seen = evidence(candidate);
                  return (
                    // Keyed by client AND address: two clients' 10.0.0.9
                    // are two candidates (MSP clients phase 2).
                    <tr key={rowKey(candidate)}>
                      <td>
                        <input
                          type="checkbox"
                          aria-label={
                            shared(candidate)
                              ? t("devices.candidates.selectOfClient", {
                                  address: candidate.address,
                                  client: clientName(candidate.client_id),
                                })
                              : candidate.address
                          }
                          checked={selected.has(rowKey(candidate))}
                          onChange={() => toggle(candidate)}
                        />
                      </td>
                      {/* v3's row: a glyph leads, the address is the title,
                          the reverse-DNS name (when one answered) the
                          sub-line. The glyph is the generic network one: a
                          sighting is not yet a machine of any known kind. */}
                      <Td
                        lead={<Icon name="network" size={18} />}
                        gutter={candidate.confidence === "low" ? undefined : "run"}
                        clip={200}
                      >
                        <div className="cell-title mono">{candidate.address}</div>
                        {candidate.hostname ? (
                          <div className="cell-sub">{candidate.hostname}</div>
                        ) : null}
                        {shared(candidate) ? (
                          <div className="cell-sub">
                            {t("devices.candidates.ofClient", {
                              client: clientName(candidate.client_id),
                            })}
                          </div>
                        ) : null}
                      </Td>
                      <td className="mono">
                        {candidate.mac ?? <NoValue label={t("devices.candidates.noMac")} />}
                      </td>
                      <td>
                        {/* The manufacturer when the platform knows it, the raw
                            prefix when it does not. Never a guess: this column is
                            what an operator triaging fifty rows uses to decide
                            what a thing IS. */}
                        {candidate.vendor ??
                          (candidate.oui_prefix ? (
                            <span className="note mono">{candidate.oui_prefix}</span>
                          ) : (
                            <NoValue label={t("devices.candidates.noVendor")} />
                          ))}
                      </td>
                      <td>
                        <StatusPill value={candidate.confidence} kind={seen.kind} label={seen.label} />
                      </td>
                      <td className="mono">{formatDateTime(candidate.last_seen_at, zone)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </TableWrap>
        </QueryState>
      </Panel>

      {/* THE WAY THROUGH TO WHAT THE SWEEP ACTUALLY DID. With recording
          automatic, the interesting list is no longer here -- it is the rows
          the platform wrote on its own evidence, which live on the fleet list
          under a filter. A copy of them on this page would be a second answer
          to one question, and the two would disagree the first time anything
          changed. */}
      <p className="note">
        {/* Underlined: a link inside muted small text must be distinguishable
            by more than its colour (axe `link-in-text-block`, SC 1.4.1). */}
        <Trans
          k="devices.candidates.note"
          components={{
            strong: <strong />,
            link: <Link to="/devices?provenance=sweep" style={{ textDecoration: "underline" }} />,
          }}
        />
      </p>

      {promote.isError &&
        (promote.error instanceof PromoteFailed ? [...promote.error.failures] : [promote.error]).map(
          (failure, at) => (
            <p key={at} role="alert" className="form-error mt-3">
              {describeSubmitError(failure)}
            </p>
          ),
        )}

      {/* THE 200 THAT PROMOTED NOTHING. `POST /candidates/promote` answers
          per-address on purpose -- "one stale entry in a selection must not
          silently discard the rest" -- and this page used to throw that answer
          away: it cleared the selection and refetched, so a response in which
          every row came back `created: false` was indistinguishable on screen
          from one where every row became an asset. That is the swallowed
          failure the audit found in another module, arriving through a 2xx
          rather than through a 503. Both halves are reported, and the
          incomplete case is an alert rather than a status. */}
      {promote.data && <PromoteOutcome results={promote.data.results} />}
      {/* A client whose request failed while another's succeeded: its own
          line, in the page's error form, beside the others' results. */}
      {promote.data?.failures.map((failure, at) => (
        <p key={at} role="alert" className="form-error mt-3">
          {describeSubmitError(failure)}
        </p>
      ))}
    </>
  );
}
