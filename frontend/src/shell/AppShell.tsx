/**
 * The application shell — v3's `.shell` / `.rail` / `.top` / `.content` layout
 * (`docs/design/itms-prototype-v3.html`), as one component every page renders
 * inside. The rail is `.sidebar` and the column beside it `.app-shell` here,
 * because the console's rail collapses and becomes a drawer, which v3's static
 * grid column does not.
 *
 * Pages migrate onto this one at a time (UI port spec §6), so during the
 * migration an unported page renders inside the new chrome with its old
 * content. That is deliberate: a half-migrated console should look like one
 * application mid-change, not like two applications stapled together.
 */

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";

import { useT } from "../i18n/context";
import { AccountMenu } from "./AccountMenu";
import {
  CommandPalette,
  type CommandPaletteItem,
  type PaletteCloseReason,
} from "./CommandPalette";
import {
  healthLabel,
  healthSignal,
  NOTHING_MEASURED,
  platformLabel,
  platformSignal,
  type HealthReading,
} from "./healthSignal";
import { Icon } from "./icons";
import { ADD_COMPUTER_TO } from "./nav";
import { Sidebar } from "./Sidebar";
import "./shell.css";

export interface AppShellProps {
  permissions: readonly string[];
  /**
   * Optional modules on for the current choice (`useEnabledModules`, phase 3
   * design §2). Absent -- the shell's own tests, `/__preview` -- every one.
   */
  modules?: ReadonlySet<string>;
  orgName: string;
  /**
   * Breadcrumb tail, supplied by the page. OPTIONAL, and its absence is a
   * real answer rather than a missing one: a route in no section (/profile,
   * reached from the account menu) draws the crumb with no section segment,
   * because naming it after a section that does not contain it is what the
   * review filed.
   */
  section?: string;
  /** Where the breadcrumb's section segment goes. Omitted rather than guessed:
   *  a crumb that links nowhere is the false affordance the audit filed. */
  sectionTo?: string;
  title: string;
  /** Live sidebar counts keyed by route; absent means no badge. */
  badges?: Readonly<Record<string, number>>;
  /** What the top bar's health glyph reports, from `useHealthReading`. Absent
   *  means nothing measured it, which is a state of its own and NOT "fine". */
  health?: HealthReading;
  user: { name: string; email?: string; role: string };
  onSignOut: () => void;
  /** True once a silent renewal has failed: the session is genuinely ending. */
  sessionExpiring?: boolean;
  onSignIn?: () => void;
  /** What the palette can jump to. Supplied by the app, never invented here. */
  searchItems?: readonly CommandPaletteItem[];
  /** Told each query so the app can look records up for it. */
  onSearchQueryChange?: (query: string) => void;
  /** True while record lookups for the current query are in flight. */
  searchPending?: boolean;
  /** What the search field says it finds, for this account. */
  searchPlaceholder?: string;
  /** The no-results explanation of what search covers, for this account. */
  searchHint?: string;
  /** Omitted when the operator lacks `ai.use`; the ask row then never renders. */
  onAsk?: (question: string) => void;
  /**
   * The top bar's client selector (phase 3 design §1.1), drawn first in the
   * cluster. A SLOT, NOT A COMPONENT THE SHELL RENDERS ITSELF: the selector
   * reads `/me` through the query client, and this shell also renders where
   * there is none -- its own tests and `/__preview`.
   */
  clientSelector?: ReactNode;
  children: ReactNode;
}

/**
 * THE TOP BAR'S HEALTH GLYPH, REPORTING STATE IN THREE CARRIERS, not one.
 *
 * It used to be `<Icon name="warning">` — an alert triangle, one colour, on
 * every page whatever the fleet was doing, beside a count that was structurally
 * always zero. Now: the rail's own `health` glyph, tinted green/amber/red by
 * the signal; a pip whose SHAPE differs per state, because SC 1.4.1 forbids
 * colour being the only carrier and red-amber-green is the worst possible trio
 * for it; and an accessible name that says the condition in words.
 * `healthSignal.ts` owns which state is which.
 *
 * WHY IT IS ITS OWN COMPONENT, and it is the only reason. `/__preview` is where
 * the design pass and the browser axe sweep (`scripts/a11y-browser.mjs`) look at
 * this console, and a shell renders ONE top bar, so the harness could only ever
 * see one of the four states — it saw `unknown`, permanently, because
 * `PreviewPage` passed `badges` and no `health`. Green, amber and red had never
 * been rendered anywhere a human or a browser could measure them. The preview
 * needs to draw all four, and the alternative to exporting this is transcribing
 * its markup into the harness, where it would drift from the thing it claims to
 * show.
 */
