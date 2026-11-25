import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import type { KeyboardEvent } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { Attributes } from "../../components/attributes";
import { EmptyState, NoValue } from "../../components/emptystate";
import { Table, Td, Th } from "../../components/ui";
import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import { joinWords } from "../../lib/joinWords";
import type { MessageKey } from "../../i18n/core";
import { formatDateTime } from "../../lib/datetime";
import type { Me, SecurityEventFilters } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { useClientScope } from "../../lib/clientScope";
import { saveBlob } from "../../lib/download";
import { RESYNC_KIND } from "../../lib/resync";
import { useLiveEvents } from "../../lib/ws";
import { Icon } from "../../shell/icons";
import { variantKind } from "../jobs/jobState";
import { eventBadgeVariant, securityEventLabel } from "./eventBadges";
import { SecurityPrerequisites } from "./SecurityPrerequisites";
import {
  Btn,
  ClientFilter,
  FilterChips,
  ListToolbar,
  PageHead,
  Panel,
  QueryState,
  SearchField,
  ShowingCount,
  StatCard,
  StatsGrid,
  StatusPill,
  useNarrowedApi,
  useUrlFilters,
} from "../../shell/components";
import { Drawer } from "../../shell/Drawer";

// Corresponds to the design's "Security logs" screen (`P['security']` in
// docs/design/itms-prototype-v3.html): tiles, then one bare panel of rows
// each led by the event's glyph, with the time as the trailing mono cell.
// The design's "Verify chain" action belongs to the audit log, which has its
// own page; its "Export" is the evidence CSV below. The design's "Chain"
// tile is OMITTED rather than faked: `GET /api/v1/security-events` serves no
// chain state, and `/audit` is where the chain is verified.

const EVENT_TYPES = [
  "logon_success",
  "logon_failure",
  "logoff",
  "account_lockout",
  "privilege_elevation",
  "admin_group_change",
  "policy_change",
] as const;

/** v3's row glyph per event: a lock for a sign-in, a shield for a privilege. */
const EVENT_GLYPH: Record<string, string> = {
  logon_success: "lock-open",
  logon_failure: "lock",
  logoff: "logout",
  account_lockout: "lock",
  privilege_elevation: "shield-lock",
  admin_group_change: "users-group",
  policy_change: "settings",
};

function eventGlyph(type: string): string {
  return EVENT_GLYPH[type] ?? "logs";
}

// `label` ("24h", "7d", "30d") is a unit abbreviation that reads the same in
// both languages; `words` is the catalogue key of the phrase it stands for,
// worded at render so a language switch re-words it.
const PRESETS = [
  { label: "24h", words: "security.page.window24h", hours: 24 },
  { label: "7d", words: "security.page.window7d", hours: 24 * 7 },
  { label: "30d", words: "security.page.window30d", hours: 24 * 30 },
] as const satisfies readonly { label: string; words: MessageKey; hours: number }[];

const DEFAULT_HOURS = 24 * 7;
const WIDEST: (typeof PRESETS)[number] = PRESETS[PRESETS.length - 1] as (typeof PRESETS)[number];

/** The query-string keys this view is addressable by (cycle 12, finding A62). */
const USERNAME_PARAM = "username";
const EVENT_TYPE_PARAM = "event_type";
const HOURS_PARAM = "hours";

function presetFor(hours: number): (typeof PRESETS)[number] {
  return PRESETS.find((preset) => preset.hours === hours) ?? PRESETS[1];
}

