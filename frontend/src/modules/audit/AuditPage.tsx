/**
 * Immutable audit — the design's "Immutable audit" screen (`P['audit']` in
 * `docs/design/itms-prototype-v3.html`), built only out of what
 * `backend/app/api/v1/audit.py` actually serves.
 *
 * THIS IS AN INTEGRITY SURFACE, WHICH CHANGES THE RULES FOR IT. Every other
 * screen in this console can be wrong in a way an operator notices. This one
 * can be wrong in a way nobody notices: a page that says "verified" because
 * that string is in the markup looks exactly like a page that says it because
 * a chain walk returned `ok: true`. The design's chain tab is the first kind:
 * a mock with no request behind it.
 *
 * So the rule this file follows is narrower than "do not fabricate data":
 *
 *   **Nothing here renders as verified unless `GET /audit/verify` returned
 *   `ok: true` in this render.** Loading is "verifying", a failed request is
 *   "verification did not run — this is not a statement about the chain", and
 *   `ok: false` is a red banner on every tab plus a named broken link. There is
 *   no code path from "we do not know" to a reassuring word.
 *
 * WHAT THE DESIGN ASKS FOR THAT THE API DOES NOT SERVE, omitted rather than
 * faked:
 *
 * | Design element            | Why it is not here                            |
 * |---------------------------|-----------------------------------------------|
 * | "Export" action, Exports tab | No export endpoint at all                  |
 * | Correlation-ID search     | Not a query parameter on the route            |
 * | Date filter               | Not a query parameter on the route            |
 *
 * The design's "Verify chain" action IS served (`GET /audit/verify`), so it
 * is the head's one control: it re-runs the walk this page already made on
 * load. The one filter the endpoint serves, `action=`, is wired as a real
 * server-side query below; the free-text box narrows the fetched page only,
 * and says so.
 */

import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { EmptyState, NoValue } from "../../components/emptystate";
import { describeSubmitError } from "../../components/form";
import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { getLanguage, type MessageKey } from "../../i18n/core";
import { Trans } from "../../i18n/Trans";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDateTime, intlLocale } from "../../lib/datetime";
import { Btn, FilterBar, PageHead, Panel, QueryState, SearchField, StatCard, StatsGrid, StatusPill, Tabs } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { AUDIT_EVENTS_KEY, AUDIT_VERIFY_KEY, listAuditEvents, verifyAuditChain } from "./api";
import { auditActionLabel, auditActorTypeLabel, auditTargetTypeLabel } from "./labels";
import type { AuditEventOut } from "./types";

/** The same code `shell/nav.ts` puts on this destination, and the same code the
 * two routes enforce server-side. Nothing here is a control (invariant 6). */
const AUDIT_READ = "audit.read";

const TABS = [
  { key: "events", label: "audit.page.tabEvents" },
  { key: "integrity", label: "audit.page.tabIntegrity" },
] as const satisfies readonly { key: string; label: MessageKey }[];

/**
 * WHICH TAB IS IN THE URL, AND THAT IS WHAT MAKES THE TILES REAL.
 *
 * The four numbers above the tabs are counts of things on two different tabs —
 * two are about the event log, two are about the chain walk. Held in `useState`
 * the integrity view had no address, so an integrity tile could only ever point
 * at the page it already sits on and land the reader back on the event table.
 * In the query string each tile opens the view its own number came from, and a
 * broken chain becomes a link somebody can paste into an incident channel.
 */
const TAB_PARAM = "tab";
const EVENTS_TAB = "events";
const INTEGRITY_TAB = "integrity";

/** Chain state as this page is allowed to describe it. `unknown` is a real
 * answer and is never collapsed into either of the other two. */
type ChainState = "verifying" | "unknown" | "intact" | "broken";

function shortHash(hash: string): string {
  return hash.length > 16 ? `${hash.slice(0, 16)}…` : hash;
}

/** v3's row glyph per action family: a shield for a job, a key for a credential, a person for a sign-in. */
function actionGlyph(action: string): string {
  const family = action.split(".")[0] ?? "";
  switch (family) {
    case "job":
    case "patch":
      return "shield-check";
    case "credential":
      return "key";
    case "auth":
    case "user":
      return "user";
    case "remote":
      return "user-screen";
    case "policy":
    case "settings":
      return "settings";
    case "ai":
      return "sparkles";
    default:
      return "logs";
  }
}

