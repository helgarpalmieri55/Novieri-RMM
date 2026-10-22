/**
 * The v3 pieces, held to the three rules the port spec sets for them: colour
 * is never the only carrier, unmeasured is absent rather than zero, and the
 * severity vocabulary is one closed set.
 */

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { StatCard, StatusPill } from "./components";
import {
  Avatar,
  Chips,
  ChipsField,
  Donut,
  Facts,
  Heat,
  KIND_ICON,
  KV,
  OsIcon,
  Person,
  Pill,
  Sparkline,
  Steps,
  Tile,
  Timeline,
  Toolbar,
  kindOf,
} from "./viz";

const user = userEvent.setup();

describe("kindOf", () => {
  it.each([
    ["failed", "crit"],
    ["critical", "crit"],
    ["revoked", "crit"],
    ["expired", "crit"],
    ["warning", "warn"],
    ["expiring", "warn"],
    ["due", "warn"],
    ["healthy", "ok"],
    ["completed", "ok"],
    ["running", "run"],
    ["renewing", "run"],
    ["scheduled", "run"],
    ["info", "info"],
    ["neutral", "mut"],
  ] as const)("classifies %s as %s", (value, kind) => {
    expect(kindOf(value)).toBe(kind);
  });

  it("classifies the wire's spellings, not only the slug", () => {
    expect(kindOf("In_Progress")).toBe("run");
    expect(kindOf("cert_revoked")).toBe("crit");
    expect(kindOf("NOT APPLICABLE")).toBe("mut");
  });

  it("gives an unknown word the muted kind rather than a colour it has not earned", () => {
    expect(kindOf("zorblax")).toBe("mut");
    expect(kindOf("")).toBe("mut");
  });
});

describe("Pill and StatusPill", () => {
  it("carries the kind's glyph, so colour is never the only carrier", () => {
    const { container } = render(<StatusPill value="failed" />);
    const pill = container.querySelector(".pill");
    expect(pill).toHaveClass("p-crit");
    expect(pill?.querySelector("svg")).not.toBeNull();
    expect(pill).toHaveTextContent("Failed");
  });

  it("names every kind's glyph from the one table", () => {
    expect(KIND_ICON).toEqual({
      crit: "circle-x",
      warn: "alert-triangle",
      ok: "circle-check",
      run: "progress",
      mut: "minus",
      info: "info-circle",
    });
  });

  it("lets a caller override the kind and the label", () => {
    const { container } = render(<StatusPill value="open" kind="warn" label="Breach in 40m" />);
    expect(container.querySelector(".pill")).toHaveClass("p-warn");
    expect(screen.getByText("Breach in 40m")).toBeInTheDocument();
  });

  it("renders a bare Pill with its kind", () => {
    const { container } = render(<Pill kind="ok">Done</Pill>);
    expect(container.querySelector(".pill.p-ok")).toHaveTextContent("Done");
  });
});

describe("Tile", () => {
  it("renders the label, the figure, the unit and the sub-line", () => {
    render(<Tile label="Agent coverage" value="95" unit="%" sub="272 of 286" />);
    expect(screen.getByText("Agent coverage")).toHaveClass("k");
    expect(screen.getByText("95")).toHaveClass("v");
    expect(screen.getByText("%").tagName).toBe("SMALL");
    expect(screen.getByText("272 of 286")).toHaveClass("z");
  });

  it("is a link when it has somewhere to go, and a plain card otherwise", () => {
    const { container, rerender } = render(
      <MemoryRouter>
        <Tile label="Open alerts" value="12" to="/health" />
      </MemoryRouter>,
    );
    expect(screen.getByRole("link", { name: /Open alerts/ })).toHaveAttribute("href", "/health");
    rerender(
      <MemoryRouter>
        <Tile label="Open alerts" value="12" />
      </MemoryRouter>,
    );
    expect(screen.queryByRole("link")).toBeNull();
    expect(container.querySelector(".tile")).not.toBeNull();
  });

  it("wears the kind and makes room for a sparkline", () => {
    const { container } = render(<Tile label="Needs a person" value="7" kind="crit" spark={[4, 5, 4, 6, 9]} />);
    const tile = container.querySelector(".tile");
    expect(tile).toHaveClass("crit", "has-sp");
    expect(tile?.querySelector("svg.sp")).not.toBeNull();
  });
});

describe("Sparkline", () => {
  it("draws an area, a line and the last point in currentColor", () => {
    const { container } = render(<Sparkline values={[4, 5, 4, 6, 9]} />);
    const svg = container.querySelector("svg.sp");
    expect(svg).toHaveAttribute("viewBox", "0 0 104 26");
    expect(svg?.querySelector("path.a")).not.toBeNull();
    expect(svg?.querySelector("path.l")).not.toBeNull();
    expect(svg?.querySelector("circle.p")).not.toBeNull();
    expect(svg).toHaveAttribute("aria-label", "Trend over 5 points, from 4 to 9");
  });

  it("draws nothing for fewer than two points, which is not a trend", () => {
    const { container } = render(<Sparkline values={[4]} />);
    expect(container.querySelector("svg")).toBeNull();
  });
});

