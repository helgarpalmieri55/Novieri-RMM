/**
 * THE LIST PATTERN (design §2, "Lista"; DESIGN.md v4, "Lists"). One way every
 * list in the console filters, counts, narrows to a client, selects and says
 * why it is empty. A page composes these pieces; no page draws its own.
 *
 * EVERYTHING IS IN THE URL (design §2, and the reason Atera's walkthrough
 * gives for competing on consistency): a filtered view is a link, a reload
 * keeps it, Back undoes a chip.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, type ReactNode } from "react";
import { useLocation, useNavigate, useSearchParams, type SetURLSearchParams } from "react-router-dom";

import { useT } from "../i18n/context";
import type { ApiClient } from "../lib/api";
import { useApi } from "../lib/ApiContext";
import { isClientId } from "../lib/clientChoice";
import { useClientScope } from "../lib/clientScope";
import { Btn, FilterBar, ResultCount } from "./components";
import { Icon } from "./icons";
import "./kit.css";

/** The page cursor of the lists that page; any filter change starts again from the first page. */
const CURSOR = "offset";
/** The search box's key: typing in it replaces the history entry instead of adding one per keystroke. */
const SEARCH = "q";

/**
 * WRITES A QUERY ONLY WHEN IT CHANGES (review fix round 1, item 6). Clicking
 * the chip that is already on, or the tab that is already open, used to push
 * a copy of the current entry, and the next Back was spent undoing nothing.
 * Compared from the render's own params, which is what `setSearchParams`'s
 * function form reads too (react-router 7.18.1), so nothing is lost by it.
 */
export function writeQuery(
  current: URLSearchParams,
  next: URLSearchParams,
  setParams: SetURLSearchParams,
  options: { replace?: boolean; state?: unknown } = {},
): void {
  if (next.toString() === current.toString()) return;
  setParams(next, options);
}

/** How a filter change is written: `replace` swaps the current history entry instead of adding one. */
export interface WriteOptions {
  readonly replace?: boolean;
}

export interface UrlFilters<K extends string> {
  /** Every key's value, `""` when the URL does not carry it. */
  readonly values: Readonly<Record<K, string>>;
  /** Whether any of the keys holds a value: what tells "nothing yet" from "nothing matches". */
  readonly active: boolean;
  /**
   * `""` removes the key. The search (`q`) replaces the history entry, so
   * typing is not twenty Backs; any other key adds one, so Back undoes a chip.
   * `options.replace` overrides that: `ClientFilter` drops a stale client in
   * place, so the stale link leaves nothing to come Back to. A value the URL
   * already holds writes nothing.
   */
  set(key: K, value: string, options?: WriteOptions): void;
  /** Removes every key: a new history entry, so Back brings the filters back. */
  clear(): void;
}

export function useUrlFilters<K extends string>(keys: readonly K[]): UrlFilters<K> {
  const [params, setParams] = useSearchParams();
  const signature = keys.join("|");
  const values = useMemo(
    // `keys` is usually a literal re-created on every render; its contents
    // (`signature`) are what decide whether the values changed.
    () => Object.fromEntries(signature.split("|").map((key) => [key, params.get(key) ?? ""])) as Record<K, string>,
    [params, signature],
  );
  const active = keys.some((key) => (params.get(key) ?? "") !== "");
  const set = useCallback(
    (key: K, value: string, options: WriteOptions = {}) => {
      const next = new URLSearchParams(params);
      if (value === "") next.delete(key);
      else next.set(key, value);
      next.delete(CURSOR);
      writeQuery(params, next, setParams, { replace: options.replace ?? key === SEARCH });
    },
    [params, setParams],
  );
  const clear = useCallback(() => {
    const next = new URLSearchParams(params);
    for (const key of signature.split("|")) next.delete(key);
    next.delete(CURSOR);
    writeQuery(params, next, setParams);
  }, [params, setParams, signature]);
  return { values, active, set, clear };
}

