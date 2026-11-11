/**
 * The notice a tab shows once its session has ended (operator decision 3,
 * 2026-11-05, "Ocultar y vaciar").
 *
 * RE-POINTED from `AppShell.test.tsx`'s "AppShell session notice" -- "warns
 * when renewal has failed, without stealing focus or blocking the page" and
 * "offers the way out, rather than only naming the problem" -- and from
 * `shell.es.test.tsx`'s banner assertions. The banner sat over a page left
 * drawn; the notice now stands in place of the shell and the page, and what
 * the person typed is kept, read-only, so the auditor's "total loss of
 * context" still does not happen. `App.auth.test.tsx` holds the guard to
 * showing it (and to the cache it empties); this file holds what it says.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoEnglish, renderInSpanish } from "../i18n/test-utils";
import { SessionEnded } from "./SessionEnded";

const KEPT = [
  { label: "Reply to the requester", text: "Restarted the spooler.\nAsk Bob to try again." },
  { label: "", text: "a box with no label" },
];

describe("SessionEnded", () => {
  it("says the session ended, as a polite status and not a dialog", () => {
    render(<SessionEnded kept={[]} onSignIn={vi.fn()} />);
    expect(screen.getByRole("heading", { level: 1, name: "Your session has ended" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(/closed and its data cleared/);
    // NOT a dialog: nothing to dismiss, and no focus trapped over a page.
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "What you were typing" })).not.toBeInTheDocument();
  });

  it("offers the way out, rather than only naming the problem", async () => {
    const onSignIn = vi.fn();
    const user = userEvent.setup();
    render(<SessionEnded kept={[]} onSignIn={onSignIn} />);

    await user.click(screen.getByRole("button", { name: "Sign in again" }));

    expect(onSignIn).toHaveBeenCalledTimes(1);
  });

  it("shows what was typed read-only, under its own label, to copy", () => {
    render(<SessionEnded kept={KEPT} onSignIn={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent(/copy what you want to keep/);
    const region = screen.getByRole("region", { name: "What you were typing" });
    const reply = within(region).getByRole("textbox", { name: "Reply to the requester" });
    expect(reply).toHaveValue("Restarted the spooler.\nAsk Bob to try again.");
    expect(reply).toHaveAttribute("readonly");
    expect(within(region).getByRole("textbox", { name: "Text 2" })).toHaveValue("a box with no label");
    // RE-POINTED (final re-review, I3): only longer text is kept now, and the
    // promise says so; it still promises no password and no code.
    expect(region).toHaveTextContent(/Only longer text, such as replies and notes, is kept; never a password or a code\./);
  });

  it("is the page's main landmark on its own, and a named region when embedded in another page", () => {
    const { unmount } = render(<SessionEnded kept={[]} onSignIn={vi.fn()} />);
    expect(screen.getByRole("main")).toContainElement(screen.getByRole("heading", { name: "Your session has ended" }));
    unmount();
    render(
      <main>
        <SessionEnded kept={KEPT.slice(0, 1)} onSignIn={vi.fn()} embedded />
      </main>,
    );
    expect(screen.getAllByRole("main")).toHaveLength(1);
    expect(screen.getByRole("region", { name: "Your session has ended" })).toBeInTheDocument();
  });

  it("is on /__preview, with a kept field, so the browser's a11y pass measures it (final re-review, M2)", () => {
    const preview = readFileSync(resolve(import.meta.dirname, "../preview/PreviewPage.tsx"), "utf8");
    // The element itself, by its whole name, and the import that makes it this component.
    expect(preview).toMatch(/^import \{ SessionEnded \} from "\.\.\/shell\/SessionEnded";$/m);
    const at = preview.search(/<SessionEnded\s/);
    expect(at).toBeGreaterThan(-1);
    const element = preview.slice(at, preview.indexOf("/>", at));
    expect(element).toMatch(/kept=\{/);
    expect(element).toMatch(/\bembedded\b/);
  });

  it("says all of it in Spanish", () => {
    const kept = [
      { label: "Respuesta al solicitante", text: "Reinicié la cola de impresión." },
      { label: "", text: "una caja sin etiqueta" },
    ];
    const { container } = renderInSpanish(<SessionEnded kept={kept} onSignIn={vi.fn()} />);
    expect(screen.getByRole("heading", { level: 1, name: "Tu sesión ha terminado" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(/copia lo que quieras conservar/);
    expect(screen.getByRole("button", { name: "Iniciar sesión de nuevo" })).toBeInTheDocument();
    const region = screen.getByRole("region", { name: "Lo que estabas escribiendo" });
    expect(within(region).getByRole("textbox", { name: "Texto 2" })).toHaveValue("una caja sin etiqueta");
    expectNoEnglish(container);
  });
});
