/**
 * A page under a DATA router (design §2, "Formulario": `UnsavedGuard` needs
 * one to block a navigation), with a query client that does not retry, so a
 * test can read `router.state.location` after a click. The API is injected the
 * way the rest of the suite does it: `vi.mock` of `lib/ApiContext`.
 *
 * `options.page` also gives the page the shell's half of the tab title
 * (`TitleShell`): `useRecordTitle` does nothing outside `AppShell`, so a record
 * page's "names the record in the browser tab" test needs it (pre-flight W-I3).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderResult } from "@testing-library/react";
import { useEffect, useState, type ReactElement, type ReactNode } from "react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";

import { useT } from "../i18n/context";
import type { MessageKey } from "../i18n/core";
import { documentTitle, RecordTitleProvider } from "../shell/title";

/**
 * What `AppShell` does for the tab title, and nothing else: the page half is
 * the rail entry's label (a catalogue key, so it follows a language switch the
 * way the shell's does), and a record page puts its name in front.
 */
export function TitleShell({ page, children }: { page: MessageKey; children: ReactNode }) {
  const t = useT();
  const [record, setRecord] = useState<string | undefined>();
  const title = t(page);
  useEffect(() => {
    document.title = documentTitle(title, record);
  }, [title, record]);
  return <RecordTitleProvider value={setRecord}>{children}</RecordTitleProvider>;
}

export function renderPage(
  path: string,
  element: ReactElement,
  pattern = "*",
  options: { page?: MessageKey } = {},
): RenderResult & { router: ReturnType<typeof createMemoryRouter>; queryClient: QueryClient } {
  const shown = options.page ? <TitleShell page={options.page}>{element}</TitleShell> : element;
  const routes =
    pattern === "*" ? [{ path: "*", element: shown }] : [{ path: pattern, element: shown }, { path: "*", element: <p>elsewhere</p> }];
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const view = render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { ...view, router, queryClient };
}