export interface FilterChipOption {
  /** `""` is "every value": the default, which writes nothing to the URL. */
  readonly value: string;
  readonly label: string;
  /** From the server. Absent is drawn as no number at all, never as 0 (The Absent Zero Rule). */
  readonly count?: number;
}

/**
 * One chip of a filter group, the same for both modes, so the single and the
 * multi-select rows can never be drawn two ways.
 */
function FilterChip({ option, on, onClick }: { option: FilterChipOption; on: boolean; onClick: () => void }) {
  return (
    <button type="button" className={on ? "chip on" : "chip"} aria-pressed={on} onClick={onClick}>
      {/* The second carrier: the selected chip says so by shape too. */}
      {on ? <Icon name="circle-check" size={13} /> : null}
      {option.label}
      {option.count === undefined ? null : <span className="n">{option.count}</span>}
    </button>
  );
}

/** One filter as a row of toggles, each with its count (DESIGN.md v4, "Chips"). */
export function FilterChips({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: readonly FilterChipOption[];
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <div className="chips" role="group" aria-label={label}>
      {options.map((option) => (
        <FilterChip
          key={option.value === "" ? "(all)" : option.value}
          option={option}
          on={option.value === value}
          onClick={() => onChange(option.value)}
        />
      ))}
    </div>
  );
}

/**
 * A MULTI-SELECT FILTER KEEPS ITS VALUES IN ONE KEY, AS A COMMA LIST (kit
 * requests batch 1, item 4): `?severity=critical,important`, which
 * `URLSearchParams` writes encoded (`critical%2Cimportant`) and reads back
 * either way. One key rather than a repeated one (`?severity=critical&
 * severity=important`) because `useUrlFilters` holds one string per key: the
 * list goes through it unchanged, `set` drops `offset` and pushes an entry as
 * for any chip, and `active` / `clear` count it like any other filter. A
 * value therefore never contains a comma (the console's filter values are
 * identifiers and enum names). Absent, or `""`, is every value.
 *
 *   const severities = parseUrlList(filters.values.severity);
 *   <MultiFilterChips values={severities} onChange={(next) => filters.set("severity", formatUrlList(next))} ... />
 */
export function parseUrlList(value: string): string[] {
  return [...new Set(value.split(",").filter((item) => item !== ""))];
}

/** The URL form of a multi-select filter's values: comma-joined, `""` (no key) for every value. */
export function formatUrlList(values: readonly string[]): string {
  return [...new Set(values.filter((item) => item !== ""))].join(",");
}

/**
 * FilterChips' MULTI-SELECT MODE (kit requests batch 1, item 4): one filter
 * that takes several values at once, drawn with `FilterChips`' own chip.
 *
 * - Each chip toggles its value; the `""` option is "every value", pressed
 *   while none is chosen, and pressing it clears the choice.
 * - The values it hands `onChange` are in the OPTIONS' ORDER, whatever order
 *   they were pressed in, so one filter has one link (pressing Important then
 *   Critical writes the same `?severity=critical,important` as the reverse).
 * - A value it is handed that no option offers (options still loading, an
 *   older link) is KEPT, after the offered ones, so a click never drops what
 *   the row cannot show.
 *
 * Keep the values in the URL with `parseUrlList` / `formatUrlList`. Single
 * select stays `FilterChips`.
 */
export function MultiFilterChips({
  label,
  options,
  values,
  onChange,
}: {
  label: string;
  options: readonly FilterChipOption[];
  values: readonly string[];
  onChange: (values: string[]) => void;
}) {
  const toggle = (value: string) => {
    if (value === "") return onChange([]);
    const chosen = new Set(values);
    if (chosen.has(value)) chosen.delete(value);
    else chosen.add(value);
    const offered = options.map((option) => option.value).filter((item) => item !== "");
    onChange([
      ...offered.filter((item) => chosen.has(item)),
      ...values.filter((item) => item !== "" && chosen.has(item) && !offered.includes(item)),
    ]);
  };
  return (
    <div className="chips" role="group" aria-label={label}>
      {options.map((option) => (
        <FilterChip
          key={option.value === "" ? "(all)" : option.value}
          option={option}
          on={option.value === "" ? values.length === 0 : values.includes(option.value)}
          onClick={() => toggle(option.value)}
        />
      ))}
    </div>
  );
}