/**
 * The chain hash, in full, for a reader who has to quote it.
 *
 * WHY THE TRUNCATION WAS THE FINDING (cycle 12, auditor B, MEDIUM). "The hash
 * chain is the entire evidentiary value of an 'immutable audit' module. A
 * truncated hash cannot be independently verified or quoted in an audit
 * response, so the column is decorative." Sixteen of sixty-four hex characters
 * with no expand, no copy and no tooltip is a picture of a hash.
 *
 * THE SHORT FORM STAYS as the visible token — a column of 64-character hashes
 * is unreadable, and the auditor asked for reachability, not for width. `title`
 * is the hover/expand half and `Copy` is the quotable half.
 */
function HashCell({ hash, onCopy }: { hash: string; onCopy: (hash: string) => void }) {
  const t = useT();
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      <span className="mono" title={hash}>
        {shortHash(hash)}
      </span>
      {/* NAMED BY THE HASH, not "Copy": a screen-reader user listing the
          buttons on this page would otherwise hear "Copy" two hundred times
          with nothing to tell them apart. */}
      <Btn
        small
        iconOnly
        icon="copy"
        label={t("audit.events.copy")}
        ariaLabel={t("audit.events.copyAria", { hash })}
        onClick={() => onCopy(hash)}
      />
    </span>
  );
}