export function HealthGlyph({ reading }: { reading: HealthReading }) {
  useT();
  const signal = healthSignal(reading);
  const count = reading.openAlerts ?? 0;
  return (
    /* v3 `.ib`: `top-icon-btn` is the same control under its older name, kept
       because the stylesheet guards (`responsive.test.ts`) scan for it. */
    <Link className={`ib top-icon-btn health-${signal}`} to="/health" aria-label={healthLabel(reading)}>
      <Icon name="heartbeat" size={17} />
      <span className="health-pip" aria-hidden="true" />
      {/* THE COUNT APPEARS ONLY WHERE THE RAIL DOES NOT. The sidebar already
          badges /health, and one number rendered in two places is the
          redundancy the audit filed against the tenant control ("two controls
          compete to represent the same thing"). Below 1024px the rail is a shut
          drawer and its badge is invisible — that is the one viewport where
          this earns its place, so `.counter-dot` is hidden above it. */}
      {count > 0 ? (
        <span className="counter-dot" aria-hidden="true">
          {count > 99 ? "99+" : count}
        </span>
      ) : null}
    </Link>
  );
}

/**
 * THE TOP BAR'S PLATFORM-STATUS GLYPH, and the hole it was put here to fill.
 *
 * RED CAME FROM A ROUTE THE GLYPH CARRYING IT WAS NOT GATED ON. `down` — a
 * dependency the whole control plane stands on being unreachable, the single
 * most severe thing this console can report — is `GET /platform/status`,
 * `platform.status.read`. `HealthGlyph` above is gated `health.read`, because
 * `/health` is where it goes. Those are separate grants, so an operator
 * holding platform-status and NOT health saw no glyph at all and a downed
 * dependency was invisible to them on every page of the console.
 *
 * WIDENING THAT GATE WAS THE WRONG ANSWER, and it is worth writing down which
 * wrong answer it is: `health.read || platform.status.read` would render a
 * control pointing at `/health` for an operator who gets a 403 from it. That
 * is the false-affordance finding this chrome has already been through once,
 * reintroduced in the name of fixing something else.
 *
 * SO PLATFORM STATUS GETS ITS OWN CONTROL AND ITS OWN DESTINATION.
 * `/platform-status` is a shipped route gated on the code its own request
 * requires (`shell/nav.ts`), which is the test `.top-actions` puts on every
 * icon in it: an icon here is a promise that something real is behind it.
 *
 * AN OPERATOR HOLDING BOTH CODES SEES BOTH, AND THAT IS NOT REDUNDANCY. The
 * health glyph escalating to red on a downed dependency is `healthSignal.ts`'s
 * stated rule and is deliberately untouched — it says the fleet picture is not
 * trustworthy. This one says where to go and read why.
 *
 * NO COUNT ON THIS ONE. `/platform/status` returns a rollup and a probe list,
 * not a queue of things to work through; a number here would invent a backlog.
 */
function PlatformGlyph({ reading }: { reading: HealthReading }) {
  useT();
  const signal = platformSignal(reading.platform);
  return (
    <Link
      className={`ib top-icon-btn health-${signal}`}
      to="/platform-status"
      aria-label={platformLabel(reading.platform)}
    >
      <Icon name="activity" size={17} />
      {/* The same pip `HealthGlyph` draws, and for the same reason: SC 1.4.1
          forbids colour being the only carrier, and `shell.css` gives each
          state a different SILHOUETTE. Sharing the `health-*` vocabulary is
          what lets one set of rules paint both glyphs. */}
      <span className="health-pip" aria-hidden="true" />
    </Link>
  );
}

/** Where the collapsed-rail preference lives between visits. */
const COLLAPSED_KEY = "itms.sidebar.collapsed";

