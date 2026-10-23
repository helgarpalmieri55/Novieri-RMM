/**
 * A COLUMN HEADER THAT NAMES NOTHING, closed at the component instead of at
 * the call site.
 *
 * WHAT WAS BROKEN. The browser pass reported twelve findings and all twelve
 * were one defect: `<Th>{""}</Th>` on a trailing actions column. A sighted
 * reader sees a blank header and infers "the buttons go here". A screen-reader
 * user navigating a row hears the column header before each cell, and for that
 * column heard nothing at all -- no name, on the column that holds Delete.
 *
 * WHY THE FIX IS NOT AT THE THREE CALL SITES THE HARNESS RENDERS. A sweep of
 * the module tree found THIRTY-ONE of these, in nineteen files, across
 * fourteen modules. Three were visible to the browser pass only because those
 * two panels happen to be in `/__preview`; the other twenty-eight were exactly
 * as broken and entirely unseen. Fixing the three the harness renders would
 * have closed the finding and left the defect, and the next table written the
 * same way would have shipped the same way.
 *
 * So `Th` itself refuses to render a nameless header. That is the guard: not a
 * rule someone has to remember and a reviewer has to catch, but a component
 * that cannot express the bad state. `label` is there for the column that is
 * not an actions column, and the tests below hold both halves.
 *
 * THE SECOND HALF, and it is the one that can rot. A component can only guard
 * the markup that goes through it. A raw `<th></th>` bypasses `Th` entirely
 * and no amount of care inside `ui.tsx` sees it, so the source scan at the
 * bottom asserts that no such tag exists. That is the same lesson
 * `jsx-tokens.test.ts` records: a floor that only holds in the files you
 * remembered to check is not a floor.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { resolve } from "node:path";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Table, Td, Th } from "../components/ui";

const REPO = resolve(import.meta.dirname, "../../..");
const SRC = resolve(REPO, "frontend/src");
const rel = (path: string) => path.slice(REPO.length + 1);

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = resolve(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    return /\.tsx$/.test(name) ? [path] : [];
  });
}

/** The accessible name a screen reader would announce for a header cell. */
const headerNames = () =>
  screen.getAllByRole("columnheader").map((th) => (th.textContent ?? "").trim());

describe("Th always names its column", () => {
  it.each([
    ["an empty string", ""],
    ["a whitespace-only string", " "],
    ["null", null],
    ["undefined", undefined],
    ["a false branch", false],
  ] as const)("falls back to a screen-reader name for %s", (_what, children) => {
    render(
      <Table>
        <thead>
          <tr>
            <Th>Device</Th>
            <Th>{children}</Th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <Td>host-1</Td>
            <Td>x</Td>
          </tr>
        </tbody>
      </Table>,
    );
    expect(headerNames()).toEqual(["Device", "Actions"]);
  });

  it("hides that fallback from sighted readers rather than printing it", () => {
    render(
      <Table>
        <thead>
          <tr>
            <Th>{""}</Th>
          </tr>
        </thead>
      </Table>,
    );
    // `sr-only` is clipped out of the visual render but stays in the
    // accessibility tree -- `base.css` reinforces it unlayered precisely so a
    // `table`-scoped element rule cannot win the cascade against it.
    const named = screen.getByRole("columnheader").firstElementChild;
    expect(named).toHaveClass("sr-only");
    expect(named).toHaveTextContent("Actions");
  });

  it("takes an explicit label for a column that is not the actions column", () => {
    render(
      <Table>
        <thead>
          <tr>
            <Th label="Selected">{""}</Th>
          </tr>
        </thead>
      </Table>,
    );
    expect(headerNames()).toEqual(["Selected"]);
  });

  it("leaves a header that already has text exactly as it was", () => {
    render(
      <Table>
        <thead>
          <tr>
            <Th>Worst severity</Th>
            <Th>{0}</Th>
          </tr>
        </thead>
      </Table>,
    );
    // `0` is a real datum, not an absent one, and must not be replaced.
    expect(headerNames()).toEqual(["Worst severity", "0"]);
    const [first] = screen.getAllByRole("columnheader");
    expect(first?.querySelector(".sr-only")).toBeNull();
  });
});

