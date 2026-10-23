import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { AccountMenu } from "./AccountMenu";
import { I18nProvider } from "../i18n/context";
import { setActiveLanguage } from "../i18n/core";

const user = { name: "Alex Morgan", email: "alex@example.com", role: "org-admin" };

function renderMenu(onChange = vi.fn()) {
  render(
    <MemoryRouter>
      <I18nProvider initial="en" onChange={onChange}>
        <AccountMenu user={user} onSignOut={vi.fn()} />
      </I18nProvider>
    </MemoryRouter>,
  );
  return onChange;
}

describe("account menu: language", () => {
  it("puts the language row between Settings and Sign out", async () => {
    renderMenu();
    await userEvent.click(screen.getByRole("button", { name: /Alex Morgan/ }));
    const items = within(screen.getByRole("menu")).getAllByRole("menuitem").map((el) => el.textContent);
    expect(items).toEqual(["Your profile", "Settings", "Language: English", "Sign out"]);
  });

  it("switches to Spanish, re-labels the menu and reports the choice to be saved", async () => {
    const onChange = renderMenu();
    await userEvent.click(screen.getByRole("button", { name: /Alex Morgan/ }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Language: English" }));
    await userEvent.click(screen.getByRole("menuitemradio", { name: "Español" }));
    expect(onChange).toHaveBeenCalledWith("es");
    await userEvent.click(screen.getByRole("button", { name: /Alex Morgan/ }));
    expect(screen.getByRole("menuitem", { name: "Idioma: Español" })).toBeInTheDocument();
    setActiveLanguage("en");
  });

  it("marks the current language as checked", async () => {
    renderMenu();
    await userEvent.click(screen.getByRole("button", { name: /Alex Morgan/ }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Language: English" }));
    expect(screen.getByRole("menuitemradio", { name: "English" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("menuitemradio", { name: "Español" })).toHaveAttribute("aria-checked", "false");
  });
});