function readCollapsed(): boolean {
  // Storage throws in a handful of real configurations -- Safari private
  // browsing, an embedded webview with site data blocked -- and a chrome
  // component that cannot render because it could not read a cosmetic
  // preference is a worse bug than the preference being forgotten.
  try {
    return window.localStorage.getItem(COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

function writeCollapsed(collapsed: boolean): void {
  try {
    window.localStorage.setItem(COLLAPSED_KEY, collapsed ? "1" : "0");
  } catch {
    /* see readCollapsed */
  }
}

export function AppShell({
  permissions,
  modules,
  orgName,
  section,
  sectionTo,
  title,
  badges,
  health,
  user,
  onSignOut,
  sessionExpiring = false,
  onSignIn,
  searchItems = [],
  onSearchQueryChange,
  searchPending,
  searchPlaceholder,
  searchHint,
  onAsk,
  clientSelector,
  children,
}: AppShellProps) {
  const t = useT();
  const [collapsed, setCollapsed] = useState(readCollapsed);

  // THE DRAWER, for viewports below 1024px where the sidebar is off-canvas
  // (`shell.css`, cycle 10). Deliberately NOT persisted: a drawer is modal
  // over the page, and an operations console that reopens a full-screen menu
  // on every visit has made the menu the product. The desktop `collapsed`
  // rail IS persisted, because that is a standing preference about density
  // rather than a transient state -- the audit filed the missing persistence
  // against the collapse button, not against this.
  const [navOpen, setNavOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  /**
   * WHAT `/` INTERRUPTED, so Escape can put the operator back on it.
   *
   * The search used to be a dialog and got focus return for free: a modal
   * remembers its trigger, and its trigger was the only way in. An inline
   * field has two ways in and they want opposite things. Reached by clicking
   * or tabbing to it, the field IS where focus belongs and Escape should leave
   * it there. Reached by `/` from somewhere else on the page, the operator
   * never chose to leave that somewhere else -- so Escape owes it back, and
   * nothing but this ref knows what it was.
   *
   * Null whenever the panel was opened from the field itself, which is why
   * `closePalette` can restore unconditionally without dragging focus around.
   */
  const paletteOpener = useRef<HTMLElement | null>(null);
  /** Read inside the document-level `/` handler, which is bound once. */
  const paletteOpenRef = useRef(false);
  paletteOpenRef.current = paletteOpen;
  /** The search field, so the shortcut can ask whether it is on screen. */
  const paletteField = useRef<HTMLInputElement>(null);
  const navToggleRef = useRef<HTMLButtonElement>(null);
  const location = useLocation();

  const closePalette = useCallback((reason: PaletteCloseReason) => {
    const opener = paletteOpener.current;
    paletteOpener.current = null;
    setPaletteOpen(false);
    // ONLY ON ESCAPE. `dismiss` means focus has already gone somewhere the
    // operator chose -- the next control they tabbed to, or whatever they
    // clicked -- and hauling it back would be a focus trap assembled out of
    // politeness. `select` is about to navigate, and the page that renders
    // decides where focus goes.
    if (reason !== "escape") return;
    if (opener?.isConnected) opener.focus();
  }, []);

  /**
   * WHAT THE HEALTH GLYPH SAYS, and it now says something.
   *
   * ONE READING FEEDS BOTH the top bar and the rail: `App.tsx` builds `badges`
   * from this same object, so the two counts cannot disagree about how many
   * alerts are open. It used to be the other way round — the top bar read
   * `badges` — and the map was never passed by anybody, so the count was
   * permanently 0 and the dot had never rendered on a deployed console.
   *
   * THE COUNT IS `aria-hidden` and the label carries the number in words: a
   * bare "3" announced next to an icon named "Alerts" is two facts a screen
   * reader has to assemble, and it reads the digit as a list position.
   */
  const reading = health ?? NOTHING_MEASURED;

  const closeNav = useCallback(() => {
    setNavOpen((open) => {
      // Focus goes back to the control that opened the drawer, but only if the
      // drawer was actually open -- otherwise a route change would yank focus
      // to the hamburger on every navigation.
      if (open) navToggleRef.current?.focus();
      return false;
    });
  }, []);

  /**
   * THE TAB SAID "ITMS" ON ALL FOURTEEN ROUTES. WCAG 2.4.2 asks a page to
   * identify itself, and a browser tab is the one place a person with a dozen
   * consoles open actually reads that. `title` is the nav item's own label, so
   * this cannot drift from what the sidebar and the page heading say.
   *
   * Restored on unmount so the login screen does not inherit the last page's
   * name.
   */
  useEffect(() => {
    const previous = document.title;
    document.title = `${title} · ITMS`;
    return () => {
      document.title = previous;
    };
  }, [title]);

  // A navigation is the drawer's whole purpose, so completing one closes it.
  useEffect(() => {
    setNavOpen(false);
  }, [location.pathname]);

  /**
   * "/" OPENS SEARCH, unless the operator is already typing somewhere. The
   * guard is the whole trick: without it, pressing "/" inside a ticket note
   * eats the character and opens a palette nobody asked for.
   */
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      const typing =
        target?.isContentEditable ||
        ["INPUT", "TEXTAREA", "SELECT"].includes(target?.tagName ?? "");
      if (typing) return;
      // The search field is itself an INPUT, so the guard above already lets a
      // slash typed INTO the search be a slash. This is the other half: with
      // the panel open, focus can also be on a result, and a shortcut that
      // re-opened what is already open would throw away the recorded opener.
      if (paletteOpenRef.current) return;
      // AND NOT AT ALL WHERE THE FIELD IS NOT ON SCREEN. Opening a panel
      // anchored to a hidden field sets state nothing renders and nothing can
      // clear -- the field cannot take focus, so it cannot blur, so it never
      // closes, and every later `/` is swallowed by the guard above. Asking
      // the element beats restating a breakpoint here, where it would drift
      // from the stylesheet that owns it.
      //
      // BELOW 1024px THE FIELD IS NOW COLLAPSED RATHER THAN REMOVED
      // (`shell.css`), so this answers true on a handset and the shortcut
      // works there -- which is the point of the collapse. The guard stays:
      // it is a claim about the element, not about a width, and it is what
      // keeps this correct if the chrome hides the control again.
      // `checkVisibility` is absent in jsdom, which has no layout and no media
      // queries; where it cannot answer, the honest default is to let the
      // shortcut through rather than block it on a guess.
      const field = paletteField.current;
      if (field?.checkVisibility && !field.checkVisibility()) return;
      event.preventDefault();
      // Read before the panel opens and takes focus for itself.
      paletteOpener.current = target && target !== document.body ? target : null;
      setPaletteOpen(true);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  // Escape closes it, like every other modal surface in this console.
  useEffect(() => {
    if (!navOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeNav();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [navOpen, closeNav]);

  // `body.nav-open` is what the stylesheet keys the slide-in and the scroll
  // lock off, matching how the design already drives `body.sidebar-collapsed`.
  useEffect(() => {
    document.body.classList.toggle("nav-open", navOpen);
    return () => document.body.classList.remove("nav-open");
  }, [navOpen]);

  // The design drives the collapsed layout from a class on <body> (the
  // `body.sidebar-collapsed` rules in shell.css are copied verbatim from the
  // prototype), so the class goes where those rules expect it rather than
  // being reimplemented against a different hook. Removed on unmount so the
  // login screen never inherits it.
  useEffect(() => {
    document.body.classList.toggle("sidebar-collapsed", collapsed);
    return () => document.body.classList.remove("sidebar-collapsed");
  }, [collapsed]);

  // "its collapsed state is not persisted anywhere I could observe" -- the
  // audit, on the collapse control. Density is a standing preference; making
  // the operator re-set it on every load is the kind of small tax that makes
  // a tool feel like it is not paying attention.
  useEffect(() => {
    writeCollapsed(collapsed);
  }, [collapsed]);

  return (
    <div className="shell">
      {/* THIRTY-SIX TAB STOPS BEFORE THE CONTENT, on every route, was what the
          auditors measured: the whole sidebar, every time, for anyone
          navigating by keyboard. First in the DOM so it is the first stop,
          visible only when focused (`.skip-link` in shell.css). */}
      <a className="skip-link" href="#main">
        {t("shell.app.skipToMain")}
      </a>
      {/* The scrim is a BUTTON, not a div with an onClick: tapping outside to
          dismiss is a real action, and it should be reachable and announced
          like one rather than being a click target only a mouse can find. */}
      {navOpen ? (
        <button type="button" className="nav-scrim" aria-label={t("shell.app.dismissNavigation")} onClick={closeNav} />
      ) : null}

      <Sidebar
        permissions={permissions}
        modules={modules}
        collapsed={collapsed}
        onToggleCollapsed={() => setCollapsed((value) => !value)}
        badges={badges}
        orgName={orgName}
      />

      <div className="app-shell">
        <header className="topbar">
          {/* THE ONLY WAY TO REACH NAVIGATION below 1024px, so it is a real
              44x44 target rather than the 30x30 chevron the audit measured.
              Hidden by the stylesheet at desktop widths, where the sidebar is
              always on screen and a second control for it would be noise. */}
          <button
            type="button"
            className="nav-toggle"
            ref={navToggleRef}
            aria-label={navOpen ? t("shell.app.closeNavigation") : t("shell.app.openNavigation")}
            aria-expanded={navOpen}
            aria-controls="sidebar"
            onClick={() => setNavOpen((open) => !open)}
          >
            <Icon name={navOpen ? "x" : "menu-2"} size={18} />
          </button>

          {/* THE BREADCRUMB AND THE PAGE TITLE ARE NOT IN THE BAR ANY MORE.
              v3 draws the crumb as the first line of `.content`, above the
              page's own `PageHead` h1, and draws no title in the bar at all;
              the bar is search-first. That is a MOVE of the crumb (see `<main>`
              below) and a removal of a duplicate: the page head already says
              the title, and `document.title` still carries it to the tab. */}

          {/* THE TENANT USED TO BE HERE TOO, as a second `<button>` with no
              handler, in a different visual language from the sidebar card
              showing the same tenant. Both auditors filed both halves: a dead
              control in the primary header, and one object rendered twice so
              the user has to work out they are the same thing. It is gone
              rather than wired up, on the auditors' own recommendation -- the
              sidebar foot is where workspace identity belongs and the header
              right is the account -- which also frees the most valuable slot
              in the chrome for something that will earn it. */}
          {/* THE FIELD AND ITS RESULTS ARE ONE CONTROL AND LIVE IN ONE PLACE.
              They used to be two: a `TopSearch` button here, and a centred
              modal with a second search input in it, mounted below the header.
              "The search bar is a pop-up, it shouldn't be like that" is the
              operator on the result, and they are objecting to the dialog, not
              to what it could do -- so the routing survives intact and the
              scrim does not. The panel drops from this field the way the
              entity picker's list drops from its own (`.picker-list`), which
              is the pattern this console already had for exactly this. */}
          <CommandPalette
            open={paletteOpen}
            onOpen={() => setPaletteOpen(true)}
            onClose={closePalette}
            fieldRef={paletteField}
            items={searchItems}
            onQueryChange={onSearchQueryChange}
            pending={searchPending}
            placeholder={searchPlaceholder}
            emptyHint={searchHint}
            /* One name for the AI feature, the rail's: "Ask the Assistant".
               Omitted entirely when `onAsk` is, so the affordance never points
               at a 403. */
            ask={onAsk ? { onAsk, label: t("shell.app.assistantObject") } : undefined}
          />

          {/* THE ICON CLUSTER, and what is deliberately NOT in it.
              Atera's top bar carries six controls; this carries one action and
              three destinations, because the operator's own rule — "I don't
              want dead tiles everywhere" — does not stop applying at the top of
              the page. An icon is a promise that something is behind it.

              NOT PRESENT, and why: a "+ New" quick-create needs a menu of
              create actions that does not exist yet; a notification bell needs
              a personal feed, and /notifications is admin configuration rather
              than an inbox. Both are worth building. Neither is worth faking.

              Each is gated on the permission its destination requires, so the
              cluster never offers a technician a 403. */}
          <div className="top-actions">
            {/* THE CLIENT EVERYTHING IN THIS CLUSTER, AND ON THE PAGE, IS FOR
                (phase 3 design §1.1): first, because it is the context of the
                rest -- "Add a computer" included. */}
            {clientSelector}
            {/* THE FIRST THING ANYONE DOES WITH THIS PRODUCT, on every route.

                ONE "ADD A COMPUTER" ENTRY. The review (2026-09-23) counted six
                install entry points; this and the Devices head now lead to the
                same page, whose first panel is the one-machine install (the
                installers and the command), with the network sweep below it
                for many machines at once.

                Kept on the `devices.read` gate -- the technician who installs
                -- while each destination refuses an under-privileged caller
                server-side. LABELLED, AND PRIMARY: this sits in the chrome, so
                it never competes with a page's own primary. */}
            {permissions.includes("devices.read") ? (
              <Link className="top-enrol primary" to={ADD_COMPUTER_TO}>
                <Icon name="plus" size={14} />
                {/* Visually hidden rather than removed below 640px: the label
                    IS the accessible name (SC 2.5.3 wants the two to agree),
                    so `display: none` would leave a nameless icon exactly
                    where a small screen can least afford to guess. */}
                <span className="top-enrol-label">{t("shell.app.addComputer")}</span>
              </Link>
            ) : null}
            {permissions.includes("health.read") ? <HealthGlyph reading={reading} /> : null}
            {permissions.includes("platform.status.read") ? (
              <PlatformGlyph reading={reading} />
            ) : null}
            {/* ONE CHROME SHORTCUT, AND IT IS THE ASSISTANT.

                THE OPERATOR'S RULING, 2026-09-08: "AI Analyst needs to be
                there, documentation link doesn't need to be there". That was
                first read narrowly -- docs hidden below 640px, kept on the
                desktop -- because the finding it answered was a handset width
                measurement (seven 44px controls plus their gaps ran the bar to
                402px at 390px, a horizontal page scroll on every route). The
                operator corrected it on 2026-09-09 looking at the deployed
                bar: the link is gone at EVERY width. /docs remains a rail item
                and a command-palette destination, so nothing is unreachable.

                The health and platform glyphs stay, because a live reading is
                not a shortcut to anything and exists nowhere else in the
                chrome. */}
            {permissions.includes("ai.use") ? (
              <Link className="ib top-icon-btn" to="/assistant" aria-label={t("shell.app.askAssistant")}>
                <Icon name="sparkles" size={17} />
              </Link>
            ) : null}
            <AccountMenu user={user} onSignOut={onSignOut} />
          </div>
        </header>

        {/* SAY IT BEFORE THE USER FINDS OUT BY LOSING SOMETHING. The console
            learns that a silent renewal failed about a minute before the next
            request 401s; until this existed it kept that to itself, and the
            audit's finding is the consequence -- "the session drops mid-task
            during continuous active use, with no warning, no expiry notice,
            and total loss of context."

            A BANNER, NOT A DIALOG, and not an automatic redirect. Interrupting
            someone mid-form to announce that they are about to be interrupted
            is the same defect in politer clothing, and a redirect would throw
            away the unsaved work this warning exists to save. `role="status"`
            is polite by definition: assistive technology announces it at the
            next pause rather than cutting across what the user is doing. */}
        {sessionExpiring ? (
          <div className="session-notice" role="status">
            <span>{t("shell.app.sessionExpired")}</span>
            <button type="button" className="session-notice-action" onClick={onSignIn}>
              {t("shell.app.signInAgain")}
            </button>
          </div>
        ) : null}

        {/* The skip link's target, and `tabIndex={-1}` is what makes it work:
            without it the browser moves the viewport but not the focus, so a
            keyboard user lands visually at the content and keeps tabbing from
            inside the sidebar. */}
        <main className="content" id="main" tabIndex={-1}>
          {/* A BREADCRUMB THAT NAVIGATES, or does not look like one -- v3's
              `.crumb`, the first line of the content above the page's own
              `PageHead`. The audit's complaint was both halves at once: "ITMS /
              Fleet is not navigable" and the accent-coloured segment still
              looked clickable. Three text nodes in a div are a picture of a
              breadcrumb.

              `sectionTo` is optional and the section is a plain <span> without
              it, because a link that goes nowhere is the false affordance this
              finding is about. `aria-current="page"` marks the last crumb,
              which is what tells a screen reader it is where you are rather
              than somewhere you can go. The chevron is v3's separator and is
              decorative (`Icon` renders it `aria-hidden`). */}
          <nav className="crumb" aria-label={t("shell.app.breadcrumb")}>
            {section === undefined ? (
              <Link to="/" aria-current="page">
                ITMS
              </Link>
            ) : (
              <>
                <Link to="/">ITMS</Link>
                <Icon name="chevron-right" size={12} />
                {sectionTo ? (
                  <Link to={sectionTo} aria-current="page">
                    {section}
                  </Link>
                ) : (
                  <span aria-current="page">{section}</span>
                )}
              </>
            )}
          </nav>
          {children}
        </main>
      </div>
    </div>
  );
}
