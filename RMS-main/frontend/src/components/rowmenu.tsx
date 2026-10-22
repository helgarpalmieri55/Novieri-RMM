/**
 * A row's less frequent actions, behind one "More actions" button.
 *
 * Red on every row stops meaning anything (review 2026-09-23: Delete,
 * Deactivate and Revoke were bordered red buttons at equal weight on every
 * table). Destructive actions live here, in the menu's red, and still open the
 * page's confirmation: this moves the control, it never skips the question.
 *
 * The menu is `position: fixed` against the button so a table's horizontal
 * scroller cannot clip it; scrolling or resizing closes it rather than leaving
 * it floating away from its row. Keyboard: the first item takes focus, arrows
 * move, Escape closes and returns focus to the button.
 */

import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { useT } from "../i18n/context";
import { Icon } from "../shell/icons";

export interface RowMenuItem {
  label: string;
  onSelect: () => void;
  /** Paints the item in the critical tone. The caller still confirms. */
  destructive?: boolean;
  disabled?: boolean;
}

export function RowMenu({ subject, items }: { subject: string; items: readonly RowMenuItem[] }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [at, setAt] = useState<{ top: number; right: number } | null>(null);
  const button = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);

  const close = (returnFocus: boolean) => {
    setOpen(false);
    if (returnFocus) button.current?.focus();
  };

  useLayoutEffect(() => {
    if (!open || !button.current) return;
    const box = button.current.getBoundingClientRect();
    setAt({ top: box.bottom + 4, right: Math.max(8, window.innerWidth - box.right) });
  }, [open]);

  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector<HTMLElement>('[role="menuitem"]:not([disabled])')?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        close(true);
      }
    };
    const onPointer = (event: MouseEvent) => {
      const target = event.target as Node;
      if (!menu.current?.contains(target) && !button.current?.contains(target)) close(false);
    };
    const onMove = () => close(false);
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onPointer);
    window.addEventListener("resize", onMove);
    window.addEventListener("scroll", onMove, true);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onPointer);
      window.removeEventListener("resize", onMove);
      window.removeEventListener("scroll", onMove, true);
    };
  }, [open]);

  const move = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    const entries = Array.from(
      menu.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not([disabled])') ?? [],
    );
    if (entries.length === 0) return;
    const now = entries.indexOf(document.activeElement as HTMLElement);
    const step = event.key === "ArrowDown" ? 1 : -1;
    entries[(now + step + entries.length) % entries.length]?.focus();
  };

  return (
    <>
      <button
        ref={button}
        type="button"
        className="btn ghost small icon-only"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t("components.rowMenu.moreActionsFor", { subject })}
        title={t("components.rowMenu.moreActions")}
        onClick={(event) => {
          event.stopPropagation();
          setOpen((value) => !value);
        }}
      >
        <Icon name="more" size={16} />
      </button>
      {open ? (
        <div
          ref={menu}
          className="account-menu"
          role="menu"
          aria-label={t("components.rowMenu.actionsFor", { subject })}
          onKeyDown={move}
          style={{ position: "fixed", top: at?.top ?? 0, right: at?.right ?? 0, left: "auto", minWidth: 180 }}
        >
          {items.map((item) => (
            <button
              key={item.label}
              type="button"
              role="menuitem"
              disabled={item.disabled}
              className={item.destructive ? "menu-item menu-item-destructive" : "menu-item"}
              onClick={(event) => {
                event.stopPropagation();
                close(false);
                item.onSelect();
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
      ) : null}
    </>
  );
}