describe("Donut", () => {
  it("draws the arc for a measured value", () => {
    const { container } = render(<Donut pct={94} kind="ok" label="Windows" sub="168 of 179" />);
    expect(container.querySelector(".don")).toHaveClass("ok");
    expect(container.querySelector("circle.arc")).not.toBeNull();
    expect(container.querySelector(".don-v")).toHaveTextContent("94%");
    expect(screen.getByText("168 of 179")).toBeInTheDocument();
  });

  it("draws no arc and says n/a for an unmeasured one, never a zero", () => {
    const { container } = render(<Donut pct={null} kind="ok" label="macOS" sub="never scanned" />);
    expect(container.querySelector("circle.arc")).toBeNull();
    expect(container.querySelector(".don-v")).toHaveTextContent("n/a");
    expect(container.querySelector(".don-v")).not.toHaveTextContent("0");
    expect(container.querySelector(".don")).toHaveClass("mut");
  });
});

describe("Heat", () => {
  it("names the counts, so 286 squares are not the only carrier", () => {
    const cells = [...Array<"crit">(7).fill("crit"), ...Array<"warn">(18).fill("warn"), ...Array<"ok">(5).fill("ok")];
    const { container } = render(<Heat cells={cells} label="devices" />);
    expect(screen.getByRole("img", { name: "30 devices: 7 critical, 18 warning, 5 ok" })).toBeInTheDocument();
    expect(container.querySelectorAll(".heat i")).toHaveLength(30);
    expect(container.querySelectorAll(".heat .h-crit")).toHaveLength(7);
  });
});

describe("Timeline", () => {
  it("leads each run with its glyph and draws a bar only where there is a span", () => {
    const { container } = render(
      <Timeline
        runs={[
          { at: "patch.apply", kind: "crit", label: "sentinel-db-02", start: 6, width: 17 },
          { at: "backup.run", kind: "ok", label: "fs-archive-01" },
        ]}
        ticks={["04:00", "05:00", "06:00"]}
      />,
    );
    expect(container.querySelectorAll(".tl-row")).toHaveLength(2);
    expect(container.querySelectorAll(".tl-k svg")).toHaveLength(2);
    expect(container.querySelectorAll(".tl-bar")).toHaveLength(1);
    expect(container.querySelector(".tl-bar.b-crit")).toHaveStyle({ left: "6%", width: "17%" });
    expect(container.querySelectorAll(".tl-axis i")).toHaveLength(3);
  });
});

describe("Facts, KV, Steps", () => {
  it("renders facts as a definition list", () => {
    render(<Facts items={[{ k: "Agent", v: "1.8.2" }, { k: "Site server", v: "rack-b4-relay" }]} />);
    expect(screen.getByText("Agent").tagName).toBe("DT");
    expect(screen.getByText("1.8.2").tagName).toBe("DD");
  });

  it("renders key-values as a definition list", () => {
    render(<KV items={[["Requester", "Adaeze Okonkwo"], ["Priority", "P2"]]} />);
    expect(screen.getByText("Priority").tagName).toBe("DT");
    expect(screen.getByText("P2").tagName).toBe("DD");
  });

  it("carries a step's state as a bar, a pill and words", () => {
    const { container } = render(
      <Steps
        items={[
          { label: "Register the network", state: "done" },
          { label: "Authorize a sweep", state: "now" },
          { label: "Promote a site server", state: "todo" },
        ]}
      />,
    );
    const steps = container.querySelectorAll(".step");
    expect(steps[0]).toHaveClass("ok");
    expect(steps[1]).toHaveClass("now");
    expect(steps[1]).toHaveAttribute("aria-current", "step");
    expect(steps[0]?.querySelector(".pill")).toHaveTextContent("Done");
    expect(steps[2]?.querySelector(".pill")).toHaveTextContent("To do");
    expect(screen.getByText("02")).toHaveClass("six");
  });
});

describe("Avatar, Person, OsIcon", () => {
  it("picks one of six tones by index and keeps the initials off the accessibility tree", () => {
    const { container } = render(<Avatar initials="AO" index={7} />);
    const avatar = container.querySelector(".av") as HTMLElement;
    expect(avatar.style.background).toContain("--av-2");
    expect(avatar).toHaveAttribute("aria-hidden", "true");
  });

  it("names the person beside the avatar", () => {
    render(<Person initials="MR" index={2} name="Mateus Ribeiro" />);
    expect(screen.getByText("Mateus Ribeiro")).toBeInTheDocument();
  });

  it("names the platform for assistive technology and falls back to a desktop", () => {
    render(<OsIcon os="windows" />);
    expect(screen.getByRole("img", { name: "Windows" })).toBeInTheDocument();
    render(<OsIcon os="plan9" />);
    expect(screen.getByRole("img", { name: "plan9" })).toBeInTheDocument();
  });
});

