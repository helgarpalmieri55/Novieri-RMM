/**
 * The add-a-computer journey's client and site on the pages it opens (MSP
 * clients phase 3, design §4.4), carried as `?client=…&site=…`.
 *
 * THE CLIENT IS THE CONSOLE'S. A page opened from the journey under «Todos
 * mis clientes» switches the console to the journey's client, once, when it
 * is one of the person's (`/me.clients`): its lists then narrow to it and its
 * new rows go to it (rule 2 of design §3.1). A client chosen above is never
 * overridden; the journey's site is then ignored, being another client's.
 *
 * ONCE PER ARRIVAL, NOT ONCE PER MOUNT. A change of client starts the page
 * again (`lib/clientScope.tsx`'s keyed fragment) on the same URL, so a hook
 * that adopted on every mount would undo the person's own choice: picking
 * «Todos mis clientes» in the top bar -- or in another tab, which reaches
 * this one through `storage` -- would remount the page and switch straight
 * back. So the question is answered once per history entry (`location.key`),
 * when `/me` has listed the person's clients, and a remount on that entry
 * leaves the choice to the person.
 *
 * AND THE PAGE'S OWN NAVIGATIONS ARE THE SAME ARRIVAL. A page that writes its
 * query -- Enrollment's tabs push `?tab=`, the subnet tree pushes `?state=`
 * and replaces `?register=` away -- makes a new history entry, with a new
 * key, that keeps the journey's `client=`. Deciding on it afresh would switch
 * a person who had since chosen «Todos mis clientes» back at the first tab
 * click (and every other tab through `storage`). So a new entry reached while
 * the page stays mounted, on the same path with the same journey client,
 * inherits the decision already made; the page's own navigations never
 * remount it. An arrival is a page mounting on an entry: the journey's link
 * followed, the URL opened (or reloaded, or reached after signing in), Back
 * or Forward from another page -- and an entry already decided stays decided.
 *
 * THE SITE IS THE PAGE'S FIRST CHOICE. `useJourneySiteState` starts a page's
 * site at the journey's, once the console works on the journey's client; the
 * person can still choose another.
 */
import { useEffect, useRef, useState } from "react";
import { useLocation, useSearchParams } from "react-router-dom";

import { useClientScope } from "../../lib/clientScope";
import { JOURNEY_CLIENT_PARAM, JOURNEY_SITE_PARAM } from "./journey";

/** The history entries whose journey client has been decided on: outlives the remounts. */
const decided = new Set<string>();

export function useAdoptJourneyClient(): void {
  const [params] = useSearchParams();
  const { key, pathname } = useLocation();
  const wanted = params.get(JOURNEY_CLIENT_PARAM);
  const { selected, select, clients, ready } = useClientScope();
  // The path and journey client this mounted page has decided on, or stood
  // on once decided: lives exactly as long as the page does.
  const standing = useRef<string | null>(null);
  useEffect(() => {
    // Not before `/me` has listed the clients: an empty list is not "not one
    // of the person's", it is "not known yet".
    if (wanted === null || !ready) return;
    const place = `${pathname}?${JOURNEY_CLIENT_PARAM}=${wanted}`;
    const sameArrival = standing.current === place;
    standing.current = place;
    if (decided.has(key)) return;
    decided.add(key);
    // A tab, a view, a dialog closed: this page's own navigation.
    if (sameArrival) return;
    if (selected === null && clients.some((client) => client.id === wanted)) select(wanted);
  }, [wanted, ready, key, pathname, selected, clients, select]);
}

/** The journey's site, while the console works on the journey's client. */
export function useJourneySite(): string | null {
  const [params] = useSearchParams();
  const { selected } = useClientScope();
  const client = params.get(JOURNEY_CLIENT_PARAM);
  return client !== null && client === selected ? params.get(JOURNEY_SITE_PARAM) : null;
}

/** A page's site choice, starting at the journey's once that is usable. */
export function useJourneySiteState(): [string | null, (id: string | null) => void] {
  const journeySite = useJourneySite();
  const [siteId, setSiteId] = useState<string | null>(journeySite);
  const seeded = useRef(journeySite !== null);
  useEffect(() => {
    if (!seeded.current && journeySite !== null) {
      seeded.current = true;
      setSiteId(journeySite);
    }
  }, [journeySite]);
  return [siteId, setSiteId];
}
