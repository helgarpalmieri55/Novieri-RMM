/**
 * The frame every screen without a session stands in (redesign design §4:
 * «las pantallas de acceso y el consentimiento OAuth reciben la misma marca
 * dentro de su propio marco»; plan Task 13). The lockup above the card, one
 * headline, the tab named after it, and below the card the language switch
 * and the product's name.
 */
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AuthCard } from "../AuthCard";

describe("the sign-in frame (redesign design §4)", () => {
  it("carries the Novieri RMM lockup above the card", () => {
    render(<AuthCard title="Sign in">x</AuthCard>);
    expect(screen.getByRole("img", { name: "Novieri RMM" })).toBeInTheDocument();
  });

  it("names the screen in the browser tab", () => {
    render(<AuthCard title="Sign in">x</AuthCard>);
    expect(document.title).toBe("Sign in · Novieri RMM");
  });

  it("keeps one headline, at the page-title rung", () => {
    render(<AuthCard title="Sign in">x</AuthCard>);
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  it("leaves the tab alone when it is drawn inside another page", () => {
    // `/__preview` draws the frame as one exhibit among many: the tab is the
    // preview's, not the sign-in page's.
    document.title = "Preview";
    render(
      <AuthCard title="Sign in" embedded>
        x
      </AuthCard>,
    );
    expect(document.title).toBe("Preview");
    expect(screen.getByRole("region", { name: "Sign in" })).toBeInTheDocument();
  });

  it("puts the lockup outside the card and the language switch and the product's name below it", () => {
    render(<AuthCard title="Sign in">x</AuthCard>);
    const main = screen.getByRole("main");
    const card = within(main).getByRole("heading", { level: 1 }).closest(".panel") as HTMLElement;
    expect(card).not.toBeNull();
    const lockup = screen.getByRole("img", { name: "Novieri RMM" });
    expect(card).not.toContainElement(lockup);
    expect(lockup.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const language = screen.getByRole("group", { name: "Language" });
    expect(card).not.toContainElement(language);
    expect(card.compareDocumentPosition(language) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(main).getByText("Novieri RMM")).toBeInTheDocument();
  });
});