/** "Showing 9 of 12" -- or "Showing 50" when the server gave no total, which is never guessed. */
export function ShowingCount({ shown, total }: { shown: number; total: number | null }) {
  const t = useT();
  return (
    <ResultCount>{total === null ? t("shell.kit.showingSome", { shown }) : t("shell.kit.showing", { shown, total })}</ResultCount>
  );
}

/** The bulk bar: only with a selection, so it is never a row of dead buttons (Atera's flaw). */
export function BulkBar({ count, onClear, children }: { count: number; onClear: () => void; children: ReactNode }) {
  const t = useT();
  if (count === 0) return null;
  return (
    <div className="bulkbar" role="region" aria-label={t("shell.kit.bulkActions")}>
      <span className="bulkbar-count">{t("shell.kit.selected", { count })}</span>
      {children}
      <span className="filter-spacer" />
      <Btn label={t("shell.kit.clearSelection")} variant="ghost" small onClick={onClear} />
    </div>
  );
}

/**
 * THE EMPTY LIST SAYS ITS REAL CAUSE (design §2): nothing exists yet, with the
 * page's action; or nothing matches these filters, with the way out. Atera
 * says "try adjusting the filters" on an account with no devices at all.
 */
export function ListEmpty({
  filtered,
  onClear,
  nothingYet,
  action,
  icon = "list",
}: {
  filtered: boolean;
  onClear: () => void;
  nothingYet: string;
  action?: ReactNode;
  icon?: string;
}) {
  const t = useT();
  return (
    <div className="empty">
      <span className="empty-icon">
        <Icon name={filtered ? "filter" : icon} size={26} />
      </span>
      <b>{filtered ? t("shell.kit.nothingMatches") : nothingYet}</b>
      {filtered ? <Btn label={t("shell.kit.clearFilters")} small onClick={onClear} /> : (action ?? null)}
    </div>
  );
}

/**
 * THE CLIENT CHIP (design §1.3): a list of one client's things shown with
 * every client gets a "Client" filter. Not drawn when the top bar has chosen a
 * client -- that choice already decides what this session sees -- nor for a
 * person with fewer than two clients, where it could change nothing.
 */
export function ClientFilter({
  filters,
}: {
  /**
   * The page's `useUrlFilters`, with `client` among its keys. The chip reads
   * and writes that key itself (review fix round 2, item 2): there is no
   * `onChange` a page could wire so that a stale client is dropped with a
   * history entry of its own.
   */
  filters: UrlFilters<"client">;
}) {
  const t = useT();
  const { selected, clients, ready } = useClientScope();
  const value = filters.values.client;
  // A CLIENT THE PERSON DOES NOT HAVE (a stale or edited link) leaves the URL
  // once /me has listed theirs, rather than being drawn as "All clients" over
  // the empty list the server answers for it (decision D17).
  //
  // IN PLACE, AND ONCE (review fix rounds 1 and 2). Dropped with a new
  // history entry, the stale link stayed one Back away, and Back reopened it,
  // which dropped it again: a page Back could never leave. So `replace`, and
  // the effect runs on `foreign` alone; the latest `filters` is read from a
  // ref, since its identity changes with every query.
  const latest = useRef(filters);
  useLayoutEffect(() => {
    latest.current = filters;
  });
  const foreign = ready && value !== "" && !clients.some((client) => client.id === value);
  useEffect(() => {
    if (foreign) latest.current.set("client", "", { replace: true });
  }, [foreign]);
  if (selected !== null || clients.length < 2) return null;
  return (
    <ChipSelect
      label={t("shell.kit.client")}
      options={[
        { value: "", label: t("shell.kit.allClients") },
        ...clients.map((client) => ({ value: client.id, label: client.name })),
      ]}
      value={value}
      // A person's choice is a new entry, so Back undoes it, as for any chip.
      onChange={(next) => filters.set("client", next)}
    />
  );
}

