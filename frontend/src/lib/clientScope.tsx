/**
 * THE CLIENT CHOSEN IN THE TOP BAR, FOR COMPONENTS (phase 3 design §1.1, §1.4).
 *
 * The choice itself lives outside React (`./clientChoice`), because the two
 * things that send it -- `ApiClient` (`X-Client-Id`) and the live socket
 * (`?client=`) -- are not components. This module is what components read:
 * the choice, the person's clients from `/me`, and whether the two agree.
 *
 * `ClientScopeProvider` is mounted ONCE, around the signed-in shell
 * (`App.tsx`'s `RequireAuth`), and does what a change of client owes the page:
 *
 * 1. IT EMPTIES THE QUERY CACHE before anything renders again (§1.4). Every
 *    cached answer was read under the previous choice; none may be shown
 *    under the next one. The one query kept is the setup gate's, which
 *    names no client and lives above this provider (`emptyForNewClient`).
 * 2. IT STARTS THE PAGE AGAIN (a `key` on its children). Emptying removes the
 *    queries, but a mounted component keeps the result it last rendered until
 *    something renders it again -- so the shell and the page under it are
 *    mounted afresh, and every query they hold is asked again with the new
 *    header. Their own state (a half-filled form for the previous client)
 *    goes with them, which is the point.
 * 3. IT FALLS BACK TO EVERY CLIENT, and says so once, when the choice is not
 *    one of `/me`'s clients -- archived, or the person lost access (§1.1, §6).
 *
 * A choice made in another tab arrives by the browser's `storage` event and
 * is handled exactly like one made here.
 *
 * INVARIANT 6: THIS GATES NOTHING. The server narrows by the header and never
 * widens: a client outside the person's scope answers empty lists and 404s.
 */

import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import {
  Fragment,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";

import { translate } from "../i18n/core";
import { Toaster, type ToastItem } from "../shell/Toaster";
import type { ApiError, Me, MeClient } from "./api";
import { useApi } from "./ApiContext";
import {
  CLIENT_STORAGE_KEY,
  adoptStoredClient,
  selectedClient,
  setSelectedClient,
  subscribeClient,
} from "./clientChoice";

export { CLIENT_STORAGE_KEY };

export interface ClientScope {
  /** The client chosen in the top bar, or `null` for "All my clients". */
  selected: string | null;
  /** Choose a client, or `null` for every client: stored, and sent from the next request on. */
  select(id: string | null): void;
  /**
   * The person's clients (`/me`), by name, Unassigned last. Empty until `/me`
   * first answers. While it is asked again after a change, the list it last
   * gave, by id, name and kind only (`ClientChoice`).
   */
  clients: readonly ClientChoice[];
  /** `/me` has answered, and the choice is one of its clients (or every client). */
  ready: boolean;
}

/** What the selector draws of a client: its id, name and kind -- nothing else of `/me`'s. */
export type ClientChoice = Pick<MeClient, "id" | "name" | "kind">;

/**
 * THE LIST `/me` LAST GAVE, kept by the provider above the remount so the
 * selector stays drawn while `/me` is asked again under the new choice. Ids,
 * names and kinds of the person's own clients and NOTHING ELSE: no
 * permission, no module, no other answer read for the previous client. It
 * goes when the provider does, at sign-out.
 */
const LastListed = createContext<readonly ClientChoice[]>([]);

/** By name, with the Unassigned client last (design §1.1). */
export function orderClients<T extends ClientChoice>(clients: readonly T[]): T[] {
  return [...clients].sort(
    (a, b) =>
      Number(a.kind === "unassigned") - Number(b.kind === "unassigned") ||
      a.name.localeCompare(b.name),
  );
}

/**
 * WHAT A CHANGE OF CLIENT EMPTIES: every query but the setup gate's, and
 * every mutation, the way `clear()` does -- in-flight requests are cancelled
 * and whatever they answer lands nowhere.
 *
 * `["setup", ...]` IS KEPT because it is `App.tsx`'s `SetupGate`, which sits
 * ABOVE this provider and is not started again with the page. Its request
 * bypasses `ApiClient`, so it never carried `X-Client-Id`, and it asks the
 * one thing about the platform no client changes: whether it has been
 * claimed. Emptied with the rest, the gate's next re-render found no query,
 * drew "Starting..." in place of the whole routes tree, and asked again.
 * Nothing read under the previous client survives this.
 */
function emptyForNewClient(queryClient: QueryClient): void {
  queryClient.removeQueries({ predicate: (query) => query.queryKey[0] !== "setup" });
  queryClient.getMutationCache().clear();
}

function useMeQuery() {
  const api = useApi();
  return useQuery<Me, ApiError>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
}

function useSelected(): string | null {
  return useSyncExternalStore(subscribeClient, selectedClient, selectedClient);
}

export function useClientScope(): ClientScope {
  const me = useMeQuery();
  const selected = useSelected();
  const last = useContext(LastListed);
  const clients = useMemo(() => (me.data ? orderClients(me.data.clients ?? []) : last), [me.data, last]);
  const ready =
    me.data !== undefined && (selected === null || clients.some((client) => client.id === selected));
  return { selected, select: setSelectedClient, clients, ready };
}

let noticeSeq = 0;

export function ClientScopeProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const me = useMeQuery();
  const selected = useSelected();
  const [notices, setNotices] = useState<ToastItem[]>([]);

  // 1. Inside the change itself: listeners run synchronously when the choice
  //    moves, and React only renders after the change has returned.
  useEffect(() => subscribeClient(() => emptyForNewClient(queryClient)), [queryClient]);

  // A choice made in another tab. `key === null` is that tab clearing storage.
  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key === CLIENT_STORAGE_KEY || event.key === null) adoptStoredClient();
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  // 3. A choice /me does not list. `undefined` is an API older than phase 3,
  //    which names no clients: there is nothing to check it against.
  const listed = me.data?.clients;
  useEffect(() => {
    if (selected === null || listed === undefined) return;
    if (listed.some((client) => client.id === selected)) return;
    noticeSeq += 1;
    setNotices([{ id: `client-gone-${noticeSeq}`, title: translate("lib.clientScope.gone"), tone: "info" }]);
    setSelectedClient(null);
  }, [selected, listed]);

  // What the selector draws while /me is asked again after a change (`LastListed`).
  const [last, setLast] = useState<readonly ClientChoice[]>([]);
  useEffect(() => {
    if (listed !== undefined) setLast(orderClients(listed).map(({ id, name, kind }) => ({ id, name, kind })));
  }, [listed]);

  const dismiss = useCallback(
    (id: string) => setNotices((current) => current.filter((notice) => notice.id !== id)),
    [],
  );

  return (
    <>
      <LastListed.Provider value={last}>
        {/* 2. A new key is a new page: every query asked again, every form empty. */}
        <Fragment key={selected ?? "all"}>{children}</Fragment>
      </LastListed.Provider>
      <Toaster toasts={notices} onDismiss={dismiss} />
    </>
  );
}
