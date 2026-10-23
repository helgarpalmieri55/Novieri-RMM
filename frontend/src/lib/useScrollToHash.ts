/**
 * Arriving at `/page#section` scrolls `#section` into view.
 *
 * React Router pushes a fragment and stops there -- it does not scroll, and
 * `App.tsx` wires no scroll restoration -- so without this a deep link to a
 * panel changes the URL and moves nothing on screen, a dead link wearing an
 * href. The same effect lives in `NotificationAdminPage`, `EmailIntakeAdminPage`
 * and `AgentReleasesPage`; this is it shared, for pages gaining anchors now.
 *
 * `ready`: pass `false` while what sits ABOVE the target is still loading. A
 * cold arrival otherwise scrolls first and is pushed off the target as the
 * panels above fill in, or runs before the target exists at all.
 */

import { useEffect } from "react";
import { useLocation } from "react-router-dom";

export function useScrollToHash(ready = true): void {
  const { hash } = useLocation();
  useEffect(() => {
    if (!hash || !ready) return;
    const target = document.getElementById(decodeURIComponent(hash.slice(1)));
    // Guarded: jsdom does no layout and defines no `scrollIntoView`, and a
    // TypeError here would take the whole page down.
    if (target && typeof target.scrollIntoView === "function") {
      target.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [hash, ready]);
}
