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
      {options.map((option) => {
        const on = option.value === value;
        return (
          <button
            key={option.value === "" ? "(all)" : option.value}
            type="button"
            className={on ? "chip on" : "chip"}
            aria-pressed={on}
            onClick={() => onChange(option.value)}
          >
            {/* The second carrier: the selected chip says so by shape too. */}
            {on ? <Icon name="circle-check" size={13} /> : null}
            {option.label}
            {option.count === undefined ? null : <span className="n">{option.count}</span>}
          </button>
        );
      })}
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
    <span className={value === "" ? "chip chip-select" : "chip chip-select on"}>
      <span aria-hidden="true">{t("shell.kit.client")}</span>
      {/* A person's choice is a new entry, so Back undoes it, as for any chip. */}
      <select aria-label={t("shell.kit.client")} value={value} onChange={(event) => filters.set("client", event.target.value)}>
        <option value="">{t("shell.kit.allClients")}</option>
        {clients.map((client) => (
          <option key={client.id} value={client.id}>
            {client.name}
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
