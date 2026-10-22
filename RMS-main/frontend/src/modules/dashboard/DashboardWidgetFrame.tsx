import type { ReactNode } from "react";

import { Button } from "../../components/ui";
import { useT } from "../../i18n/context";
import { Icon } from "../../shell/icons";
import { nextSize, SIZE_LABELS, type WidgetPlacement } from "./layout";

/**
 * One widget on the dashboard, and — in edit mode — the controls that move it.
 *
 * THE BUTTONS ARE THE PRIMARY WAY TO REARRANGE, NOT A FALLBACK. WCAG 2.2 SC
 * 2.5.7 (Dragging Movements) requires a single-pointer alternative to any
 * drag, and SC 2.1.1 requires a keyboard one; `docs/product.md` §11 makes AA
 * binding for this console. Move / Size / Hide satisfy both by construction,
 * and the drag handling below is then a convenience for a mouse rather than
 * the only door in. Building it the other way round — drag first, keyboard
 * "later" — is how a rearrangeable dashboard ships inaccessible.
 *
 * EVERY BUTTON NAMES ITS WIDGET in its accessible name ("Move Open alerts
 * earlier", not "Move earlier"). A dashboard in edit mode has three buttons
 * per widget and up to a dozen widgets; a screen reader user tabbing through
 * three dozen controls called "Move earlier" cannot tell which is which, and
 * the visible label beside them is not what the control announces.
 *
 * A DISABLED MOVE, NOT AN INERT ONE. The first widget's "earlier" and the
 * last's "later" do nothing, and `moveWidget` returns the same array for them
 * so no save is attempted. Disabled says why nothing happened; a button that
 * silently no-ops reads as broken.
 */
export function DashboardWidgetFrame({
  placement,
  title,
  editing,
  first,
  last,
  dragging,
  dropTarget,
  onMove,
  onResize,
  onToggleHidden,
  onDragStart,
  onDragOver,
  onDrop,
  onDragEnd,
  children,
}: {
  placement: WidgetPlacement;
  title: string;
  editing: boolean;
  first: boolean;
  last: boolean;
  dragging: boolean;
  dropTarget: boolean;
  onMove: (delta: -1 | 1) => void;
  onResize: () => void;
  onToggleHidden: () => void;
  onDragStart: () => void;
  onDragOver: () => void;
  onDrop: () => void;
  onDragEnd: () => void;
  children: ReactNode;
}) {
  const t = useT();
  const classes = [
    "dash-widget",
    editing ? "editing" : "",
    placement.hidden ? "is-hidden" : "",
    dragging ? "dragging" : "",
    dropTarget ? "drop-target" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div
      className={classes}
      data-size={placement.size}
      data-widget={placement.id}
      // DRAGGABLE ONLY IN EDIT MODE. A dashboard whose tiles come loose under
      // an ordinary click-and-drag — while somebody is selecting the text of a
      // hostname to copy it — is a page that fights its reader.
      draggable={editing}
      onDragStart={editing ? onDragStart : undefined}
      onDragOver={
        editing
          ? (event) => {
              // Without this the drop never fires: the default for a dragover
              // is "this is not a drop target".
              event.preventDefault();
              onDragOver();
            }
          : undefined
      }
      onDrop={
        editing
          ? (event) => {
              event.preventDefault();
              onDrop();
            }
          : undefined
      }
      onDragEnd={editing ? onDragEnd : undefined}
    >
      {editing && (
        <div className="dash-widget-bar">
          <span className="dash-widget-name">{title}</span>
          <Button
            variant="ghost"
            aria-label={t("dashboard.frame.moveEarlier", { title })}
            disabled={first}
            onClick={() => onMove(-1)}
          >
            <Icon name="chevronLeft" size={14} />
          </Button>
          <Button
            variant="ghost"
            aria-label={t("dashboard.frame.moveLater", { title })}
            disabled={last}
            onClick={() => onMove(1)}
          >
            <Icon name="chevronRight" size={14} />
          </Button>
          {/* ONE BUTTON THAT CYCLES, not a grow/shrink pair. With three sizes
              a pair is disabled a third of the time each, which reads as
              broken rather than as at-the-limit — and the label states both
              where it is now and where the press goes, so the control is not
              a guess. */}
          <Button
            variant="ghost"
            aria-label={t("dashboard.frame.resize", {
              title,
              current: t(SIZE_LABELS[placement.size]).toLowerCase(),
              next: t(SIZE_LABELS[nextSize(placement.size)]).toLowerCase(),
            })}
            onClick={onResize}
          >
            {t(SIZE_LABELS[placement.size])}
          </Button>
          <Button
            variant="ghost"
            aria-label={
              placement.hidden
                ? t("dashboard.frame.showAria", { title })
                : t("dashboard.frame.hideAria", { title })
            }
            onClick={onToggleHidden}
          >
            {placement.hidden ? t("dashboard.frame.show") : t("dashboard.frame.hide")}
          </Button>
        </div>
      )}
      {/* A HIDDEN WIDGET KEEPS ITS PLACE IN EDIT MODE, as an empty frame.
          Removing it from the DOM would mean "show" had nowhere to put it
          back, which is how "hide" and "forget where this was" become the
          same gesture. Outside edit mode it is not rendered at all. */}
      {placement.hidden && editing ? (
        <div className="dash-widget-hidden-note">{t("dashboard.frame.hiddenNote")}</div>
      ) : (
        <div className="dash-widget-body">{children}</div>
      )}
    </div>
  );
}