/**
 * THE COLUMN MEASURE, AND THE THING IT IS NOT ALLOWED TO COST.
 *
 * WHAT WAS BROKEN. An auto-layout table gives a column no floor of its own, so
 * the column that identifies the row -- the one thing that must stay legible
 * when the rest of the table is off the side of a handset -- is the one that
 * gives way. The browser pass measured it across the console at 390x844:
 * `/changes/approvals` rendered `CR-118 Upgrade core switch firmware` as six
 * stacked one-word lines in a ~65px column, in a row 200px tall;
 * `/service-desk` did the same to a ticket subject in ~95px. At 1440
 * `/devices` did the mirror image of it -- a ~230px Device column breaking
 * every FQDN over three lines while Operating system and Architecture held
 * six characters each in columns twice the width.
 *
 * `Th`'s `width` is a floor for a column that must keep WRAPPING (an error
 * paragraph, a reason someone typed). `Td`'s `clip` is the whole measure for a
 * column that must not wrap at all: `white-space: nowrap` makes the cell's
 * min-content width equal its max-content width and `max-width` clamps both,
 * so the column settles at the stated measure instead of at whatever is left
 * over.
 *
 * THE COST IT MAY NOT HAVE. An ellipsis that eats the value is not a fix, it
 * is a different defect -- so the clipped cell carries the full text on its
 * `title`, derived from the cell's own content rather than restated by hand at
 * every call site. That derivation is the part that can rot silently: these
 * cells hold `<Link>{subject}</Link>` inside a `<div className="cell-title">`,
 * so a `textOf` that stopped at the first element would hand every one of them
 * an empty title and nothing would fail.
 */
describe("a column can state its measure", () => {
  it("puts Th's width on the header as a floor, never as a cap", () => {
    render(
      <Table>
        <thead>
          <tr>
            <Th width={280}>Reason</Th>
            <Th>Granted</Th>
          </tr>
        </thead>
      </Table>,
    );
    const [reason, granted] = screen.getAllByRole("columnheader");
    // A FLOOR. The column this exists for (`/health`'s exemption reason,
    // `/provisioning`'s error) is a paragraph that must keep wrapping -- a
    // `max-width` here would cap the sentence rather than give it room.
    expect(reason).toHaveStyle({ minWidth: "280px" });
    expect(reason?.style.maxWidth).toBe("");
    expect(granted?.style.minWidth).toBe("");
  });

  it("accepts a length as well as a number of pixels", () => {
    render(
      <Table>
        <thead>
          <tr>
            <Th width="14rem">Evidence</Th>
          </tr>
        </thead>
      </Table>,
    );
    expect(screen.getByRole("columnheader")).toHaveStyle({ minWidth: "14rem" });
  });

  it("still names a blank header that carries a measure", () => {
    // The two features are independent and the sr-only branch is a SECOND
    // return: a measure passed to the actions column must not lose the name.
    render(
      <Table>
        <thead>
          <tr>
            <Th width={90}>{""}</Th>
          </tr>
        </thead>
      </Table>,
    );
    const header = screen.getByRole("columnheader");
    expect(headerNames()).toEqual(["Actions"]);
    expect(header).toHaveStyle({ minWidth: "90px" });
    // `position: relative` keeps the absolutely positioned name inside the
    // scroller; adding a measure must not displace it.
    expect(header).toHaveStyle({ position: "relative" });
  });

  it("caps a clipped cell and marks it for the stylesheet", () => {
    render(
      <Table>
        <tbody>
          <tr>
            <Td clip={260}>CR-118 Upgrade core switch firmware</Td>
            <Td>Elevated</Td>
          </tr>
        </tbody>
      </Table>,
    );
    const [name, risk] = screen.getAllByRole("cell");
    expect(name).toHaveClass("cell-clip");
    expect(name).toHaveStyle({ maxWidth: "260px" });
    expect(risk).not.toHaveClass("cell-clip");
    expect(risk?.style.maxWidth).toBe("");
  });

  it("clips within the column it already has when given no measure", () => {
    render(
      <Table>
        <tbody>
          <tr>
            <Td clip>lis-ws-041.corp.toeshee.example</Td>
          </tr>
        </tbody>
      </Table>,
    );
    const cell = screen.getByRole("cell");
    expect(cell).toHaveClass("cell-clip");
    expect(cell.style.maxWidth).toBe("");
  });

  it("keeps the whole value on the cell, so the ellipsis costs a hover and not the datum", () => {
    render(
      <Table>
        <tbody>
          <tr>
            <Td clip={280}>INC-4124 Docking station not detecting second screen</Td>
          </tr>
        </tbody>
      </Table>,
    );
    expect(screen.getByRole("cell")).toHaveAttribute(
      "title",
      "INC-4124 Docking station not detecting second screen",
    );
  });

  it("reads that value out of the elements the real cells are made of", () => {
    // EVERY page this shipped for looks like this: a title div wrapping a
    // link, a sub-line under it. A derivation that stopped at the element
    // boundary would produce "" here and lose the value silently, and one
    // that concatenated the two blocks with nothing between them would read
    // `firmwareStandard` -- a word in neither of them.
    //
    // The `lead` glyph is deliberately NOT in the title: it is an icon, it
    // names the row's kind and not its value, and `Icon` renders no text
    // anyway.
    render(
      <Table>
        <tbody>
          <tr>
            <Td clip={280} lead={<span>icon</span>}>
              <div className="cell-title">
                <a href="/changes/chg-1">Upgrade core switch firmware</a>
              </div>
              <div className="cell-sub">Standard · window 14 Mar</div>
            </Td>
          </tr>
        </tbody>
      </Table>,
    );
    expect(screen.getByRole("cell")).toHaveAttribute(
      "title",
      "Upgrade core switch firmware Standard · window 14 Mar",
    );
  });

  it("takes an explicit title where the visible text is not the whole story", () => {
    render(
      <Table>
        <tbody>
          <tr>
            <Td clip={200} title="10.4.7.9 in Lisbon office">
              <span className="mono">10.4.7.9</span>
            </Td>
          </tr>
        </tbody>
      </Table>,
    );
    expect(screen.getByRole("cell")).toHaveAttribute("title", "10.4.7.9 in Lisbon office");
  });

  it("gives an unclipped cell no title, because a tooltip repeating the cell is noise", () => {
    render(
      <Table>
        <tbody>
          <tr>
            <Td>Restart Spooler</Td>
          </tr>
        </tbody>
      </Table>,
    );
    expect(screen.getByRole("cell")).not.toHaveAttribute("title");
  });

  it("leaves an empty clipped cell without an empty title attribute", () => {
    // `title=""` is not "no title": it is an announced empty description.
    render(
      <Table>
        <tbody>
          <tr>
            <Td clip={120}>{null}</Td>
          </tr>
        </tbody>
      </Table>,
    );
    expect(screen.getByRole("cell")).not.toHaveAttribute("title");
  });
});

