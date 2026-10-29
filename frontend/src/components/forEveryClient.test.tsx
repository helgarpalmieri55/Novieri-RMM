/**
 * The "For every client" switch (MSP clients plan 2026-10-26 Task 14): shown
 * only to a person who holds the create route's code on EVERY client -- what
 * `/me`'s permissions list -- because only they may create a global row; for
 * anyone else it is absent, not offered and then refused by the server.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../i18n/core";
import type { ApiClient, Me } from "../lib/api";
import { ForEveryClientToggle } from "./forEveryClient";

const mockUseApi = vi.fn();
vi.mock("../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  permissions: ["docs.read", "docs.manage"],
};

function withMe(me: Me, ui: ReactElement) {
  mockUseApi.mockReturnValue({ me: vi.fn().mockResolvedValue(me) } as unknown as ApiClient);
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      {ui}
    </QueryClientProvider>,
  );
}

afterEach(() => setActiveLanguage("en"));

describe("ForEveryClientToggle", () => {
  it("is offered to a holder of the code on every client, and reports its state", async () => {
    const onChange = vi.fn();
    withMe(ME, <ForEveryClientToggle code="docs.manage" checked={false} onChange={onChange} />);
    const box = await screen.findByRole("checkbox", { name: /For every client/ });
    expect(box).not.toBeChecked();
    expect(screen.getByText(/belongs to your organisation's own client/)).toBeInTheDocument();
    await userEvent.click(box);
    expect(onChange).toHaveBeenCalledWith(true);
  });

  it("is absent for anyone else", async () => {
    const me = vi.fn().mockResolvedValue({ ...ME, permissions: ["docs.read"] });
    mockUseApi.mockReturnValue({ me } as unknown as ApiClient);
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <ForEveryClientToggle code="docs.manage" checked={false} onChange={() => undefined} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(me).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("is absent while /me has not answered", () => {
    mockUseApi.mockReturnValue({
      me: vi.fn().mockReturnValue(new Promise(() => undefined)),
    } as unknown as ApiClient);
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ForEveryClientToggle code="docs.manage" checked={false} onChange={() => undefined} />
      </QueryClientProvider>,
    );
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("speaks Spanish", async () => {
    setActiveLanguage("es");
    withMe(ME, <ForEveryClientToggle code="docs.manage" checked onChange={() => undefined} />);
    expect(await screen.findByRole("checkbox", { name: /Para todos los clientes/ })).toBeChecked();
    expect(screen.getByText(/es del cliente propio de tu organización/)).toBeInTheDocument();
  });
});
