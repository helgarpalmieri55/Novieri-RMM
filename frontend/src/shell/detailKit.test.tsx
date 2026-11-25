import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { renderPage } from "../test/renderPage";
import { DetailHead, SectionNav, UrlTabs, useUrlTab } from "./detailKit";
// Kit requests batch 3 through `components.tsx`, the kit's one door.
import { DetailGrid as DetailGridFromKit } from "./components";

const TABS = ["overview", "hardware", "software"] as const;
type Tab = (typeof TABS)[number];
const LABEL: Record<Tab, string> = { overview: "Overview", hardware: "Hardware", software: "Software" };

function Device() {
  const [tab, setTab] = useUrlTab<Tab>(TABS, "overview");
  return (
    <>
      <DetailHead
        back={{ to: "/devices", label: "All computers" }}
        title="srv-app02"
        status={{ kind: "ok", label: "Online" }}
        context={<span>Andes Logistics · Bogotá</span>}
        actions={<button type="button" className="btn">Connect</button>}
      />
      <UrlTabs label="Computer" items={TABS.map((key) => ({ key, label: LABEL[key] }))} active={tab} onSelect={setTab}>
        <p>{tab} panel</p>
      </UrlTabs>
    </>
  );
}

describe("DetailHead (design §2, Ficha)", () => {
  it("leads back to the list, names the record, its status and its context", () => {
    renderPage("/devices/dev-1", <Device />);
    expect(screen.getByRole("link", { name: "All computers" })).toHaveAttribute("href", "/devices");
    expect(screen.getByRole("heading", { level: 1, name: "srv-app02" })).toBeInTheDocument();
    expect(screen.getByText("Online")).toBeInTheDocument();
    expect(screen.getByText("Andes Logistics · Bogotá")).toBeInTheDocument();
  });
});

