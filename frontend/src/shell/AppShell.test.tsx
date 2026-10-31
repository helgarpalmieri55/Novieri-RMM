/**
 * The shell, rendered.
 *
 * `nav.test.ts` checks the navigation MODEL; this checks that the model
 * reaches the DOM — that an operator's permissions actually decide what the
 * sidebar shows, rather than the gating living in a pure function nobody
 * calls. Those are different failures, and only one of them is visible to a
 * user.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

const user = userEvent.setup();

import type { ComponentProps, ReactNode } from "react";

import { ApiError } from "../lib/api";
import type { AlertSummary } from "../modules/health/types";
import { AppShell } from "./AppShell";
import {
  healthSignal,
  platformLabel,
  platformSignal,
  useHealthReading,
  type HealthReading,
} from "./healthSignal";

/**
 * The shell's own API client, replaced. `AppShell` itself never calls one --
 * `useHealthReading` does, and it is the thing under test at the bottom of
 * this file. Mocked at the module boundary the health and platform-status
 * modules already mock (`vi.mock("../../../lib/ApiContext")`).
 */
const apiRef = vi.hoisted(() => ({ current: undefined as unknown }));
vi.mock("../lib/ApiContext", () => ({ useApi: () => apiRef.current }));

interface FakeApi {
  getHealthAlertSummary: ReturnType<typeof vi.fn>;
  listHealthAlerts: ReturnType<typeof vi.fn>;
  getPlatformStatus: ReturnType<typeof vi.fn>;
}

function summary(over: Partial<AlertSummary> = {}): AlertSummary {
  return {
    open: 2,
    by_state: { active: 1, acknowledged: 1, muted: 0 },
    by_severity: { info: 0, warning: 1, critical: 1 },
    worst_severity: "critical",
    ...over,
  };
}

/**
 * Two open alerts, on a platform whose rollup says `down`.
 *
 * `listHealthAlerts` IS STUBBED EVEN THOUGH NOTHING SHOULD CALL IT. That is the
 * point: the shell used to fetch a PAGE of the alert list and count it in the
 * browser, and the assertion that the page-and-filter is gone has to be able to
 * observe the call it must not make.
 */
function fakeApi(): FakeApi {
  return {
    getHealthAlertSummary: vi.fn().mockResolvedValue(summary()),
    listHealthAlerts: vi.fn().mockResolvedValue([]),
    getPlatformStatus: vi
      .fn()
      .mockResolvedValue({ overall: "down", probes: [], processes: [], modules: [] }),
  };
}