/**
 * THE FLOOR THAT MADE A DESKTOP SIDE CARD SCROLL.
 *
 * `.table-wrap > table { min-width: 560px }` exists so a five-column list on a
 * handset scrolls rather than crushing its columns (SC 1.4.10). It says "no
 * table is ever narrower than 560px", which is not the same claim as "a table
 * must not be squeezed below the width its columns need" -- and the two part
 * company inside a half-width card. `/portal/tickets/tkt-1`'s Status history
 * is three short columns in a ~500px side card at a 1440px viewport, and the
 * flat floor pushed it to 560 and drew `SCROLL FOR MORE COLUMNS` under a table
 * that had nothing to scroll to. `/ipam/registry`'s Sites card was the same.
 *
 * `fit-content(560px)` is `min(max-content, max(min-content, 560px))`: it
 * floors at 560 only for a table that actually wants more than 560, and lets a
 * genuinely small table be small. The plain `560px` stays as the first
 * declaration so an engine without `fit-content()` keeps today's behaviour
 * rather than none.
 */
describe("the table's width floor", () => {
  const components = readFileSync(
    resolve(import.meta.dirname, "../shell/components.css"),
    "utf8",
  );
  const rule = components
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .split("\n")
    .reduce<string>((found, line, i, lines) => {
      if (found) return found;
      if (!line.trim().startsWith(".table-wrap > table {")) return found;
      const end = lines.findIndex((l, j) => j >= i && l.trimEnd().endsWith("}"));
      return lines.slice(i, end + 1).join(" ");
    }, "");

  it("floors a table at its own content when that is less than 560px", () => {
    expect(rule, "no `.table-wrap > table` rule in components.css").not.toBe("");
    expect(rule).toContain("min-width: fit-content(560px)");
  });

  it("keeps the flat floor first, as the fallback for an engine without fit-content()", () => {
    expect(rule).toMatch(/min-width: 560px;[\s\S]*min-width: fit-content\(560px\)/);
  });
});

/**
 * The clipping declarations live with the measure, not on `.cell-title`.
 *
 * v3 truncates its row title (`.lrow .ttl b` is
 * `white-space:nowrap;overflow:hidden;text-overflow:ellipsis`) and the
 * console's transcription onto `.cell-title` kept the weight and the ink and
 * dropped the clipping -- which is the whole of I1. Restoring it on
 * `.cell-title` itself would have been wrong in the other direction: `nowrap`
 * with no `max-width` does not truncate, it makes the column as wide as the
 * longest string in it. So the clipping is scoped to the cells that have been
 * given a measure.
 */