export interface ChipSelectOption {
  /** `""` is "every value": the default, which writes nothing to the URL. */
  readonly value: string;
  readonly label: string;
}

/**
 * ONE FILTER AS A CHIP THAT OPENS A CHOICE (kit requests batch 1, item 5):
 * the Client chip's `.chip-select`, for any URL key, where a row of toggles
 * would be too many (a device, a queue, an assignee) or the choice is not a
 * filter at all (an order). `ClientFilter` is drawn by it.
 *
 *   const filters = useUrlFilters(["resolution"]);
 *   <ChipSelect label="Resolution" options={[{ value: "", label: "Any resolution" }, ...]}
 *     value={filters.values.resolution} onChange={(value) => filters.set("resolution", value)} />
 *
 * - `label` is the caption drawn in the chip and the select's name. The
 *   caption is hidden from assistive technology, so it is not read twice.
 * - `ariaLabel` names the select instead where the caption alone says less
 *   ("Filter by resolution" under a "Resolution" caption).
 * - `options` are drawn in their order; a `""` option is "every value", the
 *   default. Offer the value the chip holds: a page whose list can lose it
 *   (a stale link) drops it from the URL, as `ClientFilter` does.
 * - `selected`: the chip is drawn selected (`.on`, as `FilterChips` draws a
 *   chosen chip) while it holds a value other than `""`, which is a filter in
 *   force. An ORDER narrows nothing, so a sort chip passes `false` and is never
 *   drawn as a filter.
 *
 * Written through `useUrlFilters.set`, a choice is a new history entry, so
 * Back undoes it, and it drops `offset` like any chip.
 */