async function readingFor(permissions: string[], api: FakeApi): Promise<HealthReading> {
  apiRef.current = api;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { result } = renderHook(() => useHealthReading(permissions), {
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
  // Every enabled query has started by the time `renderHook` returns, so this
  // waits for them to land. A gated-off reading has none in flight and settles
  // immediately, which is exactly the assertion those cases make.
  await waitFor(() => expect(client.isFetching()).toBe(0));
  return result.current;
}

/** The sidebar only. Group labels also appear in the breadcrumb and page
 * title, so an unscoped query matches both and tells you nothing about which
 * one rendered. */
function sidebar() {
  return within(sidebar_el());
}

function sidebar_el(): HTMLElement {
  return screen.getByRole("navigation", { name: "Main" });
}

function renderShell(permissions: string[]) {
  return renderShellWith({ permissions });
}

function renderShellWith(
  overrides: {
    permissions?: string[];
    onSignOut?: () => void;
    sessionExpiring?: boolean;
    onSignIn?: () => void;
    searchItems?: ComponentProps<typeof AppShell>["searchItems"];
    health?: ComponentProps<typeof AppShell>["health"];
  } = {},
) {
  return render(
    <MemoryRouter>
      <AppShell
        permissions={overrides.permissions ?? ["devices.read"]}
        orgName="Operations"
        section="Fleet"
        title="Devices & Inventory"
        user={{ name: "Alex Morgan", email: "alex@toeshee.com", role: "Administrator" }}
        onSignOut={overrides.onSignOut ?? vi.fn()}
        sessionExpiring={overrides.sessionExpiring ?? false}
        onSignIn={overrides.onSignIn ?? vi.fn()}
        searchItems={overrides.searchItems}
        health={overrides.health}
      >
        <p>page content</p>
      </AppShell>
    </MemoryRouter>,
  );
}

describe("AppShell", () => {
  it("renders the page inside the chrome", () => {
    renderShell(["devices.read"]);
    expect(screen.getByText("page content")).toBeInTheDocument();
    // The brand block, not the breadcrumb's "ITMS" prefix.
    expect(screen.getByText("ITMS", { selector: ".brand-name" })).toBeInTheDocument();
    // v3 draws no page title in the bar (the page's own `PageHead` carries
    // it); what the chrome says about where you are is the crumb, now the
    // first line of the content.
    expect(screen.getByRole("navigation", { name: "Breadcrumb" })).toHaveTextContent("Fleet");
    expect(document.title).toBe("Devices & Inventory · ITMS");
  });

  it("draws the client selector first in the top bar's cluster (phase 3 design §1.1)", () => {
    render(
      <MemoryRouter>
        <AppShell
          permissions={["devices.read", "health.read"]}
          orgName="Operations"
          title="Devices"
          user={{ name: "A B", role: "Operator" }}
          onSignOut={vi.fn()}
          clientSelector={<span data-testid="client-selector" />}
        >
          <p>x</p>
        </AppShell>
      </MemoryRouter>,
    );
    expect(document.querySelector(".top-actions")?.firstElementChild).toBe(
      screen.getByTestId("client-selector"),
    );
  });

  it("shows an operator only the links their permissions reach", () => {
    renderShell(["devices.read", "patch.read"]);
    expect(sidebar().getByRole("link", { name: /Patching/ })).toBeInTheDocument();
    expect(sidebar().queryByRole("link", { name: /IT budget/ })).not.toBeInTheDocument();
    expect(sidebar().queryByRole("link", { name: /Backup/ })).not.toBeInTheDocument();
  });

  it("does not leave an empty area behind", () => {
    renderShell(["patch.read"]);
    expect(sidebar().getByText("Daily")).toBeInTheDocument();
    expect(sidebar().queryByText("Manage")).not.toBeInTheDocument();
    expect(sidebar().queryByText("Governance")).not.toBeInTheDocument();
  });

  it("is not vacuous: with the permissions, the hidden areas appear", () => {
    // NON-VACUITY. The two assertions above are about ABSENCE and would both
    // pass against a sidebar that rendered nothing at all.
    renderShell(["devices.read", "patch.read", "budget.read", "backup.read", "docs.read"]);
    expect(sidebar().getByRole("link", { name: /IT budget/ })).toHaveAttribute("href", "/budget");
    expect(sidebar().getByRole("link", { name: /IT documentation/ })).toBeInTheDocument();
    expect(sidebar().getByText("Governance")).toBeInTheDocument();
    expect(sidebar().getByText("Assets & Spend")).toBeInTheDocument();
  });

  it("shows the operator who they are signed in as", () => {
    renderShell(["devices.read"]);
    expect(screen.getByText("Alex Morgan")).toBeInTheDocument();
    // Initials, as the design's `.avatar` shows them.
    expect(screen.getByText("AM")).toBeInTheDocument();
  });

  it("badges a live count, and shows nothing at all for zero", () => {
    const { rerender } = render(
      <MemoryRouter>
        <AppShell
          permissions={["health.read"]}
          orgName="Operations"
          section="Fleet"
          title="Health & Alerts"
          badges={{ "/health": 12 }}
          user={{ name: "A B", role: "Operator" }}
          onSignOut={vi.fn()}
        >
          <p>x</p>
        </AppShell>
      </MemoryRouter>,
    );
    // Scoped to the rail: the top bar carries the same count for the viewport
    // where the rail is a shut drawer, so an unscoped query now matches twice.
    expect(within(sidebar_el()).getByText("12")).toBeInTheDocument();

    // Zero renders no badge: "nothing to deal with" and "a 0 in a pill" read
    // differently, and the design uses the badge to draw the eye.
    rerender(
      <MemoryRouter>
        <AppShell
          permissions={["health.read"]}
          orgName="Operations"
          section="Fleet"
          title="Health & Alerts"
          badges={{ "/health": 0 }}
          user={{ name: "A B", role: "Operator" }}
          onSignOut={vi.fn()}
        >
          <p>x</p>
        </AppShell>
      </MemoryRouter>,
    );
    expect(screen.queryByText("0")).not.toBeInTheDocument();
  });
});

/**
 * The account menu (UX remediation cycle 2).
 *
 * WHAT WAS THERE BEFORE. `<button className="profile-btn" onClick={onSignOut}>`
 * — the avatar chip in the top-right corner WAS the sign-out button. Two
 * independent auditors filed it as a BLOCKER on 2026-08-19, and the second one
 * put the reason precisely: a profile chip in that corner is a universally
 * understood MENU affordance. Gmail, GitHub, M365 and Jira all open a menu
 * there. People click it to check which account they are in, to reach their
 * settings, or to switch tenant — and in this product that click destroyed
 * their session and any unsaved work on the page, with no confirmation and no
 * undo. Combined with the sign-out redirect being broken, one stray click
 * ended an operator's session and stranded them on a Keycloak error page with
 * no link back.
 */
describe("AppShell account menu", () => {
  it("declares itself a menu button rather than looking like one", () => {
    renderShell(["devices.read"]);
    const chip = screen.getByRole("button", { name: /Alex Morgan/ });

    // The two attributes whose ABSENCE the auditors recorded by probing the
    // live DOM: `{"ae":null,"haspopup":null}`.
    expect(chip).toHaveAttribute("aria-haspopup", "menu");
    expect(chip).toHaveAttribute("aria-expanded", "false");
  });

  it("opens a menu on click instead of signing the user out", async () => {
    const onSignOut = vi.fn();
    renderShellWith({ onSignOut });

    await user.click(screen.getByRole("button", { name: /Alex Morgan/ }));

    expect(screen.getByRole("menu")).toBeInTheDocument();
    // THE ASSERTION THE WHOLE CYCLE IS FOR. Opening the menu must not be the
    // destructive act; there is no undo for a destroyed session.
    expect(onSignOut).not.toHaveBeenCalled();
  });

  it("offers the profile, settings and sign out", async () => {
    renderShell(["devices.read"]);
    await user.click(screen.getByRole("button", { name: /Alex Morgan/ }));
    const menu = within(screen.getByRole("menu"));

    expect(menu.getByRole("menuitem", { name: "Your profile" })).toHaveAttribute(
      "href",
      "/profile",
    );
    expect(menu.getByRole("menuitem", { name: "Settings" })).toHaveAttribute(
      "href",
      "/settings",
    );
    expect(menu.getByRole("menuitem", { name: "Sign out" })).toBeInTheDocument();
  });

  it("signs out only from the menu item, and marks it as the destructive one", async () => {
    const onSignOut = vi.fn();
    renderShellWith({ onSignOut });

    await user.click(screen.getByRole("button", { name: /Alex Morgan/ }));
    const item = screen.getByRole("menuitem", { name: "Sign out" });
    // Distinct from its non-destructive neighbours, which is the fix for
    // "styled identically to the actions beside it".
    expect(item).toHaveClass("menu-item-destructive");

    await user.click(item);
    expect(onSignOut).toHaveBeenCalledTimes(1);
  });

  it("closes on Escape and gives focus back to the chip", async () => {
    renderShell(["devices.read"]);
    const chip = screen.getByRole("button", { name: /Alex Morgan/ });

    await user.click(chip);
    await user.keyboard("{Escape}");

    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    // Returning focus is the half people forget: without it, Escape drops the
    // keyboard user at the top of the document.
    expect(chip).toHaveFocus();
    expect(chip).toHaveAttribute("aria-expanded", "false");
  });

  it("shows the signed-in identity, so the menu answers 'which account am I'", async () => {
    renderShell(["devices.read"]);
    await user.click(screen.getByRole("button", { name: /Alex Morgan/ }));

    expect(within(screen.getByRole("menu")).getByText("alex@toeshee.com")).toBeInTheDocument();
  });
});

/**
 * The tenant control (UX remediation cycle 2).
 *
 * Both auditors found it: a bordered, icon-prefixed, chrome-styled button
 * reading "Operations" that did NOTHING on click — before and after
 * screenshots pixel-identical, no menu role anywhere in the DOM — plus a
 * second control at the sidebar foot representing the same tenant in a
 * different visual language. `<button>` with no handler is the clearest
 * possible violation of "visibility of system status": the user cannot tell
 * whether the click registered, the app hung, or they lack permission.
 */
describe("AppShell tenant identity", () => {
  it("presents the tenant as identity, not as a control that does nothing", () => {
    renderShell(["devices.read"]);

    const tenant = screen.getByTestId("tenant-identity");
    expect(tenant).toHaveTextContent("Operations");
    expect(tenant.tagName).not.toBe("BUTTON");
    expect(within(tenant).queryByRole("button")).not.toBeInTheDocument();
  });

  it("names the tenant once, not twice in two visual languages", () => {
    renderShell(["devices.read"]);

    expect(screen.getAllByTestId("tenant-identity")).toHaveLength(1);
  });
});

/**
 * Skip link (UX remediation cycle 2). The auditors counted 36 tab stops
 * before the main content on every route — the entire sidebar, every time,
 * for anyone navigating by keyboard.
 */
describe("AppShell skip link", () => {
  it("offers a skip to the main content, pointing at something that exists", () => {
    renderShell(["devices.read"]);

    const skip = screen.getByRole("link", { name: /skip to (the )?main content/i });
    expect(skip).toHaveAttribute("href", "#main");
    // A skip link pointing at no target is worse than none: it moves focus
    // nowhere and reports success.
    expect(document.getElementById("main")).not.toBeNull();
  });
});

/**
 * The session-expiry banner (UX remediation cycle 2 remnant).
 *
 * Auditor A, HIGH: "The session drops mid-task during continuous active use,
 * with no warning, no expiry notice, and total loss of context." The console
 * knows a failed renewal has happened roughly a minute before the user finds
 * out by having a request fail. Saying so lets them finish the sentence they
 * are typing and save.
 */
describe("AppShell session notice", () => {
  it("says nothing while the session is healthy", () => {
    renderShell(["devices.read"]);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("warns when renewal has failed, without stealing focus or blocking the page", async () => {
    renderShellWith({ sessionExpiring: true });

    const notice = screen.getByRole("status");
    expect(notice).toHaveTextContent(/sign in again/i);
    // NOT a dialog. Interrupting someone mid-form to announce that they are
    // about to be interrupted is the same defect in politer clothing, and a
    // modal here would discard the unsaved work the warning exists to save.
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByText("page content")).toBeInTheDocument();
  });

  it("offers the way out, rather than only naming the problem", async () => {
    const onSignIn = vi.fn();
    renderShellWith({ sessionExpiring: true, onSignIn });

    await user.click(within(screen.getByRole("status")).getByRole("button", { name: /sign in/i }));

    expect(onSignIn).toHaveBeenCalledTimes(1);
  });
  /* --- The mobile drawer (UX cycle 10) ---------------------------------- */

  it("offers one 44px navigation control, not a 30x30 chevron", () => {
    // The audit measured the collapse chevron at 30x30 and found no other way
    // to reach navigation on a handset. The size itself is a stylesheet fact
    // jsdom cannot measure; what it CAN check is that the control exists,
    // announces its state, and points at the thing it opens.
    renderShell(["devices.read"]);
    const toggle = screen.getByRole("button", { name: "Open navigation" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveAttribute("aria-controls", "sidebar");
  });

  it("opens and closes the drawer, and says which it is", async () => {
    renderShell(["devices.read"]);
    await user.click(screen.getByRole("button", { name: "Open navigation" }));

    expect(document.body).toHaveClass("nav-open");
    const toggle = screen.getByRole("button", { name: "Close navigation" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");

    await user.click(toggle);
    expect(document.body).not.toHaveClass("nav-open");
  });

  it("closes on Escape and gives focus back to the control that opened it", async () => {
    renderShell(["devices.read"]);
    await user.click(screen.getByRole("button", { name: "Open navigation" }));

    await user.keyboard("{Escape}");

    expect(document.body).not.toHaveClass("nav-open");
    // Focus left on a dismissed scrim is focus on nothing: the next Tab
    // restarts from the top of the document.
    expect(screen.getByRole("button", { name: "Open navigation" })).toHaveFocus();
  });

  it("dismisses on the scrim, and the scrim is reachable without a mouse", async () => {
    renderShell(["devices.read"]);
    await user.click(screen.getByRole("button", { name: "Open navigation" }));

    // A <button>, not a div with an onClick: tapping outside to dismiss is a
    // real action and belongs in the tab order with a name of its own.
    const scrim = screen.getByRole("button", { name: "Dismiss navigation" });
    expect(scrim.tagName).toBe("BUTTON");
    expect(scrim).toHaveClass("nav-scrim");

    await user.click(scrim);
    expect(document.body).not.toHaveClass("nav-open");
  });

  it("renders no scrim while the drawer is shut, so a desktop click passes through", () => {
    // A permanently-mounted scrim with `pointer-events: none` would be one
    // stylesheet mistake away from swallowing every click in the console.
    renderShell(["devices.read"]);
    expect(document.querySelector(".nav-scrim")).toBeNull();
  });

  it("closes the drawer when a navigation actually happens", async () => {
    renderShell(["devices.read", "patch.read"]);
    await user.click(screen.getByRole("button", { name: "Open navigation" }));
    expect(document.body).toHaveClass("nav-open");

    await user.click(sidebar().getByRole("link", { name: /Patching/ }));

    expect(document.body).not.toHaveClass("nav-open");
  });

  it("remembers the collapsed rail between visits", async () => {
    // "its collapsed state is not persisted anywhere I could observe."
    window.localStorage.clear();
    const first = renderShell(["devices.read"]);
    await user.click(screen.getByRole("button", { name: "Collapse sidebar" }));
    expect(document.body).toHaveClass("sidebar-collapsed");
    first.unmount();

    renderShell(["devices.read"]);
    expect(document.body).toHaveClass("sidebar-collapsed");
    window.localStorage.clear();
  });

  it("starts expanded when nothing was stored", () => {
    window.localStorage.clear();
    renderShell(["devices.read"]);
    expect(document.body).not.toHaveClass("sidebar-collapsed");
  });

  it("still renders when localStorage throws", () => {
    // Safari private browsing and locked-down webviews throw on access. A
    // console that will not paint because it could not read a cosmetic
    // preference is worse than one that forgets the preference.
    const storage = Object.getOwnPropertyDescriptor(window, "localStorage");
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      get() {
        throw new DOMException("denied", "SecurityError");
      },
    });
    try {
      renderShell(["devices.read"]);
      expect(screen.getByText("page content")).toBeInTheDocument();
    } finally {
      if (storage) Object.defineProperty(window, "localStorage", storage);
    }
  });
  /* --- Breadcrumb and page title (cycle 12) ----------------------------- */

  it("names the page in the browser tab, not just 'ITMS'", async () => {
    // WCAG 2.4.2. The tab read the constant "ITMS" on all fourteen routes,
    // which is the one place a person with a dozen consoles open actually
    // reads a page's identity.
    const view = renderShell(["devices.read"]);
    expect(document.title).toBe("Devices & Inventory · ITMS");

    // Restored on unmount, so the login screen does not inherit it.
    view.unmount();
    expect(document.title).not.toContain("Devices & Inventory");
  });

  it("makes the breadcrumb a breadcrumb, not a picture of one", () => {
    renderShell(["devices.read"]);
    const crumbs = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(crumbs).getByRole("link", { name: "ITMS" })).toHaveAttribute("href", "/");
  });

  it("marks the current segment as current", () => {
    // What tells a screen reader this crumb is where you ARE rather than
    // somewhere you can go.
    renderShell(["devices.read"]);
    const crumbs = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(crumbs).getByText("Fleet")).toHaveAttribute("aria-current", "page");
  });

  it("does not dress the section as a link when it goes nowhere", () => {
    // A crumb that links nowhere is the false affordance the finding is about,
    // so the section is a plain element unless a destination was supplied.
    renderShell(["devices.read"]);
    const crumbs = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(crumbs).queryByRole("link", { name: "Fleet" })).toBeNull();
  });

  it("links the section when the caller supplies its destination", () => {
    render(
      <MemoryRouter>
        <AppShell
          permissions={["devices.read"]}
          orgName="Operations"
          section="Fleet"
          sectionTo="/devices"
          title="Devices & Inventory"
          user={{ name: "Alex Morgan", role: "Administrator" }}
          onSignOut={vi.fn()}
        >
          <p>page content</p>
        </AppShell>
      </MemoryRouter>,
    );
    const crumbs = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(crumbs).getByRole("link", { name: "Fleet" })).toHaveAttribute("href", "/devices");
  });
  /* --- Find or ask (top bar search) ------------------------------------- */

  it("offers one control that both finds and asks", () => {
    renderShell(["devices.read"]);
    // "Find or ask", not "Search". The second verb is the affordance; a
    // placeholder promising only search is why nobody would try the other.
    //
    // AND IT IS A FIELD, which is the operator's complaint answered: "the
    // search bar is a pop-up, it shouldn't be like that". It was a <button>
    // that opened a centred dialog; it is now the input itself, sitting in the
    // top bar with its results dropping below it.
    expect(screen.getByRole("combobox", { name: /Find or ask/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Find or ask/ })).toBeNull();
  });

  it("takes the character that was typed straight into the field", async () => {
    renderShell(["devices.read"]);
    const field = screen.getByRole("combobox", { name: /Find or ask/ });
    field.focus();

    await user.keyboard("w");

    // The keystroke is not swallowed. This used to need a seeding handover --
    // the button caught the character and passed it to an input that did not
    // exist yet -- and now it is just what an input does.
    expect(field).toHaveValue("w");
    expect(screen.getByRole("listbox", { name: "Search results" })).toBeInTheDocument();
  });

  it("opens on / from anywhere on the page, and puts the caret in the field", async () => {
    renderShell(["devices.read"]);
    await user.keyboard("/");
    const field = screen.getByRole("combobox", { name: /Find or ask/ });
    expect(field).toHaveFocus();
    expect(field).toHaveValue("");
    expect(screen.getByRole("listbox", { name: "Search results" })).toBeInTheDocument();
  });

  it("gives focus back to whatever / interrupted", async () => {
    // THE HALF OF FOCUS RETURN THE COMPONENT CANNOT OWN. Escape in the field
    // leaves focus in the field, which is right when the field is where the
    // operator went. It is wrong when a shortcut took them there from
    // somewhere else: they never chose to leave the control they were on, so
    // backing out has to put them back on it. The modal got this free by
    // remembering its trigger; an inline field has to be told.
    render(
      <MemoryRouter>
        <AppShell
          permissions={["devices.read"]}
          orgName="Operations"
          section="Fleet"
          title="Devices & Inventory"
          user={{ name: "Alex Morgan", role: "Administrator" }}
          onSignOut={vi.fn()}
        >
          <button type="button">Enroll device</button>
        </AppShell>
      </MemoryRouter>,
    );
    const wasOn = screen.getByRole("button", { name: "Enroll device" });
    wasOn.focus();

    await user.keyboard("/");
    expect(screen.getByRole("combobox", { name: /Find or ask/ })).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(wasOn).toHaveFocus();
  });

  it("leaves focus alone when the operator went to the field themselves", async () => {
    // THE NEGATIVE CONTROL for the test above: restoring unconditionally would
    // mean Escape yanked focus to whatever happened to hold it before the
    // operator clicked the search, which is nowhere they asked to be.
    renderShell(["devices.read"]);
    const field = screen.getByRole("combobox", { name: /Find or ask/ });
    await user.click(field);
    expect(screen.getByRole("listbox")).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(field).toHaveFocus();
  });

  it("leaves / alone while somebody is typing", async () => {
    // THE GUARD THAT MAKES THE SHORTCUT SAFE. Without it, a slash inside a
    // ticket note eats the character and opens a palette nobody asked for.
    render(
      <MemoryRouter>
        <AppShell
          permissions={["devices.read"]}
          orgName="Operations"
          section="Fleet"
          title="Devices & Inventory"
          user={{ name: "Alex Morgan", role: "Administrator" }}
          onSignOut={vi.fn()}
        >
          <textarea aria-label="Ticket note" />
        </AppShell>
      </MemoryRouter>,
    );
    const note = screen.getByLabelText("Ticket note");
    note.focus();

    await user.keyboard("half/hour");

    expect(note).toHaveValue("half/hour");
    // The results panel is what opening looks like now -- the search FIELD is
    // always on screen, so its presence can no longer be the tell.
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(screen.getByRole("combobox", { name: /Find or ask/ })).toHaveValue("");
  });

  it("leaves / alone while somebody is typing in the search itself", async () => {
    // THE SAME GUARD, ON THE CONTROL THAT DID NOT EXIST WHEN IT WAS WRITTEN.
    // The search is an INPUT now, so a slash typed into it is caught by the
    // same "somebody is typing" test -- and has to be, or searching for a path
    // would be impossible in the one field most likely to be handed one.
    renderShell(["devices.read"]);
    const field = screen.getByRole("combobox", { name: /Find or ask/ });
    field.focus();

    await user.keyboard("etc/hosts");

    expect(field).toHaveValue("etc/hosts");
  });

  it("does not open a panel hung off a field the layout has hidden", async () => {
    // BELOW 1024px THE WHOLE CONTROL LEAVES THE TOP BAR (`shell.css`), where
    // the rail is a drawer and there is no room for it. The panel is anchored
    // to that field, so opening it there would set state that renders nothing,
    // cannot take focus, therefore never blurs, therefore never closes -- and
    // the "already open" guard would then swallow every later `/`. jsdom has
    // no media queries, so the condition is reproduced the way the shortcut
    // reads it: by asking the element.
    renderShell(["devices.read"]);
    const field = screen.getByRole("combobox", { name: /Find or ask/ });
    Object.defineProperty(field, "checkVisibility", { value: () => false, configurable: true });

    await user.keyboard("/");
    expect(screen.queryByRole("listbox")).toBeNull();

    // NON-VACUITY: the same keypress against a visible field does open it, so
    // this is the guard firing and not the shortcut being broken outright.
    Object.defineProperty(field, "checkVisibility", { value: () => true, configurable: true });
    await user.keyboard("/");
    expect(screen.getByRole("listbox", { name: "Search results" })).toBeInTheDocument();
  });

  it("hands a question to the assistant when the operator asks for that", async () => {
    const onAsk = vi.fn();
    render(
      <MemoryRouter>
        <AppShell
          permissions={["devices.read", "ai.use"]}
          orgName="Operations"
          section="Fleet"
          title="Devices & Inventory"
          user={{ name: "Alex Morgan", role: "Administrator" }}
          onSignOut={vi.fn()}
          onAsk={onAsk}
        >
          <p>page content</p>
        </AppShell>
      </MemoryRouter>,
    );
    await user.type(screen.getByRole("combobox", { name: /Find or ask/ }), "why did patching stall");

    await user.click(screen.getByRole("button", { name: /Ask the Assistant/ }));
    expect(onAsk).toHaveBeenCalledWith("why did patching stall");
  });

  it("carries the assistant in the chrome and no documentation link at all", async () => {
    // THE OPERATOR'S RULING, twice. On 2026-09-08: "AI Analyst needs to be
    // there, documentation link doesn't need to be there". That was first read
    // as "hide docs on a handset", because the finding it answered was a
    // handset measurement -- seven 44px targets ran the bar to 402px at 390px.
    // On 2026-09-09, looking at the deployed bar, the operator meant what the
    // words say: the link is gone at every width.
    //
    // ASSERTED WITH `docs.read` GRANTED, which is the whole point: an operator
    // who MAY read documentation still gets no top-bar link. A test run
    // without the permission would pass on the gate rather than on the
    // removal, and would go green again the day somebody puts the link back.
    renderShell(["ai.use", "docs.read"]);
    expect(await screen.findByRole("link", { name: "Ask the Assistant" })).toBeInTheDocument();
    // THE TOP BAR, specifically: the rail lists IT documentation as an
    // ordinary page (Governance), which is where it belongs.
    const bar = document.querySelector("header.topbar") as HTMLElement;
    expect(bar).not.toBeNull();
    expect(within(bar).queryByRole("link", { name: "IT documentation" })).toBeNull();
  });

  it("never offers the assistant to an operator who cannot reach it", async () => {
    renderShell(["devices.read"]);
    await user.type(screen.getByRole("combobox", { name: /Find or ask/ }), "why did patching stall");
    expect(screen.queryByRole("button", { name: /Ask/ })).toBeNull();
  });
});

describe("the search field's data source", () => {
  it("finds a page the operator can reach, rather than answering that nothing matches", async () => {
    // THE DEFECT THIS PINS. `AppShell` has always accepted `searchItems` and
    // defaulted it to `[]`, and no caller ever passed it -- `grep -rn
    // searchItems src` returned only the three lines inside AppShell itself.
    // So the Find half of Find-or-Ask reached "No records match that" for
    // every query in the deployed console, however well spelled, while the
    // Ask half worked. A component waiting for a prop is not a feature.
    const user = userEvent.setup();
    const navigated: string[] = [];
    renderShellWith({
      searchItems: [
          {
            id: "/devices",
            label: "Devices & Inventory",
            sub: "Fleet",
            type: "Page",
            icon: "devices",
          onSelect: () => navigated.push("/devices"),
        },
      ],
    });

    await user.click(screen.getByRole("combobox", { name: /find or ask/i }));
    await user.keyboard("devic");

    const option = await screen.findByRole("option", { name: /Devices & Inventory/ });
    await user.click(option);
    expect(navigated).toEqual(["/devices"]);
  });

  it("offers nothing it cannot also reach, because the list is permission-filtered upstream", () => {
    // `App.tsx` builds these from `visibleGroups(permissions)`, the same model
    // the rail draws from, so search cannot surface a page the rail hid. This
    // asserts the shape that guarantee depends on: an empty list searches
    // nothing rather than falling back to a hardcoded catalogue.
    renderShellWith({ searchItems: [] });
    expect(screen.queryByRole("option")).toBeNull();
  });
});

/**
 * THE TOP BAR'S HEALTH GLYPH (this cycle).
 *
 * WHAT WAS THERE. `<Icon name="warning">` — the alert TRIANGLE, in one colour,
 * on every page of the console whatever the fleet was doing. It said "warning"
 * when nothing was wrong, and it said exactly the same thing when a dependency
 * was down, so an operator could learn nothing from it and stopped looking.
 * The rail already uses `icons.tsx`'s `health` glyph for /health; the top bar
 * used a different picture for the same destination.
 *
 * WHAT IT SAYS NOW, and the rule for each state, is in `healthSignal.ts`.
 * COLOUR IS NOT THE CARRIER — SC 1.4.1 is explicit that it may never be the
 * only one — so every state also names its condition in the accessible name
 * and draws a differently SHAPED pip. The three assertions below are the three
 * states an operator will actually see, plus the fourth that says "nobody has
 * measured this", which must never be allowed to read as the green one.
 */
describe("the top bar's health glyph", () => {
  const healthLink = () => screen.getByRole("link", { name: /^Health and alerts/ });

  it("uses the health glyph, not the alert triangle", () => {
    renderShellWith({ permissions: ["health.read"], health: { openAlerts: 0, platform: "ok" } });
    // The paths are Tabler's, verbatim from `icons.tsx`: `heartbeat` (a pulse
    // line over a heart) and `alert-triangle`, the triangle that used to
    // stand in for it.
    expect(healthLink().innerHTML).toContain("M3 13h2l2 3l2 -6l1 3h3");
    expect(healthLink().innerHTML).not.toContain("M10.363 3.591l-8.106 13.534");
  });

  it("is green, and says all is clear, when the fleet and the platform are both fine", () => {
    renderShellWith({ permissions: ["health.read"], health: { openAlerts: 0, platform: "ok" } });
    expect(healthLink()).toHaveClass("health-ok");
    expect(healthLink()).toHaveAccessibleName("Health and alerts, no open alerts");
  });

  it("is amber, and counts them, when health alerts are open", () => {
    renderShellWith({ permissions: ["health.read"], health: { openAlerts: 3, platform: "ok" } });
    expect(healthLink()).toHaveClass("health-alerts");
    expect(healthLink()).toHaveAccessibleName("Health and alerts, 3 needing attention");
  });

  it("is red, and names the reason, when a platform dependency is down", () => {
    renderShellWith({
      permissions: ["health.read"],
      health: { openAlerts: 2, platform: "down" },
    });
    expect(healthLink()).toHaveClass("health-down");
    // BOTH facts, not the worse one only: a dependency being down does not
    // make the two open alerts stop existing.
    expect(healthLink()).toHaveAccessibleName(
      "Health and alerts, a platform dependency is down, 2 needing attention",
    );
  });

  it("treats a degraded dependency as something to look at, not as fine", () => {
    renderShellWith({
      permissions: ["health.read"],
      health: { openAlerts: 0, platform: "degraded" },
    });
    expect(healthLink()).toHaveClass("health-alerts");
    expect(healthLink()).toHaveAccessibleName(
      "Health and alerts, a platform dependency is degraded, no open alerts",
    );
  });

  it("says nothing at all when nothing has been measured", () => {
    // THE STATE THAT MUST NOT BE GREEN. "0 open alerts" and "nobody asked" look
    // identical in a count and mean opposite things (DESIGN.md: state a zero's
    // provenance). An operator whose alert query 403'd or failed is owed a
    // glyph that claims nothing.
    renderShellWith({ permissions: ["health.read"] });
    expect(healthLink()).toHaveClass("health-unknown");
    expect(healthLink()).toHaveAccessibleName("Health and alerts");
  });

  it("states the count as a total, because it now is one", () => {
    // "AT LEAST 40" IS GONE, and the reason it existed is what changed. The
    // count came from one PAGE of the alert list filtered in the browser, so
    // a full page was a floor and saying "40" would have been a claim the
    // console could not make. `GET /health/alerts/summary` counts the whole
    // org in Postgres; the hedge would now be a false modesty about a number
    // that is exact.
    renderShellWith({
      permissions: ["health.read"],
      health: { openAlerts: 40, platform: "ok" },
    });
    expect(healthLink()).toHaveAccessibleName("Health and alerts, 40 needing attention");
    expect(healthLink().textContent).not.toContain("at least");
  });

  it("still carries the count where the rail's badge is hidden", () => {
    renderShellWith({
      permissions: ["health.read"],
      health: { openAlerts: 7, platform: "ok" },
    });
    expect(within(healthLink()).getByText("7")).toHaveClass("counter-dot");
  });

  it("offers nothing to an operator who cannot reach the destination", () => {
    renderShellWith({ permissions: ["devices.read"], health: { openAlerts: 3, platform: "down" } });
    expect(screen.queryByRole("link", { name: /^Health and alerts/ })).toBeNull();
  });
});

/**
 * THE TOP BAR'S PLATFORM-STATUS GLYPH, and the hole it fills.
 *
 * THE DEFECT. Red — a dependency the whole control plane stands on being DOWN,
 * the single most severe thing this console can report — comes from
 * `GET /platform/status`, gated `platform.status.read`. The glyph carrying it
 * was a `<Link to="/health">`, gated `health.read`, because that is what its
 * DESTINATION requires. The two codes are separate grants: an operator holding
 * platform-status and not health saw no glyph at all, and a dependency being
 * down was invisible to them on every page of the console.
 *
 * WHAT IS NOT THE FIX: widening the health glyph's gate to
 * `health.read || platform.status.read`. That points a control at `/health`,
 * which 403s for exactly the operator it was widened for — a false affordance,
 * which is the finding this chrome has already been through once.
 *
 * WHAT IS: platform status earns its own affordance with its own destination.
 * `/platform-status` is a shipped route, gated on the code the page's own
 * request requires (`shell/nav.ts`), so the control is a promise the console
 * keeps. `healthSignal.ts` is untouched as a rule; `platformSignal` /
 * `platformLabel` are added beside it, in the same one tested place, and speak
 * the same four-state vocabulary so both glyphs share one set of paint rules.
 */
describe("the top bar's platform-status glyph", () => {
  const platformLink = () => screen.getByRole("link", { name: /^Platform status/ });

  it("shows the red state to an operator who holds ONLY platform.status.read", () => {
    // THE WHOLE DEFECT, IN ONE ASSERTION. This operator could measure a
    // dependency being down and had nowhere on the console that said so.
    renderShellWith({
      permissions: ["platform.status.read"],
      health: { openAlerts: null, platform: "down" },
    });
    expect(platformLink()).toHaveClass("health-down");
    expect(platformLink()).toHaveAttribute("href", "/platform-status");
    expect(platformLink()).toHaveAccessibleName("Platform status, a dependency is down");
  });

  it("points at the page the permission it is gated on actually opens", () => {
    // NOT `/health`. Widening the health glyph's gate would have aimed this
    // operator's only control at a route they get a 403 from.
    renderShellWith({
      permissions: ["platform.status.read"],
      health: { openAlerts: null, platform: "ok" },
    });
    expect(platformLink()).toHaveAttribute("href", "/platform-status");
    expect(screen.queryByRole("link", { name: /^Health and alerts/ })).toBeNull();
  });

  it("is green only from a real answer, and grey from none", () => {
    // A 403 OR A FAILED REQUEST MUST NOT PAINT GREEN. `platform: null` is
    // "nobody measured this", and the grey state exists precisely so it cannot
    // render as an all-clear. SC 1.4.1: the condition is in the name too.
    renderShellWith({
      permissions: ["platform.status.read"],
      health: { openAlerts: null, platform: "ok" },
    });
    expect(platformLink()).toHaveClass("health-ok");
    expect(platformLink()).toHaveAccessibleName("Platform status, all dependencies healthy");

    cleanup();
    renderShellWith({ permissions: ["platform.status.read"] });
    expect(platformLink()).toHaveClass("health-unknown");
    expect(platformLink()).toHaveAccessibleName("Platform status");
  });

  it("treats a degraded dependency as amber rather than as fine", () => {
    renderShellWith({
      permissions: ["platform.status.read"],
      health: { openAlerts: null, platform: "degraded" },
    });
    expect(platformLink()).toHaveClass("health-alerts");
    expect(platformLink()).toHaveAccessibleName("Platform status, a dependency is degraded");
  });

  it("offers nothing to an operator who cannot reach the destination", () => {
    renderShellWith({
      permissions: ["health.read"],
      health: { openAlerts: 0, platform: "down" },
    });
    expect(screen.queryByRole("link", { name: /^Platform status/ })).toBeNull();
  });

  it("gives an operator holding both codes one control per destination", () => {
    // NOT REDUNDANT — the two answer different questions and land in different
    // places. The health glyph escalating to red on a downed dependency is
    // `healthSignal.ts`'s stated rule and is deliberately left alone: it says
    // "the fleet picture you are looking at is not trustworthy". This one says
    // where to go and read why.
    renderShellWith({
      permissions: ["health.read", "platform.status.read"],
      health: { openAlerts: 2, platform: "down" },
    });
    expect(screen.getByRole("link", { name: /^Health and alerts/ })).toHaveAttribute(
      "href",
      "/health",
    );
    expect(platformLink()).toHaveAttribute("href", "/platform-status");
  });
});

/**
 * THE RULE ITSELF, read without a shell around it.
 *
 * Four states, and the two that matter are the ends: `down` must never be
 * anything but red, and NOTHING MEASURED must never be green.
 */
describe("platformSignal", () => {
  it.each([
    ["down", "down"],
    ["degraded", "alerts"],
    ["ok", "ok"],
  ] as const)("maps %s to %s", (status, signal) => {
    expect(platformSignal(status)).toBe(signal);
  });

  it("maps a missing answer to unknown, never to ok", () => {
    expect(platformSignal(null)).toBe("unknown");
    expect(platformLabel(null)).toBe("Platform status");
    // And the healthy name is a DIFFERENT string, so the two are separable
    // with the colour taken away.
    expect(platformLabel("ok")).not.toBe(platformLabel(null));
  });
});

/**
 * THE STYLESHEET HALF of both of the above.
 *
 * A class name in JSX is not a component; the rule it selects is (DESIGN.md,
 * "The Cascade Is The Component" — `confirm.tsx` shipped `className="modal
 * open"` for a whole programme with no `.modal` rule behind it, and twelve
 * jsdom tests passed on it because jsdom has no cascade). These read the rules
 * themselves, so a state class with nothing behind it fails here.
 */
describe("the health glyph's rules", () => {
  const SHELL_CSS = readFileSync(resolve(import.meta.dirname, "shell.css"), "utf8").replace(
    /\/\*[\s\S]*?\*\//g,
    "",
  );

  function rule(selector: string): string {
    const at = SHELL_CSS.indexOf(`\n${selector} {`);
    expect(at, `no rule \`${selector}\` in shell.css`).toBeGreaterThanOrEqual(0);
    return SHELL_CSS.slice(at, SHELL_CSS.indexOf("}", at));
  }

  // v3's severity tokens on v3's `.ib` icon button (the JSX carries both `ib`
  // and the older `top-icon-btn` name; the rules are written against `.ib`).
  it.each([
    [".health-ok", "--sev-ok"],
    [".health-alerts", "--sev-warn"],
    [".health-down", "--sev-crit"],
  ])("paints %s from %s, so the colour is the design's and not a literal", (selector, token) => {
    expect(rule(`.ib${selector}`)).toContain(`var(${token})`);
  });

  it("gives each state a different SHAPE, so colour is never the only carrier", () => {
    // SC 1.4.1. Roughly one man in twelve cannot separate the green from the
    // amber reliably, and a red/green pair is the single worst choice for it —
    // which is what a status glyph carried by hue alone would be.
    const pip = (state: string) => rule(`.ib${state} .health-pip`);
    const silhouettes = [".health-ok", ".health-alerts", ".health-down"].map(
      (state) => /(clip-path|border-radius):\s*([^;]+);/.exec(pip(state))?.[2]?.trim() ?? "",
    );
    expect(silhouettes.every((mark) => mark !== "")).toBe(true);
    expect(
      new Set(silhouettes).size,
      `two of the three verdicts share a silhouette: ${silhouettes.join(" | ")}`,
    ).toBe(3);

    // And "nobody measured this" is drawn differently again — hollow, so that
    // with the colour taken away it cannot be read as the filled green one.
    // The hollow is the button's own surface, named by its token.
    expect(pip(".health-unknown")).toMatch(/background:\s*var\(--surface\)/);
  });
});

/**
 * "WHERE'S THE INSTALL AGENT BUTTON?" — the operator, again, and this time
 * about the top bar rather than the Devices page.
 *
 * Getting an agent onto a machine is the FIRST thing anyone does with this
 * product and the thing they do most often afterwards, and until now it was
 * reachable only from the seventh group of the rail (behind a label naming the
 * artefact, "Enrollment & Certificates") or from one button on one page. The
 * top bar is the one surface on every route.
 *
 * ONE "ADD A COMPUTER" ENTRY (review 2026-09-23). It leads to the same page as
 * the Devices head, whose first panel is the one-machine install -- the
 * installers and the command, never a bare enrollment token ("it redirects me
 * to enroll a token which again is not the agent installation", the operator)
 * -- with the network sweep below it.
 */
describe("the top bar's enrol affordance", () => {
  const enrol = () => screen.getByRole("link", { name: /add a computer/i });

  it("puts getting an agent onto a machine on every page", () => {
    renderShellWith({ permissions: ["devices.read"] });
    expect(enrol()).toHaveAttribute("href", "/devices/onboard");
  });

  it("carries a visible label, because an unlabelled icon is what made it unfindable", () => {
    renderShellWith({ permissions: ["devices.read"] });
    expect(within(enrol()).getByText("Add a computer")).toBeInTheDocument();
  });

  it("is gated on the technician permission, not shown to someone with neither", () => {
    // The control keeps its `devices.read` gate -- the technician who enrols
    // and manages endpoints -- rather than switching to the destination's
    // `agent.release.read`: the page itself refuses an under-privileged caller
    // server-side (invariant 6), so the button need not re-gate on it, and a
    // reader with neither permission is offered no dead affordance.
    renderShellWith({ permissions: ["health.read"] });
    expect(screen.queryByRole("link", { name: /add a computer/i })).toBeNull();
  });

  it("is drawn in the primary voice, as v3 draws it", () => {
    // RULED 2026-09-08, closing the v3 port. This assertion used to read
    // `not.toHaveClass("primary")`, from DESIGN.md's one-primary rule read as
    // "one per screen". The rule is one per page BODY, and this control is
    // chrome: it sits on the top bar's rung with the search field and the
    // account menu, never on the page's own, so it does not compete with a
    // page's primary for the first glance. v3's `topbar()` draws it
    // `.btn.pri`, DESIGN.md now records the exception, and the console
    // paints the accent. The guard is kept and pointed at what is now true,
    // not deleted.
    renderShellWith({ permissions: ["devices.read"] });
    expect(enrol()).toHaveClass("primary");
  });

  it("does not push the chrome off the side of a phone", () => {
    // Below 1024px the crumb's own text is the thing that can set the
    // content's floor: `min-width: 0` shrinks the BOX and leaves the text
    // painting outside it, into the document's scrollable overflow. That is
    // SC 1.4.10 Reflow, and it is a browser fact rather than a jsdom one --
    // what can be asserted here is that the rule which clips it exists at
    // all. (It was the top bar's page title; v3 moved the crumb into the
    // content and dropped the bar's title, so the rule moved with it.)
    const css = readFileSync(resolve(import.meta.dirname, "shell.css"), "utf8");
    const narrow = css.slice(css.indexOf("@media (max-width: 1024px)"));
    const crumb = narrow.slice(narrow.indexOf(".crumb > *"));
    expect(crumb.slice(0, crumb.indexOf("}"))).toContain("text-overflow: ellipsis");
  });

  it("keeps its name where the layout drops the label", () => {
    // Narrow viewports have no room for the word, and `display: none` on the
    // label would take the accessible name with it — leaving a nameless icon
    // exactly where the smallest screens can least afford to guess.
    const css = readFileSync(resolve(import.meta.dirname, "shell.css"), "utf8");
    const narrow = css.slice(css.indexOf(".top-enrol-label"));
    expect(narrow).not.toMatch(/\.top-enrol-label[^}]*display:\s*none/);
  });
});

