/**
 * The account menu behind the avatar chip.
 *
 * WHY THIS COMPONENT EXISTS. The chip used to BE the sign-out button
 * (`onClick={onSignOut}`), and both UX auditors filed that as a BLOCKER on
 * 2026-08-19. The second stated the reason exactly: an avatar in the top-right
 * corner is a universally understood menu affordance — Gmail, GitHub, M365 and
 * Jira all open a menu there — so people click it to check which account they
 * are in, to reach their settings, or to switch tenant. In this product that
 * click destroyed the session and any unsaved work on the page, with no
 * confirmation and no undo, and (until the same cycle fixed the realm) landed
 * them on a Keycloak error page carrying no link back.
 *
 * KEYBOARD BEHAVIOUR IS PART OF THE FIX, not polish on top of it. A menu that
 * traps a keyboard user is a different accessibility defect from the one being
 * closed, so: Escape closes and returns focus to the chip, arrows move between
 * items, and a click outside dismisses.
 */

import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useLanguage, useT } from "../i18n/context";

export interface AccountMenuProps {
  user: { name: string; email?: string; role: string };
  onSignOut: () => void;
}

export function AccountMenu({ user, onSignOut }: AccountMenuProps) {
  const t = useT();
  const { language, setLanguage } = useLanguage();
  const [open, setOpen] = useState(false);
  const [languageOpen, setLanguageOpen] = useState(false);
  const chip = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);

  const initials = user.name
    .split(" ")
    .map((part) => part[0] ?? "")
    .join("")
    .slice(0, 2)
    .toUpperCase();
  // v3's `avatar(ini, i)` picks one of six `--av-*` tones by index. The index
  // is derived from the name so the same operator is always the same colour,
  // which is what makes the tone carry identity rather than decoration.
  const tone = ([...user.name].reduce((sum, ch) => sum + ch.charCodeAt(0), 0) % 6) + 1;

  const close = (returnFocus: boolean) => {
    setOpen(false);
    setLanguageOpen(false);
    // Escape and outside-click are different: only the first should move the
    // caret back to the chip. Returning focus after an outside click would
    // yank it away from whatever the user actually clicked on.
    if (returnFocus) chip.current?.focus();
  };

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        close(true);
      }
    };
    const onPointer = (event: MouseEvent) => {
      const target = event.target as Node;
      if (!menu.current?.contains(target) && !chip.current?.contains(target)) {
        close(false);
      }
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onPointer);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onPointer);
    };
  }, [open]);

  const move = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    const items = Array.from(
      menu.current?.querySelectorAll<HTMLElement>('[role="menuitem"], [role="menuitemradio"]') ?? [],
    );
    if (items.length === 0) return;
    const at = items.indexOf(document.activeElement as HTMLElement);
    const step = event.key === "ArrowDown" ? 1 : -1;
    // Wraps, because a menu this short has no useful "you have reached the
    // end" state and stopping dead reads as a broken key.
    items[(at + step + items.length) % items.length]?.focus();
  };

  return (
    <div className="account-menu-anchor">
      <button
        className="profile-btn"
        type="button"
        ref={chip}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <span className={`avatar av-${tone}`}>{initials}</span>
        <span className="profile-copy">
          <span className="profile-name">{user.name}</span>
          <span className="profile-role">{user.role}</span>
        </span>
      </button>

      {open ? (
        <div className="account-menu" role="menu" ref={menu} onKeyDown={move}>
          <div className="account-menu-head">
            <div className="account-menu-name">{user.name}</div>
            {/* WHICH ACCOUNT AM I. The question people open this menu to
                answer, and the chip alone cannot answer it: two people can
                share a display name, and an operator with a personal and an
                admin identity has to be able to tell them apart BEFORE acting
                on a fleet. */}
            {user.email ? <div className="account-menu-email">{user.email}</div> : null}
          </div>

          <Link className="menu-item" role="menuitem" to="/profile" onClick={() => close(false)}>
            {t("shell.accountMenu.yourProfile")}
          </Link>
          <Link className="menu-item" role="menuitem" to="/settings" onClick={() => close(false)}>
            {t("shell.accountMenu.settings")}
          </Link>

          <button
            className="menu-item"
            role="menuitem"
            type="button"
            aria-haspopup="menu"
            aria-expanded={languageOpen}
            onClick={() => setLanguageOpen((v) => !v)}
          >
            {t("shell.accountMenu.language", { language: language === "es" ? t("common.language.spanish") : t("common.language.english") })}
          </button>
          {languageOpen ? (
            <div className="account-menu-sub" role="menu" aria-label={t("common.language.label")}>
              {(["en", "es"] as const).map((l) => (
                <button
                  key={l}
                  className="menu-item"
                  role="menuitemradio"
                  aria-checked={language === l}
                  type="button"
                  onClick={() => {
                    setLanguage(l);
                    setLanguageOpen(false);
                    close(true);
                  }}
                >
                  {l === "es" ? t("common.language.spanish") : t("common.language.english")}
                </button>
              ))}
            </div>
          ) : null}

          <button
            className="menu-item menu-item-destructive"
            role="menuitem"
            type="button"
            onClick={() => {
              close(false);
              onSignOut();
            }}
          >
            {t("shell.accountMenu.signOut")}
          </button>
        </div>
      ) : null}
    </div>
  );
}
