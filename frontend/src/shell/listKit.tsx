/**
 * THE LIST PATTERN (design §2, "Lista"; DESIGN.md v4, "Lists"). One way every
 * list in the console filters, counts, narrows to a client, selects and says
 * why it is empty. A page composes these pieces; no page draws its own.
 *
 * EVERYTHING IS IN THE URL (design §2, and the reason Atera's walkthrough
 * gives for competing on consistency): a filtered view is a link, a reload
 * keeps it, Back undoes a chip.
 */
import { useCallback, useEffect, useMemo, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";

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

export interface UrlFilters<K extends string> {
  /** Every key's value, `""` when the URL does not carry it. */
  readonly values: Readonly<Record<K, string>>;
  /** Whether any of the keys holds a value: what tells "nothing yet" from "nothing matches". */
  readonly active: boolean;
  /**
   * `""` removes the key. The search (`q`) replaces the history entry, so
   * typing is not twenty Backs; any other key adds one, so Back undoes a chip.
   */
  set(key: K, value: string): void;
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
    (key: K, value: string) =>
      setParams(
        (current) => {
          const next = new URLSearchParams(current);
          if (value === "") next.delete(key);
          else next.set(key, value);
          next.delete(CURSOR);
          return next;
        },
        { replace: key === SEARCH },
      ),
    [setParams],
  );
  const clear = useCallback(
    () =>
      setParams((current) => {
        const next = new URLSearchParams(current);
        for (const key of signature.split("|")) next.delete(key);
        next.delete(CURSOR);
        return next;
      }),
    [setParams, signature],
  );
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
export function ClientFilter({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const t = useT();
  const { selected, clients, ready } = useClientScope();
  // A CLIENT THE PERSON DOES NOT HAVE (a stale or edited link) leaves the URL
  // once /me has listed theirs, rather than being drawn as "All clients" over
  // the empty list the server answers for it (decision D17).
  const foreign = ready && value !== "" && !clients.some((client) => client.id === value);
  useEffect(() => {
    if (foreign) onChange("");
  }, [foreign, onChange]);
  if (selected !== null || clients.length < 2) return null;
  return (
    <span className={value === "" ? "chip chip-select" : "chip chip-select on"}>
      <span aria-hidden="true">{t("shell.kit.client")}</span>
      <select aria-label={t("shell.kit.client")} value={value} onChange={(event) => onChange(event.target.value)}>
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
 * WHAT THE CHIP NARROWS TO, AND WHETHER THAT IS DECIDED (decisions D6, D17).
 * The top bar's choice wins when there is one, and an empty chip is the
 * console's own choice: both decided at once. Otherwise the chip's client,
 * only once `/me` has said it is one of the person's: until then `ready` is
 * false and a list reads nothing, so every client's rows never sit under one
 * client's chip. A value that is not one of theirs is never sent (the server
 * answers a malformed header with 400, and a foreign client with zero rows).
 */
export function useNarrowing(clientId: string): { client: string | null; ready: boolean } {
  const { selected, clients, ready } = useClientScope();
  if (clientId === "" || selected !== null) return { client: null, ready: true };
  if (!ready) return { client: null, ready: false };
  const mine = isClientId(clientId) && clients.some((client) => client.id === clientId);
  return { client: mine ? clientId : null, ready: true };
}

/**
 * THE API, NARROWED TO THE CHIP'S CLIENT (decision D6). `X-Client-Id` narrows
 * and never widens; the database enforces it (invariant 5a). For READS: a
 * write keeps `useApi()`, so the chip never changes where a write goes.
 */
export function useApiFor(clientId: string): ApiClient {
  const api = useApi();
  const { client } = useNarrowing(clientId);
  return useMemo(() => (client === null ? api : api.forClient(client)), [api, client]);
}

/** `?new=<kind>` opens the page's create dialog (decision D8), so the New menu can link to it. */
export function useNewParam(kind: string): { open: boolean; start: () => void; close: () => void } {
  const [params, setParams] = useSearchParams();
  const open = params.get("new") === kind;
  const start = useCallback(
    () =>
      setParams((current) => {
        const next = new URLSearchParams(current);
        next.set("new", kind);
        return next;
      }),
    [setParams, kind],
  );
  const close = useCallback(
    () =>
      setParams(
        (current) => {
          const next = new URLSearchParams(current);
          next.delete("new");
          return next;
        },
        { replace: true },
      ),
    [setParams],
  );
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