/**
 * WHERE THE THREE COLOURS COME FROM — the half that was missing entirely.
 *
 * `badges` was a dead prop: `AppShell` accepted it, `Sidebar` read it, and
 * `grep -rn 'badges=' src` found no caller outside this file and the preview
 * page. So the rail's /health badge and the top bar's count had never rendered
 * on a deployed console, and any tri-state built on them would have been
 * permanently stuck on whatever "zero" painted.
 *
 * These exercise the reading itself: that it asks the real routes, that it
 * asks NEITHER of them without the permission that route requires, and that a
 * refusal reads as "unknown" rather than as "fine".
 */
describe("useHealthReading", () => {
  it("takes the count from the server's summary and does not fetch the alert list", async () => {
    // THE DEFECT THIS REPLACES. `GET /health/alerts` is a PAGE -- 200 rows by
    // the route's own default -- and its `state` filter is single-valued, so
    // "open" (active OR acknowledged OR muted) could not be asked for at all.
    // The shell fetched a page and filtered it in the browser, which on a
    // fleet with more than a page of open alerts under-reports silently. Both
    // halves are asserted: the summary IS called, and the list is NOT -- a
    // reading that quietly kept the old fetch alongside the new one would
    // still be paying for a page on every route of every session.
    const api = fakeApi();
    const reading = await readingFor(["health.read", "platform.status.read"], api);

    expect(api.getHealthAlertSummary).toHaveBeenCalledTimes(1);
    expect(api.listHealthAlerts).not.toHaveBeenCalled();
    expect(api.getPlatformStatus).toHaveBeenCalledTimes(1);
    expect(reading).toEqual({ openAlerts: 2, platform: "down" });
  });

  it("reports the server's total verbatim, however large", async () => {
    // NOT RE-FILTERED, NOT RE-COUNTED, NOT CAPPED. The browser no longer holds
    // an opinion about which alerts are open -- `open` is the server's answer
    // over the whole org, and a number bigger than any page it could have
    // fetched is exactly the case the old code could not produce.
    const api = fakeApi();
    api.getHealthAlertSummary.mockResolvedValue(summary({ open: 4_312 }));
    const reading = await readingFor(["health.read"], api);

    expect(reading.openAlerts).toBe(4_312);
  });

  it("keeps a measured zero apart from an unmeasured one", async () => {
    // `?? null` ON A ZERO IS THE BUG THIS GUARDS. A count of 0 is a MEASUREMENT
    // and must reach the glyph as 0, so it paints green; a refusal must reach
    // it as null, so it paints grey. Written wrong (`data?.open || null`) the
    // two collapse into the same value and a healthy fleet reports "nobody
    // measured this".
    const api = fakeApi();
    api.getHealthAlertSummary.mockResolvedValue(
      summary({
        open: 0,
        by_state: { active: 0, acknowledged: 0, muted: 0 },
        by_severity: { info: 0, warning: 0, critical: 0 },
        worst_severity: null,
      }),
    );
    const reading = await readingFor(["health.read"], api);

    expect(reading.openAlerts).toBe(0);
    expect(healthSignal(reading)).toBe("ok");
  });

  it("never fires a request the operator would only get a 403 from", async () => {
    // The point of the gate. Chrome renders on EVERY route, so an ungated call
    // is not one 403 -- it is one per page load, forever, for every technician
    // in the tenant, filling the audit log to draw an icon they never see.
    const api = fakeApi();
    const reading = await readingFor(["devices.read"], api);

    expect(api.getHealthAlertSummary).not.toHaveBeenCalled();
    expect(api.getPlatformStatus).not.toHaveBeenCalled();
    expect(reading.openAlerts).toBeNull();
    expect(reading.platform).toBeNull();
  });

  it("asks only the route the operator can reach, when they hold one code", async () => {
    const api = fakeApi();
    const reading = await readingFor(["health.read"], api);

    expect(api.getHealthAlertSummary).toHaveBeenCalledTimes(1);
    expect(api.getPlatformStatus).not.toHaveBeenCalled();
    // No platform reading is not a healthy platform. It is no reading.
    expect(reading.platform).toBeNull();
    expect(reading.openAlerts).toBe(2);
  });

  it("reports a refused or failed count as unmeasured, never as zero", async () => {
    const api = fakeApi();
    api.getHealthAlertSummary.mockRejectedValue(new ApiError(403, ""));
    const reading = await readingFor(["health.read"], api);

    expect(reading.openAlerts).toBeNull();
    expect(healthSignal(reading)).toBe("unknown");
  });
});
