import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { renderPage } from "../test/renderPage";
import { DetailHead, SectionNav, UrlTabs, useUrlTab } from "./detailKit";

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