describe("UrlTabs and useUrlTab (design §2: tabs that change the URL)", () => {
  // `TabItem.note` through `UrlTabs` (P3 adoption pass): its items type took no
  // `note`, so Settings, the one caller, could pass one only by widening its
  // array. The note is drawn by `Tabs`; the panel keeps the tab's NAME.
  it("draws a tab's note, and names the panel after the tab's name alone", () => {
    function Noted() {
      const [tab, setTab] = useUrlTab<Tab>(TABS, "overview");
      return (
        <UrlTabs
          label="Computer"
          items={TABS.map((key) => ({ key, label: LABEL[key], ...(key === "hardware" ? { note: "Not collected" } : {}) }))}
          active={tab}
          onSelect={setTab}
        >
          <p>{tab} panel</p>
        </UrlTabs>
      );
    }
    renderPage("/devices/dev-1?tab=hardware", <Noted />);
    const hardware = screen.getByRole("tab", { name: "Hardware Not collected" });
    expect(hardware.querySelector(".tab-note")).toHaveTextContent("Not collected");
    expect(screen.getByRole("tabpanel", { name: "Hardware" })).toHaveTextContent("hardware panel");
  });

  it("opens the tab the URL names", () => {
    renderPage("/devices/dev-1?tab=hardware", <Device />);
    expect(screen.getByRole("tab", { name: "Hardware" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel", { name: "Hardware" })).toHaveTextContent("hardware panel");
    expect(screen.getByRole("tablist", { name: "Computer" })).toBeInTheDocument();
  });

  it("writes a tab into the URL as a new history entry, and the default tab writes nothing", async () => {
    const { router } = renderPage("/devices/dev-1", <Device />);
    await userEvent.click(screen.getByRole("tab", { name: "Software" }));
    expect(router.state.location.search).toBe("?tab=software");
    await act(() => router.navigate(-1));
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
    await userEvent.click(screen.getByRole("tab", { name: "Hardware" }));
    await userEvent.click(screen.getByRole("tab", { name: "Overview" }));
    expect(router.state.location.search).toBe("");
  });

  it("writes nothing when the tab is already open, so Back is not spent on a duplicate (review fix round 1, item 6)", async () => {
    const router = createMemoryRouter(
      [
        { path: "/start", element: <p>start</p> },
        { path: "/devices/:id", element: <Device /> },
      ],
      { initialEntries: ["/start", "/devices/dev-1?tab=hardware"], initialIndex: 1 },
    );
    render(<RouterProvider router={router} />);
    await userEvent.click(screen.getByRole("tab", { name: "Hardware" }));
    expect(router.state.location.search).toBe("?tab=hardware");
    await act(() => router.navigate(-1));
    expect(router.state.location.pathname).toBe("/start");
  });

  it("falls back to the default for a tab it does not know", () => {
    renderPage("/devices/dev-1?tab=nope", <Device />);
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
  });

  it("keeps the strip on one line, as one scrolling row (design §1.3)", () => {
    renderPage("/devices/dev-1", <Device />);
    expect(screen.getByRole("tablist")).toHaveClass("scroll");
  });
});

describe("SectionNav", () => {
  it("is a navigation of links that marks the current page, not a tablist", () => {
    renderPage(
      "/deploy/packages",
      <SectionNav
        label="Software"
        items={[
          { to: "/deploy", label: "Deployments", end: true },
          { to: "/deploy/packages", label: "Packages" },
        ]}
      />,
    );
    const nav = screen.getByRole("navigation", { name: "Software" });
    expect(nav).not.toHaveAttribute("role", "tablist");
    expect(screen.getByRole("link", { name: "Packages" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Deployments" })).not.toHaveAttribute("aria-current");
  });
});

/**
 * THE NARROW COLUMN FIRST (kit requests batch 3, B3-6; O6's K1): the docs
 * library on the left and the reader on the right, as the brief and Atera draw
 * it. The kit's grid put `main` (2fr) on the left, so the library would have
 * taken 2/3 and the reader 1/3.
 */
describe("DetailGrid asideFirst (B3-6)", () => {
  it("keeps main first by default", () => {
    const { container } = render(<DetailGridFromKit main={<p>reader</p>} aside={<p>library</p>} />);
    const grid = container.querySelector(".detail-grid");
    expect(grid).not.toHaveClass("aside-first");
    expect([...(grid?.children ?? [])].map((child) => child.className)).toEqual(["detail-main", "detail-aside"]);
  });

  it("draws the aside first, in the DOM too, so reading and Tab order follow what is drawn", () => {
    const { container } = render(<DetailGridFromKit main={<p>reader</p>} aside={<p>library</p>} asideFirst />);
    const grid = container.querySelector(".detail-grid");
    expect(grid).toHaveClass("aside-first");
    expect([...(grid?.children ?? [])].map((child) => child.className)).toEqual(["detail-aside", "detail-main"]);
  });

  it("gives the aside the narrow track and the main the wide one, and one column on a narrow screen (kit.css)", () => {
    const kit = readFileSync(resolve(import.meta.dirname, "./kit.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
    expect(kit).toMatch(/\.detail-grid\.aside-first\s*\{\s*grid-template-columns:\s*minmax\(0, 1fr\) minmax\(0, 2fr\);?\s*\}/);
    // `.detail-grid.aside-first` outranks the narrow rule's `.detail-grid`, so it needs one of its own.
    expect(kit).toMatch(
      /@media \(max-width: 1024px\) \{\s*\.detail-grid\.aside-first\s*\{\s*grid-template-columns:\s*minmax\(0, 1fr\);?\s*\}\s*\}/,
    );
  });

  it("is on /__preview, so the browser's a11y pass measures it", () => {
    const preview = readFileSync(resolve(import.meta.dirname, "../preview/PreviewPage.tsx"), "utf8");
    expect(preview).toMatch(/<DetailGrid\s[^>]*\basideFirst\b/);
  });

  /*
   * NOTHING OPEN BESIDE THE LIBRARY (P3 adoption pass, territory C's request
   * R1): /docs draws its library alone until a document is opened. With no
   * `main`, the main column is empty, takes no track and no gap, and the aside
   * spans the whole row; without this the closed state is a third-wide list
   * beside an empty column. No API change: `main={null}`.
   */
  it("leaves the main column empty with no main, and draws the aside alone across the row (kit.css)", () => {
    const { container } = render(<DetailGridFromKit main={null} aside={<p>library</p>} asideFirst />);
    const grid = container.querySelector(".detail-grid.aside-first");
    expect([...(grid?.children ?? [])].map((child) => child.className)).toEqual(["detail-aside", "detail-main"]);
    expect(grid?.querySelector(".detail-main")).toBeEmptyDOMElement();
    const kit = readFileSync(resolve(import.meta.dirname, "./kit.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
    expect(kit).toMatch(
      /\.detail-grid\.aside-first:has\(> \.detail-main:empty\)\s*\{\s*grid-template-columns:\s*minmax\(0, 1fr\);?\s*\}/,
    );
    expect(kit).toMatch(/\.detail-grid\.aside-first > \.detail-main:empty\s*\{\s*display:\s*none;?\s*\}/);
  });

  it("has the library-alone case on /__preview, so the browser measures it", () => {
    const preview = readFileSync(resolve(import.meta.dirname, "../preview/PreviewPage.tsx"), "utf8");
    expect(preview).toMatch(/<DetailGrid\s+asideFirst\s+main=\{null\}/);
  });
});