export function AuditPage() {
  const t = useT();
  const api = useApi();
  const [params, setParams] = useSearchParams();
  // An unrecognised `?tab=` falls back to the event search rather than
  // rendering nothing: a hand-edited URL must not produce a blank page.
  const tab = params.get(TAB_PARAM) === INTEGRITY_TAB ? INTEGRITY_TAB : EVENTS_TAB;
  function setTab(key: string) {
    const next = new URLSearchParams(params);
    if (key === EVENTS_TAB) next.delete(TAB_PARAM);
    else next.set(TAB_PARAM, key);
    // `replace`: flipping between two views of one page is not a step back
    // through the operator's history.
    setParams(next, { replace: true });
  }
  const [search, setSearch] = useState("");
  const [action, setAction] = useState("");
  const [copied, setCopied] = useState<string | null>(null);

  async function copyHash(hash: string) {
    try {
      await navigator.clipboard?.writeText(hash);
      setCopied(hash);
    } catch {
      // A browser that refuses clipboard access is not a reason to claim the
      // hash was copied; `title` still holds the full value.
      setCopied(null);
    }
  }

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const canRead = (me.data?.permissions ?? []).includes(AUDIT_READ);
  // THE OPERATOR'S ZONE, not the browser's (cycle 5). An audit timestamp is
  // evidence: two people reading one incident from two machines must read the
  // same instant, and `toLocaleString()` gave them each their own.
  const zone = me.data?.timezone ?? null;

  // `action` is part of the key because it is a SERVER-side filter: a different
  // action is a different result set, not a different view of the same one.
  const events = useQuery({
    queryKey: [AUDIT_EVENTS_KEY, action],
    queryFn: () => listAuditEvents(api, { action: action || undefined, limit: 200 }),
    enabled: canRead,
  });

  const verify = useQuery({
    queryKey: [AUDIT_VERIFY_KEY],
    queryFn: () => verifyAuditChain(api),
    enabled: canRead,
  });

  const items: AuditEventOut[] = useMemo(() => events.data?.items ?? [], [events.data]);

  // DEVICE TARGETS BY HOSTNAME. People are named by the audit route itself;
  // devices are not, so they are looked up in the fleet list -- only when
  // this account may read it and a device target is on the page. The list
  // takes no id filter, so this is one bounded page (the route's maximum);
  // a device beyond it keeps its id, which is what the log holds anyway.
  const canReadDevices = (me.data?.permissions ?? []).includes("devices.read");
  const hasDeviceTargets = items.some((event) => event.target_type === "device");
  const devices = useQuery({
    queryKey: ["audit", "device-names"],
    queryFn: () => api.listDevices({ limit: 500 }),
    enabled: canRead && canReadDevices && hasDeviceTargets,
    staleTime: 300_000,
  });
  const hostnames = useMemo(
    () =>
      new Map(
        (devices.data?.items ?? [])
          .filter((device) => device.hostname)
          .map((device) => [device.id, device.hostname as string]),
      ),
    [devices.data],
  );
  const targetName = (event: AuditEventOut): string | null =>
    event.target_type === "device"
      ? (hostnames.get(event.target_id) ?? null)
      : event.target_type === "user"
        ? (event.target_display_name ?? null)
        : null;

  // The search matches the action's LABEL as well as its code, and the label
  // follows the language: the language is a dependency of the memo, or a switch
  // with a search typed in would keep filtering by the old language's words.
  // (`useT()` above is what re-renders this on a switch.)
  const language = getLanguage();
  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (needle === "") return items;
    return items.filter((event) =>
      [
        event.action,
        auditActionLabel(event.action),
        event.actor_type,
        event.actor_id ?? "",
        event.actor_display_name ?? "",
        event.target_type,
        event.target_id,
        targetName(event) ?? "",
      ]
        .join(" ")
        .toLowerCase()
        .includes(needle),
    );
    // `targetName` reads `hostnames`, which is the dependency that matters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, search, hostnames, language]);

  const actionTypes = useMemo(
    () => new Set(items.map((event) => event.action)).size,
    [items],
  );

  /**
   * The commonest action on the loaded page, and how much of it that is —
   * "job.dispatch, 9 of 16". The tile above it counts DISTINCT actions, which
   * is a number nobody can picture; this is the line that says what the page
   * is mostly made of.
   */
  // Only the language-free {action, count} is memoised; the sentence (and the
  // action's label) is built at render, so a language switch re-words it.
  const commonest = useMemo(() => {
    if (items.length === 0) return null;
    const counts = new Map<string, number>();
    for (const event of items) counts.set(event.action, (counts.get(event.action) ?? 0) + 1);
    const [action, count] = [...counts.entries()].sort(
      (a, b) => b[1] - a[1] || a[0].localeCompare(b[0], intlLocale()),
    )[0] as [string, number];
    return { action, count };
  }, [items]);
  const commonestAction = commonest
    ? t("audit.tiles.mostly", {
        action: auditActionLabel(commonest.action),
        count: commonest.count,
        total: items.length,
      })
    : null;

  /**
   * The whole point of the file, in five lines. `ok === true` is the ONLY path
   * to "intact"; everything else — still in flight, request failed, not
   * permitted to ask — resolves to a word that claims nothing.
   */
  const chain: ChainState = !canRead
    ? "unknown"
    : verify.isLoading
      ? "verifying"
      : verify.data === undefined
        ? "unknown"
        : verify.data.ok
          ? "intact"
          : "broken";

  const chainLabel: Record<ChainState, string> = {
    verifying: t("audit.tiles.chain.verifying"),
    unknown: t("audit.tiles.chain.unknown"),
    intact: t("audit.tiles.chain.intact"),
    broken: t("audit.tiles.chain.broken"),
  };

  /**
   * THE SECOND LINE ON EVERY TILE, which v3 draws and the port dropped: each
   * of these four figures arrived as a bare number, and two of them ("Audit
   * events 16" beside "Events in verified chain 16") were the same number
   * twice with nothing to tell a reader why both are on the page. The line
   * under each says WHERE its figure comes from — the loaded page, the
   * server's own walk — because that is exactly the difference between them.
   *
   * Each is derived or fixed copy; none is measured. A tile that cannot
   * support a line (nothing loaded, nothing walked) carries none.
   */
  const chainFoot: Record<ChainState, string> = {
    verifying: t("audit.tiles.chainFoot.verifying"),
    unknown: t("audit.tiles.chainFoot.unknown"),
    intact: t("audit.tiles.chainFoot.intact"),
    broken: t("audit.tiles.chainFoot.broken"),
  };
  const loadedFoot =
    !canRead || !events.data
      ? undefined
      : items.length < events.data.total
        ? t("audit.tiles.loaded", { loaded: items.length })
        : t("audit.tiles.wholeRecord");

  return (
    <>
      <PageHead
        title={t("audit.page.title")}
        description={t("audit.page.description")}
        actions={
          canRead ? (
            // THE DESIGN'S ONE SERVED ACTION. The walk runs on load; this runs
            // it again, and the tiles above answer from the new walk.
            <Btn
              icon="shield-check"
              label={verify.isFetching ? t("audit.page.verifying") : t("audit.page.verifyChain")}
              onClick={() => void verify.refetch()}
              disabled={verify.isFetching}
            />
          ) : undefined
        }
      />

      {/* Rendered on EVERY tab, not only the integrity one: a broken chain is
          not a detail an operator should have to navigate to. */}
      {chain === "broken" ? (
        <div className="notice red" role="alert" style={{ marginBottom: 14 }}>
          <Icon name="circle-x" size={17} />
          <div>
            <strong>{t("audit.broken.title")}</strong>
            <br />
            <Trans
              k="audit.broken.body"
              vars={{ id: verify.data?.broken_at ?? t("audit.page.unknown") }}
              components={{ mono: <span className="mono" /> }}
            />
          </div>
        </div>
      ) : null}

      {/* FOUR NUMBERS, AND EACH ONE OPENS THE VIEW IT WAS COUNTED FROM. Two
          belong to the event log and two to the chain walk, which is exactly
          why the tab now lives in the URL above.

          COLOUR IS CATEGORY. Blue is an inventory count, neutral is an inert
          total, and the integrity tile takes the colour of what it is SAYING —
          green verified, red broken, gold not known. Only "broken" is urgent:
          it is the one state on this page that needs a person tonight, and a
          tile that shouted at "Unverified" too would make the two
          indistinguishable at a glance, which is the whole defect this screen
          exists to prevent. */}
      <StatsGrid columns={4}>
        <StatCard
          label={t("audit.tiles.events")}
          value={canRead && events.data ? String(events.data.total) : "—"}
          icon="logs"
          to="/audit"
          tone="blue"
          zeroLabel={t("audit.tiles.eventsZero")}
          foot={loadedFoot}
        />
        <StatCard
          label={t("audit.tiles.integrity")}
          value={chainLabel[chain]}
          icon="shield-check"
          to={`/audit?${TAB_PARAM}=${INTEGRITY_TAB}`}
          tone={chain === "intact" ? "green" : chain === "broken" ? "red" : "gold"}
          urgent={chain === "broken"}
          /* No `canRead` guard: an account that may not read the log has
             not had the chain walked for it either, which is exactly what
             the `unknown` line says. */
          foot={chainFoot[chain]}
        />
        <StatCard
          label={t("audit.tiles.verified")}
          // Only a SUCCESSFUL verification may report a count. A count from a
          // failed walk would read as reassurance.
          value={chain === "intact" ? String(verify.data?.count ?? 0) : "—"}
          icon="list-check"
          to={`/audit?${TAB_PARAM}=${INTEGRITY_TAB}`}
          tone="green"
          zeroLabel={t("audit.tiles.verifiedZero")}
          // THE LINE THAT DISTINGUISHES THIS TILE FROM THE FIRST ONE. Both
          // read 16 on a small tenant; only one of them was counted by the
          // server walking the hash chain link by link.
          foot={chain === "intact" ? t("audit.tiles.verifiedFoot") : undefined}
        />
        <StatCard
          label={t("audit.tiles.actionTypes")}
          value={canRead && events.data ? String(actionTypes) : "—"}
          icon="filter"
          // The loaded page IS the view this counts, and the action filter that
          // narrows it sits on that tab.
          to="/audit"
          tone="neutral"
          zeroLabel={t("audit.tiles.actionTypesZero")}
          foot={commonestAction ?? undefined}
        />
      </StatsGrid>

      <Tabs items={TABS.map((item) => ({ key: item.key, label: t(item.label) }))} active={tab} onSelect={setTab} panelId="audit-tabpanel" />

      <div id="audit-tabpanel" role="tabpanel" aria-label={tab === INTEGRITY_TAB ? t("audit.page.tabIntegrity") : t("audit.page.tabEvents")} tabIndex={-1}>
      {!canRead ? (
        <Panel>
          <EmptyState
            icon="lock"
            title={me.isLoading ? t("audit.denied.loading") : t("audit.denied.title")}
            body={
              me.isLoading
                ? t("audit.denied.checking")
                : t("audit.denied.body", { code: AUDIT_READ })
            }
          />
        </Panel>
      ) : tab === "events" ? (
        <Panel bare>
          <FilterBar>
            {/* SHORT ENOUGH TO READ AT 390px. Both placeholders were
                sentences and both truncated mid-word on a phone ("...or
                targ", "...(server-"), which is worse than a short label: a
                cut-off sentence reads as a broken control. What each field
                does in full is on its `aria-label`, where it is not competing
                for pixels.

                AND NO TRAILING SPACER. `.filter-spacer` grows, so on the
                wrapped second row it took the width from the field beside it
                and the two fields came out different sizes. Nothing follows
                them here, so there is nothing for it to push. */}
            <SearchField
              placeholder={t("audit.events.searchPlaceholder")}
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              aria-label={t("audit.events.searchAria")}
            />
            <SearchField
              placeholder={t("audit.events.actionPlaceholder")}
              value={action}
              onChange={(event) => setAction(event.target.value)}
              aria-label={t("audit.events.actionAria")}
            />
          </FilterBar>

          <QueryState
            loading={events.isLoading}
            error={events.error}
            empty={filtered.length === 0}
            emptyIcon="logs"
            emptyLabel={
              items.length === 0
                ? action
                  ? t("audit.events.emptyForAction", { action })
                  : t("audit.events.emptyNone")
                : t("audit.events.emptyFiltered")
            }
          >
            <Table>
              <thead>
                <tr>
                  <Th>{t("audit.events.action")}</Th>
                  <Th>{t("audit.events.actor")}</Th>
                  <Th>{t("audit.events.when")}</Th>
                  <Th>{t("audit.events.chainHash")}</Th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((event) => (
                  <tr key={event.id}>
                    {/* v3's row: the action family's glyph as the lead, the
                        action in words as the title with its code beside it
                        (what an auditor filters by), the target under it. */}
                    <Td lead={<Icon name={actionGlyph(event.action)} size={18} />}>
                      <div className="cell-title">{auditActionLabel(event.action)}</div>
                      <div className="cell-sub mono">{event.action}</div>
                      <div className="cell-sub" title={event.target_id}>
                        <span>{auditTargetTypeLabel(event.target_type)}</span> ·{" "}
                        {targetName(event) !== null ? (
                          <span>{targetName(event)}</span>
                        ) : (
                          <span className="mono">{event.target_id}</span>
                        )}
                      </div>
                    </Td>
                    {/* WHO, by name when the directory has one (the route
                        joins it at read time), with the id kept beneath:
                        the id is what the chain holds and survives a
                        rename. No name falls back to the id. */}
                    <Td>
                      <div className="cell-title">
                        {event.actor_display_name ?? auditActorTypeLabel(event.actor_type)}
                      </div>
                      <div className="cell-sub mono" title={event.actor_id ?? undefined}>
                        {event.actor_id ?? <NoValue label={t("audit.events.noActor")} />}
                      </div>
                    </Td>
                    <Td>
                      <span className="mono" style={{ whiteSpace: "nowrap" }}>
                        {formatDateTime(event.ts, zone)}
                      </span>
                    </Td>
                    <Td>
                      <HashCell hash={event.hash} onCopy={(hash) => void copyHash(hash)} />
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </QueryState>
          {copied != null && (
            <p role="status" className="cell-sub" style={{ padding: "10px 18px" }}>
              <Trans
                k="audit.events.copied"
                vars={{ hash: copied }}
                components={{ mono: <span className="mono" /> }}
              />
            </p>
          )}
        </Panel>
      ) : (
        <Panel
          title={t("audit.integrity.title")}
          subtitle={t("audit.integrity.subtitle")}
        >
          {chain === "verifying" ? (
            <EmptyState
              icon="clock"
              title={t("audit.integrity.verifyingTitle")}
              body={t("audit.integrity.verifyingBody")}
            />
          ) : chain === "unknown" ? (
            <div className="notice gold" role="alert">
              <Icon name="alert-triangle" size={17} />
              <div>
                <strong>{t("audit.integrity.didNotRun")}</strong>
                <br />
                <Trans k="audit.integrity.didNotRunBody" components={{ em: <em /> }} />
                {/* WHY it failed. "Verification did not run" was the whole
                    message, so a 403 (this account may read events but not
                    walk the chain) was indistinguishable from a 503 — one is
                    permanent and one is "try again", and the operator was left
                    to guess which. The caught error is stated, never printed:
                    a raw body here would be the same defect one door along. */}
                {verify.error != null ? <> {describeSubmitError(verify.error)}</> : null}
              </div>
            </div>
          ) : chain === "broken" ? (
            <div className="notice red" role="alert">
              <Icon name="circle-x" size={17} />
              <div>
                <strong>{t("audit.integrity.brokenTitle")}</strong>
                <br />
                <Trans
                  k="audit.integrity.brokenBody"
                  vars={{ count: verify.data?.count ?? 0, id: verify.data?.broken_at ?? t("audit.page.unknown") }}
                  components={{ mono: <span className="mono" /> }}
                />
              </div>
            </div>
          ) : (
            <div className="notice">
              <Icon name="shield-check" size={17} />
              <div>
                <strong>
                  {t("audit.integrity.verifiedTitle")}{" "}
                  <StatusPill value="success" label={t("audit.integrity.verifiedPill")} />
                </strong>
                <br />
                {t("audit.integrity.verifiedBody", { count: verify.data?.count ?? 0 })}
              </div>
            </div>
          )}
        </Panel>
      )}
      </div>
    </>
  );
}
