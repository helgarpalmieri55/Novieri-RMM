/**
 * THE "NEW" MENU (redesign design §1.2; Atera's "New ▾"): every way to create
 * something, from every page. The chrome's one primary control (DESIGN.md v4,
 * "Buttons"). Nothing at all when the person may create nothing: no dead
 * control. A WAI-ARIA menu button: opens on its first item, arrows move,
 * Escape closes and gives focus back, and Tab leaves it closed (its items are
 * not tab stops; critique of the shell, 2026-11-24).
 */
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Link } from "react-router-dom";

import { useT } from "../i18n/context";
import type { MessageKey } from "../i18n/core";
import { Icon } from "./icons";
import type { NewMenuEntry, NewMenuKind } from "./newMenu";

const LABEL: Record<NewMenuKind, MessageKey> = {
  ticket: "shell.newMenu.ticket",
  client: "shell.newMenu.client",
  contact: "shell.newMenu.contact",
  device: "shell.newMenu.device",
  document: "shell.newMenu.document",
  change: "shell.newMenu.change",
};

export function NewMenu({ entries }: { entries: readonly NewMenuEntry[] }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);

  const close = (returnFocus: boolean) => {
    setOpen(false);
    if (returnFocus) button.current?.focus();
  };

  useEffect(() => {
    if (!open) return undefined;
    menu.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        close(true);
      }
    };
    const onPointer = (event: MouseEvent) => {
      const target = event.target as Node;
      if (!menu.current?.contains(target) && !button.current?.contains(target)) close(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onPointer);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onPointer);
    };
  }, [open]);

  const move = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const items = Array.from(menu.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? []);
    if (items.length === 0) return;
    const at = items.indexOf(document.activeElement as HTMLElement);
    const next =
      event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 : (at + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
    items[next]?.focus();
  };

  if (entries.length === 0) return null;
  return (
    <div
      className="new-menu-anchor"
      onBlur={(event) => {
        // Focus left the button and its list (Tab, or a click elsewhere).
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false);
      }}
    >
      <button
        type="button"
        ref={button}
        className="btn primary new-menu-btn"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <Icon name="plus" size={14} />
        <span className="new-menu-label">{t("shell.newMenu.label")}</span>
        <Icon name="chevron-down" size={14} className="chev" />
      </button>
      {open ? (
        <div className="top-menu" role="menu" aria-label={t("shell.newMenu.label")} ref={menu} onKeyDown={move}>
          {entries.map((entry) => (
            <Link key={entry.kind} className="menu-item" role="menuitem" tabIndex={-1} to={entry.to} onClick={() => close(false)}>
              <Icon name={entry.icon} size={15} />
              {t(LABEL[entry.kind])}
            </Link>
          ))}
        </div>
      ) : null}
    </div>
  );
}
