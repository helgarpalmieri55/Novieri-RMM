import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { appRoutes } from "./App";
import { RouteError } from "./shell/RouteError";

describe("the console's router (decision D7)", () => {
  it("is a data router over the same routes, under the setup gate", () => {
    const routes = appRoutes();
    expect(routes).toHaveLength(1);
    const paths = (routes[0]?.children ?? []).map((route) => route.path);
    expect(paths).toContain("/devices");
    expect(paths).toContain("/devices/:deviceId");
    expect(paths).toContain("/clients/:clientId");
    expect(paths).toContain("*");
  });

  it("puts a translated error page on the layout route (decision D18)", () => {
    expect(appRoutes()[0]?.errorElement).toBeDefined();
  });

  it("says what failed in the console's words when a page throws, never React Router's page", () => {
    function Broken(): never {
      throw new Error("boom");
    }
    const router = createMemoryRouter([{ path: "/", element: <Broken />, errorElement: <RouteError /> }]);
    // React reports the caught error through console.error; expected here.
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      render(<RouterProvider router={router} />);
      expect(screen.getByRole("alert")).toHaveTextContent("This page could not be shown");
      expect(screen.getByRole("link", { name: "Go to the dashboard" })).toHaveAttribute("href", "/dashboard");
      expect(screen.queryByText(/Unexpected Application Error/)).toBeNull();
    } finally {
      spy.mockRestore();
    }
  });
});
