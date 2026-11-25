import { useLocation } from "react-router-dom";

import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import { SectionNav } from "../../shell/components";
import { Icon } from "../../shell/icons";

// The module's two surfaces, in one place so `App.tsx`'s routes and the
// in-page nav cannot fall out of sync (the precedent `IpamNav` and `SlaNav`
// set).
//
// TWO ENTRIES, NOT ONE PAGE WITH A FILTER. They are not two views of one
// thing: one decides WHERE this platform may present an administrative
// credential at all, the other decides WHICH stored secret may be presented
// inside a prefix. Invariant 2a clause (d) requires both to hold
// independently, and a screen that merged them would suggest that satisfying
// one satisfies the other.
const PUSH_NAV_ITEMS: ReadonlyArray<{ to: string; label: MessageKey }> = [
  { to: "/agent-push/authorizations", label: "agentPush.nav.authorizations" },
  { to: "/agent-push/credential-scopes", label: "agentPush.nav.credentialScopes" },
];

/**
 * A NAVIGATION, NOT A TABLIST -- and the difference was not cosmetic.
 *
 * This strip announced itself as `role="tablist"` with `role="tab"` children
 * and had none of what a tablist promises: no `aria-controls`, no roving
 * tabindex, no arrow keys. A screen-reader user was told "tab 1 of 2" and then
 * found the arrow keys did nothing, which is worse than an unlabelled list of
 * links -- and the element was a `nav` landmark and a tablist at once, two
 * contradictory descriptions of the same box.
 *
 * THE KIT'S `Tabs` IS NOT THE FIX HERE. It switches in-page state through an
 * `onSelect` callback and renders `<button>`s; these two entries are ROUTES,
 * and a button cannot be middle-clicked, opened in a new tab, copied as a link
 * or restored by the browser's Back. Losing that to gain arrow keys would
 * trade a control an operator uses for one this widget never had.
 *
 * So it is what it always was underneath: two links, the current one marked
 * `aria-current="page"`, wearing the design's `.tabs` / `.tab.active` skin.
 * Nothing about the pixels, the destinations or the keyboard path changes;
 * what changes is that the announcement is now true.
 */
export function PushNav() {
  const t = useT();
  const location = useLocation();
  // THE KIT'S SECTION NAV (redesign plan, the waves' common rules): the same
  // two links, the current one `aria-current="page"`. `/agent-push` itself
  // renders the authorizations page (App.tsx), so on that path the first entry
  // points at it -- the same page -- and is the current one: a two-entry nav
  // with nothing current reads as "you are on neither of these".
  const bare = location.pathname === "/agent-push";
  return (
    <SectionNav
      label={t("agentPush.nav.label")}
      items={PUSH_NAV_ITEMS.map((item, at) => ({
        to: bare && at === 0 ? "/agent-push" : item.to,
        label: t(item.label),
        end: bare && at === 0,
      }))}
    />
  );
}

/**
 * The sentence every screen in this module carries, verbatim.
 *
 * **IT IS A REQUIREMENT, NOT A DECORATION** (spec §10.4 point 7: "one plain
 * sentence, not a tooltip"). What these two screens configure is a path that
 * presents administrative credentials to machines this platform has never
 * managed and cannot authenticate on first contact -- a property invariant 2a
 * explicitly does not claim to fix. An operator granting a prefix is entitled
 * to read that on the screen where they grant it, not in a spec.
 *
 * Rendered as ONE CHILD of `.notice`, which is a flex row shaped
 * icon-then-body: three stacked paragraphs handed to it directly lay out as
 * three columns.
 */
export function PushHazardNotice() {
  const t = useT();
  return (
    <div className="notice gold mb-4">
      <Icon name="warning" size={17} />
      <div>
        <p>
          <strong>{t("agentPush.hazard.headline")}</strong>
        </p>
        <p className="mt-1">{t("agentPush.hazard.firstContact")}</p>
        <p className="mt-1">{t("agentPush.hazard.dispatches")}</p>
      </div>
    </div>
  );
}