describe("Chips and Toolbar", () => {
  it("is a group of pressed buttons, one of them on", async () => {
    const onSelect = vi.fn();
    render(<Chips items={["All", "Windows", "Linux"]} active={0} onSelect={onSelect} />);
    expect(screen.getByRole("button", { name: "All" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Linux" })).toHaveAttribute("aria-pressed", "false");
    await user.click(screen.getByRole("button", { name: "Linux" }));
    expect(onSelect).toHaveBeenCalledWith(2);
  });

  it("puts the count on the right", () => {
    const { container } = render(<Toolbar left={<span>filters</span>} right={<span>7 of 286</span>} />);
    expect(container.querySelector(".tb-l")).toHaveTextContent("filters");
    expect(container.querySelector(".tb-r")).toHaveTextContent("7 of 286");
  });

  /**
   * `ChipsField`'s whole reason to exist: three settings call sites hand-
   * rolled a whole parallel `<select disabled>` beside the live chip group,
   * because `Chips` had nothing to render for "inert". A `disabled` group
   * has to be actually non-interactive, not merely styled grey -- so this
   * is a keyboard test, not a markup read: a real `disabled` button drops
   * out of the tab order AND refuses a click event a test can still fire
   * directly at it (jsdom does not enforce that a disabled element ignores
   * `.click()`, so the assertion is on the callback, not on the DOM alone).
   */
  it("a disabled group is not focusable and not clickable", async () => {
    const onSelect = vi.fn();
    render(
      <Chips items={["All", "Windows", "Linux"]} active={0} onSelect={onSelect} label="Platform" disabled />,
    );
    const group = screen.getByRole("group", { name: "Platform" });
    for (const button of within(group).getAllByRole("button")) {
      expect(button).toBeDisabled();
    }

    // Tab order: a disabled button is never a stop, so focus moves straight
    // past the whole group to whatever document.body itself hands it next
    // -- there is nothing else on this page, so it simply never leaves
    // <body>.
    await user.tab();
    expect(document.body).toHaveFocus();

    // A click most test setups fire without going through real focus first
    // -- `disabled` still has to refuse it.
    await user.click(within(group).getByRole("button", { name: "Windows" }));
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("stays live and clickable when `disabled` is left at its default", async () => {
    const onSelect = vi.fn();
    render(<Chips items={["All", "Windows", "Linux"]} active={0} onSelect={onSelect} label="Platform" />);
    const group = screen.getByRole("group", { name: "Platform" });
    for (const button of within(group).getAllByRole("button")) {
      expect(button).not.toBeDisabled();
    }
    await user.click(within(group).getByRole("button", { name: "Windows" }));
    expect(onSelect).toHaveBeenCalledWith(1);
  });
});

/**
 * `ChipsField` closes the three near-identical call sites in Settings
 * (`EmailTab`'s Encryption, `BudgetTab`'s "Fiscal year starts in",
 * `OrganizationTab`'s per-key choice fields): a caption, a `Chips` group
 * named by that caption through `aria-labelledby` rather than also through
 * `label` -- the double-announcement bug all three shared -- and an
 * optional help line under the group for the raw key `OrganizationTab`
 * prints there.
 */
describe("ChipsField", () => {
  it("names the group with its own visible caption, once — not twice", () => {
    const { container } = render(
      <ChipsField caption="Encryption" items={["StartTLS", "SSL/TLS", "None"]} active={0} onSelect={() => {}} />,
    );
    const group = screen.getByRole("group", { name: "Encryption" });
    // `aria-labelledby`, not a second `aria-label` repeating the same text —
    // the visible `<span>` is the ONLY place "Encryption" is written, so if
    // the group carried its own `aria-label` too, the DOM would hold the
    // word twice for exactly one caption.
    expect(group).not.toHaveAttribute("aria-label");
    expect(group).toHaveAttribute("aria-labelledby");
    expect(container.textContent?.match(/Encryption/g)).toHaveLength(1);
    const captionId = group.getAttribute("aria-labelledby");
    expect(document.getElementById(captionId ?? "")).toHaveTextContent("Encryption");
  });

  it("passes `disabled` through to the chip group", () => {
    render(
      <ChipsField
        caption="Fiscal year starts in"
        items={["January", "February"]}
        active={0}
        onSelect={() => {}}
        disabled
      />,
    );
    const group = screen.getByRole("group", { name: "Fiscal year starts in" });
    for (const button of within(group).getAllByRole("button")) {
      expect(button).toBeDisabled();
    }
  });

  it("prints the optional help line, for the raw key `OrganizationTab` shows under a choice field", () => {
    render(
      <ChipsField
        caption="Date format"
        items={["Year-month-day", "Day/month/year"]}
        active={0}
        onSelect={() => {}}
        help="date_format"
      />,
    );
    expect(screen.getByText("date_format")).toHaveClass("help", "mono");
  });

  it("omits the help line when none is given, rather than an empty span", () => {
    const { container } = render(
      <ChipsField caption="Encryption" items={["StartTLS", "SSL/TLS"]} active={0} onSelect={() => {}} />,
    );
    expect(container.querySelector(".help")).toBeNull();
  });

  /**
   * INDEX-BASED, LIKE `Chips` ITSELF. `ChipsField` is a thin wrapper, so the
   * value <-> index mapping stays the caller's job (`ENCRYPTION_CHOICES.
   * findIndex`, `MONTHS[at]`, and so on) -- what this pins is only that
   * `ChipsField` relays `active` and `onSelect`'s index faithfully, with no
   * off-by-one of its own, the failure mode an extraction like this could
   * introduce silently.
   */
  it("relays the active index and the selected index without shifting either", async () => {
    const onSelect = vi.fn();
    render(
      <ChipsField
        caption="Fiscal year starts in"
        items={["January", "February", "March"]}
        active={1}
        onSelect={onSelect}
      />,
    );
    const group = screen.getByRole("group", { name: "Fiscal year starts in" });
    expect(within(group).getByRole("button", { name: "February" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await user.click(within(group).getByRole("button", { name: "March" }));
    expect(onSelect).toHaveBeenCalledWith(2);
  });

  it("shows no chip pressed when the active index is out of range — a stored value the list no longer has", () => {
    render(
      <ChipsField caption="Date format" items={["Year-month-day", "Day/month/year"]} active={-1} onSelect={() => {}} />,
    );
    const group = screen.getByRole("group", { name: "Date format" });
    for (const button of within(group).getAllByRole("button")) {
      expect(button).toHaveAttribute("aria-pressed", "false");
    }
  });
});

describe("StatCard renders a Tile", () => {
  const card = (props: Partial<Parameters<typeof StatCard>[0]> = {}) =>
    render(
      <MemoryRouter>
        <StatCard label="At risk" value="18" icon="warning" to="/devices?state=at-risk" {...props} />
      </MemoryRouter>,
    );

  it("is a v3 tile and a link, and keeps the hooks its module tests query", () => {
    const { container } = card();
    const tile = screen.getByRole("link", { name: /At risk/ });
    expect(tile).toHaveClass("tile", "stat-card");
    expect(container.querySelector(".stat-card .stat-value")).toHaveTextContent("18");
    expect(container.querySelector(".stat-card .stat-label")).toHaveTextContent("At risk");
  });

  it("says `n/a` while the figure is unmeasured, rather than a confident zero", () => {
    // A tile above a `QueryState` renders while its list is still loading, so
    // `String(items.length)` is 0 -- a measurement nobody made, on every stat
    // row in the console. The spec is explicit that unmeasured renders `n/a`;
    // the label, the tone and the destination are unchanged, because the tile
    // is still about the same thing.
    const { container } = card({ value: "0", pending: true, tone: "red", urgent: true, zeroLabel: "Nothing breached" });
    expect(container.querySelector(".stat-value")).toHaveTextContent("n/a");
    expect(container.querySelector(".stat-label")).toHaveTextContent("At risk");
    // Not urgent and not "nothing breached": both are claims about a reading.
    expect(container.querySelector(".tile")).not.toHaveClass("urgent");
    expect(container.querySelector(".stat-zero")).toBeNull();
  });

  it("wears the crit wash only when urgent and red, and never for a measured zero", () => {
    const { container, rerender } = card({ tone: "red", urgent: true });
    expect(container.querySelector(".tile")).toHaveClass("crit", "urgent");
    rerender(
      <MemoryRouter>
        <StatCard label="Breached" value="0" icon="clock" to="/sla" tone="red" urgent zeroLabel="Nothing breached" />
      </MemoryRouter>,
    );
    expect(container.querySelector(".tile")).not.toHaveClass("crit");
    expect(container.querySelector(".tile")).not.toHaveClass("urgent");
    expect(screen.getByText("Nothing breached")).toBeInTheDocument();
  });

  it("carries the trend's direction as a glyph and a word, not a colour", () => {
    const { container } = card({ tone: "red", urgent: true, trend: { direction: "up", label: "+4 since yesterday" } });
    const trend = container.querySelector(".stat-trend") as HTMLElement;
    expect(trend.querySelector("svg")).not.toBeNull();
    expect(trend).toHaveTextContent("Up, +4 since yesterday");
  });
});