export function SecurityPage() {
  const t = useT();
  const api = useApi();
  const queryClient = useQueryClient();
  const location = useLocation();

  // THE FILTERS LIVE IN THE URL, NOT IN COMPONENT STATE (cycle 12, auditor A,
  // MEDIUM). "The failed logins for this account over the last 24 hours" is
  // precisely the artefact one pastes into an incident ticket or a regulator's
  // request, and it could not be linked to or reproduced by a colleague:
  // `location.href` stayed `/security` whatever was typed, and a reload
  // discarded all three. `useSearchParams` is the whole mechanism -- state
  // that is READ from the URL cannot drift out of step with it.
  //
  // ON THE LIST KIT (redesign plan, O3 3.10): the same three keys every link
  // into this page already carries (`username`, `event_type`, `hours`), plus
  // the Client chip's `client`; the event open in its drawer is `?event=`.
  const [params, setParams] = useSearchParams();
  const filtersUrl = useUrlFilters([USERNAME_PARAM, EVENT_TYPE_PARAM, "client"] as const);
  const username = filtersUrl.values.username;
  const eventType = filtersUrl.values.event_type;
  // A hand-edited `?hours=9999` must not invent a fourth range: an unknown
  // value falls back to the default rather than requesting a window no button
  // on the page can express.
  const requestedHours = Number(params.get(HOURS_PARAM));
  const presetHours = PRESETS.some((preset) => preset.hours === requestedHours)
    ? requestedHours
    : DEFAULT_HOURS;
  const preset = presetFor(presetHours);
  const windowWords = t(preset.words);

  const opened = useUrlFilters(["event"] as const);
  const selected = opened.values.event === "" ? null : opened.values.event;
  const setSelected = (id: string) => opened.set("event", id);
  const closeEvent = () => opened.set("event", "", { replace: true });
  /** This view with one event open: what a row's link points at. */
  const eventHref = (id: string) => {
    const next = new URLSearchParams(params);
    next.set("event", id);
    return `/security?${next.toString()}`;
  };
  const [copied, setCopied] = useState(false);

  // `replace` on every write: typing eight characters into the username box is
  // one intent, not eight history entries to press Back through.
  function setFilters(patch: Record<string, string>) {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(patch)) {
      if (value === "") next.delete(key);
      else next.set(key, value);
    }
    setCopied(false);
    setParams(next, { replace: true });
  }

  /**
   * The same patch as a URL, for the stat tiles.
   *
   * A tile is a LINK, not a button that calls `setFilters` (see StatCard's
   * header): middle-clickable, pasteable, keyboard reachable. It builds on the
   * CURRENT params rather than from scratch so that a tile clicked inside a
   * 24-hour view opens a 24-hour view — the number it is printing was counted
   * over this window, and a destination showing a different one would not hold
   * that number.
   */
  function hrefWith(patch: Record<string, string>): string {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(patch)) {
      if (value === "") next.delete(key);
      else next.set(key, value);
    }
    // The window is stated explicitly even when it is the default, so the link
    // still means this window after the default is ever changed.
    next.set(HOURS_PARAM, String(presetHours));
    return `/security?${next.toString()}`;
  }

  // THE ZONE IS THE OPERATOR'S PROFILE ZONE (`/me`.timezone, cycle 2), not the
  // browser's. This page is evidence: a lockout at 02:14 is read here, quoted
  // in a ticket, and compared against a domain controller's own log, and the
  // three have to agree about which 02:14 they mean. Same `["me"]` key and
  // `staleTime` the rest of the console uses, so this shares the cached
  // response rather than adding a request; a null timezone falls
  // `formatDateTime` back to the browser zone.
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const zone = me.data?.timezone;

  // BUG FIXED DURING MIGRATION, worth recording: `Date.now()` drifts by a
  // millisecond or two on every render, so recomputing `fromTs` inline made
  // `filters` -- and therefore the query key -- a little different on every
  // render. TanStack Query hashes query keys by VALUE, so a key that changes
  // on every render is, to it, a brand new query every time: the previous
  // request's settled/error state is abandoned before it can ever be shown,
  // and the page reads as permanently loading. `useMemo` keyed on the one
  // thing that should actually move the window (`presetHours`) pins `fromTs`
  // until the operator picks a different preset.
  const fromTs = useMemo(
    () => new Date(Date.now() - presetHours * 3600 * 1000).toISOString(),
    [presetHours],
  );
  const filters: SecurityEventFilters = useMemo(
    () => ({
      username: username || undefined,
      event_type: eventType || undefined,
      from_ts: fromTs,
    }),
    [username, eventType, fromTs],
  );

  // UNDER THE CLIENT CHIP (decisions D6, D20): the events are a client's on
  // the server (`security_logs/api.py`), so the search reads that client's,
  // through `useNarrowedApi`, under its key. The evidence export and the one
  // event opened by id keep `useApi()`, as before.
  const { api: readApi, client, ready } = useNarrowedApi(filtersUrl.values.client);
  const events = useQuery({
    queryKey: ["security-events", filters, client],
    queryFn: () => readApi.listSecurityEvents(filters),
    enabled: ready,
  });
  const detail = useQuery({
    queryKey: ["security-event", selected],
    queryFn: () => api.getSecurityEvent(selected as string),
    enabled: selected != null,
  });

  // A resync too (final review W1): an ingest announced during a socket gap.
  useLiveEvents((event) => {
    if (event.kind === "security_events_ingested" || event.kind === RESYNC_KIND) {
      void queryClient.invalidateQueries({ queryKey: ["security-events"] });
    }
  });

  // THE EXPORT IS THE VIEW (cycle 12, auditor A, HIGH -- A58). It was not,
  // and could not be made so from here: `GET /api/v1/reports/access.csv` took
  // `from_ts`/`to_ts` and nothing else, so putting the two filters in this
  // href would have sent parameters FastAPI discarded while the link claimed
  // otherwise -- the deception, made worse. So the page named the window
  // instead and disclosed which filters the file did NOT carry.
  //
  // The route now takes `username` and `event_type` (closeout recommendation
  // 7), built from the same query builder the search uses, so the file and
  // the screen agree by construction. The disclaimer went with the
  // limitation: a page still apologising for something it no longer does is
  // lying in the other direction. The WINDOW stays in the label, because a
  // period is the one thing a CSV cannot say about itself once it is detached
  // from this page and pasted into an evidence pack.
  //
  // AND IT IS A REQUEST, NOT A LINK. This was
  // `<a className="btn" href={csvHref} download>`. A browser follows a link
  // itself and sends no `Authorization` header, so `audit.read` on the route
  // saw an anonymous caller, answered 401, and the browser saved the refusal
  // body as the evidence file. The same defect shipped on Devices' and
  // Software's exports; this one was invisible to a sweep for `href="/api`
  // because the href is built from a template. `lib/download.ts` carries the
  // reasoning in full.
  const evidenceParams = {
    from_ts: fromTs,
    ...(username !== "" ? { username } : {}),
    ...(eventType !== "" ? { event_type: eventType } : {}),
  };
  const exporter = useMutation({
    mutationFn: () => api.exportAccessEvidence(evidenceParams),
    onSuccess: (blob) => saveBlob(blob, `access-evidence-${preset.label}.csv`),
  });
  const items = events.data?.items ?? [];
  const failedInWindow = items.filter((event) => event.event_type === "logon_failure").length;
  const distinctPrincipals = new Set(items.map((event) => event.username).filter(Boolean)).size;
  const filtered = username !== "" || eventType !== "";
  // The Client chip empties a list as surely as a filter does, and the empty
  // state names it with the others (it is not in the evidence CSV's note:
  // the export is today's, every client's).
  const { clients } = useClientScope();
  const chipClient = client === null ? null : (clients.find((item) => item.id === client)?.name ?? null);
  const listFiltered = filtered || chipClient !== null;

  // "across the 250 events loaded so far" -- the auditor's own words for what
  // "(loaded)" was trying and failing to say. One qualifier style on all three
  // cards: the first genuinely IS the window total (the server counts it), the
  // other two are derived from the page in hand.
  const loadedNote = t("security.page.loadedNote", { count: items.length });

  function moveRange(event: KeyboardEvent<HTMLDivElement>) {
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    const jump = event.key === "Home" ? 0 : event.key === "End" ? PRESETS.length - 1 : -1;
    if (step === 0 && jump < 0) return;
    event.preventDefault();
    const current = PRESETS.findIndex((option) => option.hours === presetHours);
    const at = jump >= 0 ? jump : (current + step + PRESETS.length) % PRESETS.length;
    const target = PRESETS[at];
    if (!target) return;
    setFilters({ [HOURS_PARAM]: String(target.hours) });
    // Selection follows focus, so focus has to follow selection: the newly
    // checked radio is the only one in the tab order a moment from now.
    event.currentTarget.querySelector<HTMLButtonElement>(`[data-range="${target.label}"]`)?.focus();
  }

  async function copyLink() {
    const href = `${window.location.origin}${location.pathname}${location.search}`;
    try {
      await navigator.clipboard?.writeText(href);
      setCopied(true);
    } catch {
      // A browser that refuses clipboard access is not a reason to claim the
      // link was copied; the URL bar already holds it.
      setCopied(false);
    }
  }

  return (
    <>
      <PageHead
        // SENTENCE CASE, which is the port's rule for every heading in the
        // console and what the design titles this route. It was title case
        // to match `shell/nav.ts`'s label character for character (cycle 12,
        // auditor A, MEDIUM: "Security Logs" was rendered three ways), and
        // that agreement is worth keeping -- but by moving the nav label to
        // sentence case, not by keeping title case here. `shell/nav.ts` now
        // reads "Security logs" too, so the crumb, the top-bar title and this
        // heading agree in wording and in case. No eyebrow: the crumb above
        // is the shell's, and it is the one answer to "where am I".
        title={t("security.page.title")}
        description={t("security.page.description")}
        actions={
          <div style={{ display: "grid", gap: 6, justifyItems: "end" }}>
            {/* The row is as wide as its two buttons, as v3's head draws them;
                only the notes under it are measured, so they wrap as prose. */}
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", justifyContent: "flex-end" }}>
              <Btn
                icon="download"
                label={t("security.page.exportEvidence", { range: preset.label })}
                onClick={() => exporter.mutate()}
                disabled={exporter.isPending}
              />
              <Btn icon="link" label={t("security.page.copyLink")} onClick={() => void copyLink()} />
            </div>
            {filtered && (
              <p className="cell-sub" style={{ maxWidth: 380 }}>
                {t("security.page.csvCovers", { window: windowWords })}
              </p>
            )}
            {copied && (
              <p role="status" className="cell-sub" style={{ maxWidth: 380 }}>
                {t("security.page.linkCopied")}
              </p>
            )}
            {/* THE ANCHOR FAILED SILENTLY BY CONSTRUCTION: the browser saved
                the 401 and the console never knew. An evidence export that
                quietly produces a file of the wrong thing is worse here than
                anywhere else in the product, because the file is destined for
                an audit pack. */}
            {exporter.isError && (
              <p role="alert" className="form-error">
                {t("security.page.exportFailed", { reason: describeSubmitError(exporter.error) })}
              </p>
            )}
          </div>
        }
      />

      <SecurityPrerequisites />

      {/* EACH TILE OPENS THIS SEARCH WITH THE FILTER THAT PRODUCED ITS NUMBER,
          which is possible only because the filters live in the query string
          (see `useSearchParams` above). "Failed logins" is the one count on
          this page somebody acts on, so it is the only urgent one, and it is
          red because failure is a category, not a decoration; the other two
          are blue inventory facts of the same window. */}
      <StatsGrid columns={3}>
        <StatCard
          label={t("security.page.tiles.events")}
          // "The search answered with none" and "the search has not answered"
          // are different facts and `?? 0` printed the same 0 for both.
          value={events.data ? String(events.data.total) : "—"}
          icon="logs"
          // The whole window, with the narrowing filters dropped: this number
          // is the server's total for the window, not for the filtered view.
          to={hrefWith({ [EVENT_TYPE_PARAM]: "", [USERNAME_PARAM]: "" })}
          tone="blue"
          zeroLabel={t("security.page.tiles.eventsZero")}
          foot={windowWords}
        />
        <StatCard
          label={t("security.page.tiles.failed")}
          value={events.data ? String(failedInWindow) : "—"}
          icon="lock"
          // The same window and the same username, narrowed to the event type
          // this tile counted — the drill-down an operator would otherwise do
          // by hand in the dropdown below.
          to={hrefWith({ [EVENT_TYPE_PARAM]: "logon_failure" })}
          tone="red"
          urgent
          zeroLabel={t("security.page.tiles.failedZero")}
          foot={loadedNote}
        />
        <StatCard
          label={t("security.page.tiles.accounts")}
          value={events.data ? String(distinctPrincipals) : "—"}
          icon="user"
          // No principals list exists to open — this control plane serves no
          // route enumerating the accounts seen in security events — so the
          // destination is the events these principals appear in, with the
          // username narrowing dropped since it would leave exactly one.
          to={hrefWith({ [USERNAME_PARAM]: "" })}
          tone="blue"
          zeroLabel={t("security.page.tiles.accountsZero")}
          foot={loadedNote}
        />
      </StatsGrid>

      <Panel bare>
        <ListToolbar
          search={
            <SearchField
              placeholder={t("security.page.filters.usernamePlaceholder")}
              value={username}
              // Typing is one intent, not one history entry per character.
              onChange={(event) => {
                setCopied(false);
                filtersUrl.set(USERNAME_PARAM, event.target.value, { replace: true });
              }}
              aria-label={t("security.page.filters.usernameLabel")}
              maxLength={200}
            />
          }
          chips={
            <>
              {/* THE EVENT TYPE AS THE KIT'S CHIPS, one per type the log
                  carries, in words; no counts, since the search answers a
                  total and no count by type. */}
              <FilterChips
                label={t("security.page.filters.eventTypeLabel")}
                options={[
                  { value: "", label: t("security.page.filters.allEventTypes") },
                  ...EVENT_TYPES.map((name) => ({ value: name, label: securityEventLabel(name) })),
                ]}
                value={eventType}
                onChange={(value) => {
                  setCopied(false);
                  filtersUrl.set(EVENT_TYPE_PARAM, value);
                }}
              />
              <ClientFilter filters={filtersUrl} />
              {/* COLOUR WAS THE ONLY SIGNAL (cycle 12, auditor A, HIGH). Three
                  bare <button>s, the selected one distinguished by a purple
                  fill and nothing else. The WAI-ARIA radio-group pattern says
                  it in structure; the check glyph says it without colour; the arrow keys
                  make the one tab stop navigable. */}
              <div
                role="radiogroup"
                aria-label={t("security.page.filters.timeRange")}
                onKeyDown={moveRange}
                className="chips"
              >
                {PRESETS.map((option) => {
                  const active = presetHours === option.hours;
                  return (
                    <button
                      key={option.label}
                      type="button"
                      role="radio"
                      aria-checked={active}
                      data-range={option.label}
                      tabIndex={active ? 0 : -1}
                      // DRAWN AS THE KIT'S CHIPS (DESIGN.md, "Chips"): the
                      // selected one in the cobalt wash with the check glyph,
                      // not a filled primary button beside the page's others.
                      className={active ? "chip on" : "chip"}
                      onClick={() => setFilters({ [HOURS_PARAM]: String(option.hours) })}
                    >
                      {active ? <Icon name="circle-check" size={13} /> : null}
                      {option.label}
                    </button>
                  );
                })}
              </div>
            </>
          }
          end={events.data ? <ShowingCount shown={items.length} total={events.data.total} /> : null}
        />

        {/* `empty` is handled below rather than by `QueryState`, whose empty
            slot takes a sentence and no way out of it. Loading and error still
            belong to it, so all three states stay the console's. */}
        <QueryState loading={events.isPending} error={events.error} empty={false} emptyLabel="">
          {items.length === 0 ? (
            listFiltered ? (
              // NAME THE FILTERS THAT PRODUCED THE EMPTINESS, and offer the two
              // ways out (cycle 12, auditor A, MEDIUM). "The user is left to
              // guess whether there genuinely are no security events, whether
              // their username filter is misspelled, or whether the agent is
              // not shipping logs at all — a materially different conclusion in
              // a security context."
              <EmptyState
                icon="logs"
                title={t("security.page.emptyFiltered.title")}
                body={t("security.page.emptyFiltered.body", {
                  window: windowWords,
                  filters: joinWords(
                    [
                      username && t("security.page.emptyFiltered.filterUsername", { username }),
                      eventType &&
                        t("security.page.emptyFiltered.filterEventType", { eventType: securityEventLabel(eventType) }),
                      chipClient === null ? "" : t("security.page.emptyFiltered.filterClient", { client: chipClient }),
                    ].filter(Boolean),
                  ),
                })}
                action={
                  <div style={{ display: "flex", justifyContent: "center", gap: 8 }}>
                    {presetHours !== WIDEST.hours && (
                      <Btn
                        label={t("security.page.emptyFiltered.widen", { range: WIDEST.label })}
                        onClick={() => setFilters({ [HOURS_PARAM]: String(WIDEST.hours) })}
                      />
                    )}
                    <Btn
                      variant="primary"
                      label={t("security.page.emptyFiltered.clear")}
                      onClick={filtersUrl.clear}
                    />
                  </div>
                }
              />
            ) : (
              <EmptyState
                icon="logs"
                title={t("security.page.empty.title")}
                body={t("security.page.empty.body", { window: windowWords })}
                action={
                  presetHours !== WIDEST.hours ? (
                    <Btn
                      label={t("security.page.emptyFiltered.widen", { range: WIDEST.label })}
                      onClick={() => setFilters({ [HOURS_PARAM]: String(WIDEST.hours) })}
                    />
                  ) : undefined
                }
              />
            )
          ) : (
            <Table label={t("security.page.title")}>
              <thead>
                <tr>
                  <Th>{t("security.page.columns.event")}</Th>
                  <Th>{t("security.page.columns.host")}</Th>
                  <Th>{t("security.page.columns.user")}</Th>
                  <Th>{t("security.page.columns.when")}</Th>
                </tr>
              </thead>
              <tbody>
                {items.map((event) => {
                  const kind = variantKind(eventBadgeVariant(event.event_type));
                  const isSelected = selected === event.id;
                  return (
                    <tr
                      key={event.id}
                      data-selected={isSelected || undefined}
                      style={{
                        cursor: "pointer",
                        background: isSelected ? "var(--accent-s)" : undefined,
                      }}
                      onClick={() => setSelected(event.id)}
                    >
                      {/* v3's row: the event's glyph as the lead, its
                          severity as the gutter, the event named in words over
                          the source it came from. */}
                      <Td lead={<Icon name={eventGlyph(event.event_type)} size={18} />} gutter={kind}>
                        <div className="cell-title">
                          {/* THE KEYBOARD'S WAY IN. The row opens on a click,
                              and a row is not a control; this link is,
                              named by what it opens. It stops the click so
                              the row does not write the same URL twice. */}
                          <Link
                            to={eventHref(event.id)}
                            aria-label={t("security.page.openEventAria", {
                              event: securityEventLabel(event.event_type),
                              host: event.hostname,
                              when: formatDateTime(event.ts, zone),
                            })}
                            onClick={(click) => click.stopPropagation()}
                          >
                            <StatusPill value={event.event_type} kind={kind} label={securityEventLabel(event.event_type)} />
                          </Link>
                        </div>
                        <div className="cell-sub mono">{event.source}</div>
                      </Td>
                      {/* THE HOSTNAME IS NOT BROKEN ACROSS LINES. At 390px
                          this cell lands at about forty pixels inside the
                          design's scrolling `.table-wrap`, and every host
                          came out as three fragments ("lis-", "ws-", "041"),
                          tripling the row height and destroying the one
                          identifier somebody reads a security log for. The
                          table scrolls at that width -- the design's own
                          answer -- and the page does not. */}
                      <Td className="whitespace-nowrap">{event.hostname}</Td>
                      <Td className="cell-title">
                        {event.username || (
                            <NoValue label={t("security.page.noAccount")} />
                        )}
                      </Td>
                      <Td>
                        <span className="mono" style={{ whiteSpace: "nowrap" }}>
                          {formatDateTime(event.ts, zone)}
                        </span>
                      </Td>
                    </tr>
                  );
                })}
              </tbody>
            </Table>
          )}
        </QueryState>
      </Panel>

      {/* THE EVENT OPEN, IN ITS DRAWER (redesign plan, O3 3.10), from
          `?event=`, so a row is a link and Back closes it. It was a panel
          under the list that waited, empty, for a choice. */}
      <Drawer
        open={selected != null}
        onClose={closeEvent}
        title={t("security.page.raw.title")}
        subtitle={t("security.page.raw.subtitle")}
      >
        {/* THE FETCH USED TO FAIL INTO "Select an event.". `detail.error` was
            read nowhere, so a 403 or a 503 on `GET /security-events/{id}` put
            the panel back into the state it is in before anything is selected
            — the operator's click looked like it had not registered, and the
            row they were investigating looked like it had no stored fields.
            Announced, and the three states are now distinct. */}
        {detail.isError ? (
          <p role="alert" className="form-error">
            {describeSubmitError(detail.error)}
          </p>
        ) : detail.isLoading ? (
          <p className="cell-sub">{t("security.page.raw.loading")}</p>
        ) : detail.data ? (
          <>
          {/* WHICH EVENT THIS IS, first: the drawer covers the row it came
              from (all of it, on a phone), so it says the event, the
              machine, the account and the time again before the fields. */}
          <div className="detail-meta" style={{ marginBottom: 12 }}>
            <StatusPill
              value={detail.data.event_type}
              kind={variantKind(eventBadgeVariant(detail.data.event_type))}
              label={securityEventLabel(detail.data.event_type)}
            />
            <span className="detail-context">
              {[detail.data.hostname, detail.data.username || t("security.page.noAccount"), formatDateTime(detail.data.ts, zone)].join(" · ")}
            </span>
          </div>
          {/* A JSON DUMP WAS THE DEFAULT (cycle 12, auditor A, LOW -- flagged
              there as unverified, and it was exactly the /devices/:id defect).
              `Attributes` labels the stored fields and keeps the underlying
              object behind a closed disclosure, which is what an engineer
              reading a Windows event id actually needs. */}
          <Attributes
            value={detail.data.raw}
            empty={t("security.page.raw.noFields")}
          />
          </>
        ) : null}
      </Drawer>
    </>
  );
}