export function ChipSelect({
  label,
  options,
  value,
  onChange,
  ariaLabel,
  selected = value !== "",
}: {
  label: string;
  options: readonly ChipSelectOption[];
  value: string;
  onChange: (value: string) => void;
  ariaLabel?: string;
  selected?: boolean;
}) {
  return (
    <span className={selected ? "chip chip-select on" : "chip chip-select"}>
      <span aria-hidden="true">{label}</span>
      <select aria-label={ariaLabel ?? label} value={value} onChange={(event) => onChange(event.target.value)}>
        {options.map((option) => (
          <option key={option.value === "" ? "(all)" : option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </span>
  );
}

/**
 * THE API A LIST BEHIND THE CHIP READS THROUGH, AND HOW TO KEY IT (decisions
 * D6, D17; review fix round 1, item 4). One hook, so a list cannot take the
 * API and forget the rest:
 *
 *   const { api, client, ready } = useNarrowedApi(filters.values.client);
 *   useQuery({ queryKey: ["devices", ..., client], queryFn: () => api.listDevices(...), enabled: ready });
 *
 * BOTH, ALWAYS: `client` in the key AND `ready` in `enabled` (review fix
 * round 2, item 3). `ready` never goes in the key in place of `enabled`: a
 * query keyed on `ready: false` still runs, against the API that refuses, and
 * its error is what the list would show until `/me` answers.
 *
 * - `client`: the identifier actually sent as `X-Client-Id`, or `null` when the
 *   list reads with the top bar's choice. It goes in the query key, so a change
 *   of chip reads again and no row of the last client stays on screen.
 * - `ready`: whether that is decided. The top bar's choice wins when there is
 *   one, and an empty chip is the console's own choice: both decided at once.
 *   Otherwise the chip's client, only once `/me` has said it is one of the
 *   person's. A value that is not one of theirs is never sent (the server
 *   answers a malformed header with 400, and a foreign client with zero rows).
 * - `api`: the console's API, narrowed to `client`. UNTIL `ready` IT REFUSES
 *   EVERY CALL (it rejects with `NarrowingPending`), so a list that forgot
 *   `enabled: ready` shows an error in its own tests rather than every
 *   client's rows under one client's chip.
 *
 * `X-Client-Id` narrows and never widens; the database enforces it (invariant
 * 5a). For READS: a write keeps `useApi()`, so the chip never changes where a
 * write goes.
 */
export interface NarrowedApi {
  readonly api: ApiClient;
  readonly client: string | null;
  readonly ready: boolean;
}

/** What `useNarrowedApi`'s API rejects with until the chip's client is decided. */
export class NarrowingPending extends Error {
  constructor(method: string) {
    super(
      `${method} was called before /me said whether the Client chip's value is one of this person's clients; ` +
        "read with `enabled: ready` from useNarrowedApi",
    );
    this.name = "NarrowingPending";
  }
}

/** The API while nothing is decided: every method rejects, and nothing reaches the network. */
const PENDING_API: ApiClient = new Proxy({} as ApiClient, {
  get(_target, prop) {
    if (typeof prop !== "string" || prop === "then") return undefined;
    if (prop === "forClient") return () => PENDING_API;
    return () => Promise.reject(new NarrowingPending(prop));
  },
});

export function useNarrowedApi(clientId: string): NarrowedApi {
  const consoleApi = useApi();
  const { selected, clients, ready: known } = useClientScope();
  const decided = clientId === "" || selected !== null;
  const ready = decided || known;
  const client =
    !decided && known && isClientId(clientId) && clients.some((item) => item.id === clientId) ? clientId : null;
  const api = useMemo(
    () => (!ready ? PENDING_API : client === null ? consoleApi : consoleApi.forClient(client)),
    [consoleApi, client, ready],
  );
  return { api, client, ready };
}

/**
 * A LINK OUT OF A LIST UNDER THE CLIENT CHIP KEEPS THE CHIP (kit requests
 * batch 2, K1; general ruling 2). A tile or a figure counted under the chip
 * counted that client's rows, so the list it opens is that client's too:
 *
 *   const { client } = useNarrowedApi(filters.values.client);
 *   <Tile to={withClient("/health?state=open", client)} ... />
 *
 * `client` is the client actually read: `useNarrowedApi`'s `client`, `null`
 * without one (Health's `withClient`, which this is), or a chip's raw value,
 * where `""` is no client too (O4's `withChip`). With none, `to` comes back
 * unchanged; otherwise `client=<id>` is added after `to`'s own query. `to`
 * names no client of its own.
 */
export function withClient(to: string, client: string | null): string {
  if (client === null || client === "") return to;
  return `${to}${to.includes("?") ? "&" : "?"}client=${encodeURIComponent(client)}`;
}

/**
 * Marks the history entry `start()` pushed, with this document's identity:
 * the entry a `close()` may go Back from. A reload is another document, and a
 * link that arrived with `?new=` carries no mark.
 */
const OPENED_IN = Math.random().toString(36).slice(2);
interface OpenedMark {
  readonly kitNewOpened?: { readonly kind: string; readonly document: string };
}

/**
 * `?new=<kind>` opens the page's create dialog (decision D8), so the New menu
 * can link to it.
 *
 * CLOSING UNDOES OPENING (review fix round 1, item 6). `start()` adds a
 * history entry, so Back closes the dialog. `close()` then goes Back too when
 * the current entry is the one this document's `start()` added; replacing
 * instead left two copies of the list behind, and the next Back stayed on it.
 * A dialog that arrived in a link, or one whose query has moved on since
 * (another filter written meanwhile), is closed in place.
 *
 * A FORM IN THE DIALOG passes `search={["new"]}` to its `UnsavedGuard`, so
 * Back or Close with a change in it asks first (see `useUnsavedGuard`).
 */
export function useNewParam(kind: string): { open: boolean; start: () => void; close: () => void } {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const open = params.get("new") === kind;
  const start = useCallback(() => {
    const next = new URLSearchParams(params);
    next.set("new", kind);
    writeQuery(params, next, setParams, { state: { kitNewOpened: { kind, document: OPENED_IN } } satisfies OpenedMark });
  }, [params, setParams, kind]);
  const close = useCallback(() => {
    if (!params.has("new")) return;
    const mark = (location.state as OpenedMark | null)?.kitNewOpened;
    if (mark?.kind === params.get("new") && mark.document === OPENED_IN) {
      void navigate(-1);
      return;
    }
    const next = new URLSearchParams(params);
    next.delete("new");
    writeQuery(params, next, setParams, { replace: true });
  }, [params, setParams, location.state, navigate]);
  return { open, start, close };
}

/** Marks the history entry an editor's `open()` pushed: what it edits, which record, and this document. */
interface EditOpenedMark {
  readonly kitEditOpened?: { readonly kind: string; readonly id: string; readonly document: string };
}

/**
 * `?edit=<id>` opens the page's editor for one record (kit requests batch 1,
 * item 6): the twin of `useNewParam`, so an edit is a link as a create is.
 *
 *   const created = useNewParam("policy");   // ?new=policy
 *   const edit = useEditParam("policy");     // ?edit=<the policy's id>
 *   <Btn label="Edit" onClick={() => edit.open(policy.id)} />
 *   {edit.id !== "" ? <PolicyEditor id={edit.id} onClose={edit.close} /> : null}
 *
 * - `id`: the record the URL names, `""` when it names none (the kit's
 *   absent value, as `useUrlFilters`'s).
 * - `open(id)`: adds a history entry, so Back closes the editor. Opening the
 *   editor already open writes nothing.
 * - `close()`: CLOSING UNDOES OPENING, exactly as `useNewParam`'s does. It
 *   goes Back when the current entry is the one this editor's `open()` added
 *   in this document; an editor that arrived in a link, or one whose query has
 *   moved on since (a filter written meanwhile), is closed in place. Writing
 *   `?edit=` through `useUrlFilters` instead pushed a second entry on close,
 *   and Back reopened the editor (O4 review, item 7).
 * - `kind` names what the editor edits, as `useNewParam`'s does, and is part
 *   of the mark: only this editor's `close()` goes Back from the entry its
 *   `open()` added, never another editor's of the same page.
 *
 * A FORM IN THE EDITOR passes `search={["edit"]}` (or `["new", "edit"]` when
 * the same form creates too) to its `UnsavedGuard`, so Back or Close with a
 * change in it asks first (see `useUnsavedGuard`).
 */
export function useEditParam(kind: string): { id: string; open: (id: string) => void; close: () => void } {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const id = params.get("edit") ?? "";
  const open = useCallback(
    (record: string) => {
      const next = new URLSearchParams(params);
      next.set("edit", record);
      writeQuery(params, next, setParams, {
        state: { kitEditOpened: { kind, id: record, document: OPENED_IN } } satisfies EditOpenedMark,
      });
    },
    [params, setParams, kind],
  );
  const close = useCallback(() => {
    if (!params.has("edit")) return;
    const mark = (location.state as EditOpenedMark | null)?.kitEditOpened;
    if (mark?.kind === kind && mark.id === params.get("edit") && mark.document === OPENED_IN) {
      void navigate(-1);
      return;
    }
    const next = new URLSearchParams(params);
    next.delete("edit");
    writeQuery(params, next, setParams, { replace: true });
  }, [params, setParams, location.state, navigate, kind]);
  return { id, open, close };
}

/** The head of a list, flush inside its bare panel: search, chips, then the count at the end. */
export function ListToolbar({ search, chips, end }: { search?: ReactNode; chips?: ReactNode; end?: ReactNode }) {
  return (
    <FilterBar>
      {search}
      {chips ? <div className="list-chips">{chips}</div> : null}
      <span className="filter-spacer" />
      {end}
    </FilterBar>
  );
}