describe("a clipped cell truncates the way the design does", () => {
  const components = readFileSync(
    resolve(import.meta.dirname, "../shell/components.css"),
    "utf8",
  );
  const declarations = (selector: string) => {
    const lines = components.replace(/\/\*[\s\S]*?\*\//g, "").split("\n");
    const start = lines.findIndex((line) => line.trim().startsWith(`${selector} {`));
    if (start < 0) return "";
    const end = lines.findIndex((line, i) => i >= start && line.trimEnd().endsWith("}"));
    return lines.slice(start, end + 1).join(" ");
  };

  it("gives the cell the three declarations that make an ellipsis", () => {
    const cell = declarations("td.cell-clip");
    expect(cell, "no `td.cell-clip` rule in components.css").not.toBe("");
    expect(cell).toContain("white-space: nowrap");
    expect(cell).toContain("overflow: hidden");
    expect(cell).toContain("text-overflow: ellipsis");
  });

  it("clips the title and the sub-line separately, as v3 clips `.ttl b` and `.ttl small`", () => {
    // A single `nowrap` on the cell is not enough for these: they are block
    // children, and each has to clip on its own line or the sub-line drags the
    // column open.
    const parts = declarations("td.cell-clip .cell-title, td.cell-clip .cell-sub");
    expect(parts, "the row title and sub-line are not clipped inside a measured cell").not.toBe("");
    expect(parts).toContain("text-overflow: ellipsis");
    expect(parts).toContain("white-space: nowrap");
  });

  it("leaves `.cell-title` outside a measured cell wrapping", () => {
    // `.cell-title` is in 101 files, most of them not in a clipped column and
    // some not in a table at all. Clipping it globally would have been a
    // one-line fix and a wide regression.
    const title = declarations(".cell-title");
    expect(title).not.toContain("white-space: nowrap");
  });
});

describe("nothing bypasses that guard", () => {
  const FILES = sources(SRC).filter((path) => !/\.test\./.test(path));

  /**
   * THE BACKLOG IS EMPTY, AND THIS LIST IS WHAT KEPT IT HONEST WHILE IT WAS NOT.
   *
   * The sweep that produced this test found sixteen raw `<th>{""}</th>`.
   * Thirteen were fixed with it; three sat in files a concurrent session held
   * open, and were listed here by path rather than hidden, so the count could
   * not drift silently and the next reader knew the backlog was three and not
   * "some". All three are now fixed and the list is empty -- which is the only
   * state it is allowed to reach, because the assertion below fails on any
   * entry that no longer carries the defect it is excused for.
   *
   * `CandidatesPage` is why `Th` takes a `label`. Its blank header was the
   * LEADING column, before "Address", holding a per-row selection checkbox --
   * so `Th`'s "Actions" default would have announced the wrong thing, which is
   * as much a defect as announcing nothing. It is named "Select".
   */
  const KNOWN_UNFIXED: string[] = [];

  /**
   * A raw `<th>` with nothing in it. `<th key={x} scope="col">{x}</th>` is
   * fine -- what this forbids is the empty one, which `Th` can no longer
   * produce and a hand-written tag still could.
   */
  it("no raw <th> tag renders an empty header", () => {
    const empty = /<th(\s[^>]*?)?>(\s*|\{\s*(""|''|null|undefined|`\s*`)\s*\}|&nbsp;)<\/th>/g;
    const offenders: string[] = [];
    for (const path of FILES) {
      if (KNOWN_UNFIXED.includes(rel(path))) continue;
      const source = readFileSync(path, "utf8");
      for (let m = empty.exec(source); m; m = empty.exec(source)) {
        offenders.push(`${rel(path)}:${source.slice(0, m.index).split("\n").length}  ${m[0]}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  /**
   * The exemption list is itself asserted, so it shrinks and never grows: a
   * path that has been fixed must leave this list, and the test says so by
   * failing when the file no longer carries the defect it is excused for.
   */
  it("excuses nothing that has been fixed", () => {
    // `it.each([])` registers no test and passes vacuously, so the empty case
    // is asserted here instead -- otherwise emptying the list would silently
    // retire the guard that keeps it shrinking.
    for (const path of KNOWN_UNFIXED) {
      expect(readFileSync(resolve(REPO, path), "utf8")).toMatch(/<th>\{""\}<\/th>/);
    }
    expect(KNOWN_UNFIXED.length, "the backlog is empty; entries may only be removed").toBe(0);
  });
});
