import { useEffect, useMemo, useState, type ReactNode } from "react";

import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import { Btn } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { KIND_ICON } from "../../shell/viz";
import { DashboardWidgetFrame } from "./DashboardWidgetFrame";
import {
  moveWidget,
  moveWidgetTo,
  nextSize,
  setWidgetHidden,
  setWidgetSize,
  type WidgetPlacement,
  type WidgetSize,
} from "./layout";
import { useDashboardLayout } from "./useDashboardLayout";

/** One thing the dashboard can show, and where it sits by default. */
export interface DashboardWidget {
  /** PERMANENT ONCE SHIPPED — it is the id stored in `dashboard_layouts`. */
  id: string;
  /** What the edit-mode toolbar calls it, and what every button announces. */
  title: string;
  /** The read code its query needs. Filtered before the layout is resolved. */
  permission: string;
  defaultSize: WidgetSize;
  node: ReactNode;
}

/**
 * The dashboard's arrangement, resolved: which widgets this caller may see,
 * where the stored layout puts them, and the writes that move them.
 *
 * A HOOK RATHER THAN STATE INSIDE THE CANVAS, because v3 draws the Customize
 * control in the page head and the canvas below it. The page owns the head,
 * so the page has to own the arrangement the head's button edits; the canvas
 * renders it.
 */
export interface DashboardArrangement {
  visible: DashboardWidget[];
  /** The placements as rendered: the stock order while the stored one is in flight. */
  placements: WidgetPlacement[];
  loading: boolean;
  apply: (update: (current: WidgetPlacement[]) => WidgetPlacement[]) => void;
  reset: () => void;
  resetting: boolean;
  saveError: unknown;
}

export function useDashboardArrangement(
  catalogue: DashboardWidget[],
  can: (code: string) => boolean,
): DashboardArrangement {
  // FILTERED BEFORE THE LAYOUT IS RESOLVED. A widget the caller may not see is
  // not in the arrangement at all, rather than present-and-hidden — otherwise
  // a role that changed for an afternoon would rewrite a saved layout.
  const visible = useMemo(
    () => catalogue.filter((widget) => can(widget.permission)),
    [catalogue, can],
  );
  const availableKey = visible.map((widget) => widget.id).join(",");
  const available = useMemo(
    () => visible.map((widget) => ({ id: widget.id, defaultSize: widget.defaultSize })),
    // Keyed on the ids: `visible` is a new array every render (its `node`s are
    // fresh JSX), and depending on it would re-seed the layout continuously.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [availableKey],
  );

  const { layout, loading, apply, reset, saveError, resetting } = useDashboardLayout(available);
  // The stock arrangement while the stored one is in flight. Rendering nothing
  // would blank the first screen on every page load for the sake of a
  // preference; rendering the defaults means a customized dashboard settles
  // into place rather than appearing from nothing.
  const placements =
    layout ??
    visible.map((widget) => ({ id: widget.id, size: widget.defaultSize, hidden: false }));

  return { visible, placements, loading, apply, reset, resetting, saveError };
}

/**
 * The dashboard's grid, and the controls that change it.
 *
 * The operator, 2026-09-01: "main dashboard should be customizable with tiles
 * also give me the ability to resize and order in whichever way i want."
 *
 * EDIT MODE IS A MODE, and that is deliberate rather than a shortcut. A
 * dashboard whose tiles are always draggable fights its reader: selecting a
 * hostname to copy it becomes a drag, and a toolbar over every panel turns a
 * glance-at screen into a settings screen. Outside edit mode this renders
 * exactly what the page rendered before — the widgets, in the arrangement, and
 * nothing else.
 *
 * WHAT IS SAVED, AND WHEN. Every change writes through immediately
 * (`useDashboardLayout`), so there is no Save button to forget and no
 * unsaved-work state to lose on navigation. "Done" only leaves edit mode. If a
 * save fails the arrangement STAYS as the operator left it and the failure is
 * stated; discarding visible work to reach a state nobody asked for is worse
 * than a stale row.
 */
export function DashboardCanvas({
  arrangement,
  editing,
}: {
  arrangement: DashboardArrangement;
  editing: boolean;
}) {
  const t = useT();
  const { visible, placements, apply, reset, resetting, saveError } = arrangement;
  const [dragging, setDragging] = useState<string | null>(null);
  const [over, setOver] = useState<string | null>(null);

  // LEAVING EDIT MODE DROPS WHATEVER WAS IN THE HAND. A drag that is still
  // "in progress" when the operator presses Done left `dragging`/`over` set,
  // and `DashboardWidgetFrame` paints `.dragging`/`.drop-target` from those
  // props without consulting `editing` -- so a widget kept the lifted styling
  // on a read-only dashboard, and re-entering edit mode resumed a drag nobody
  // had started. Cleared here rather than gated at the frame, because the
  // stale state is the defect; the styling is only how it showed.
  useEffect(() => {
    if (!editing) {
      setDragging(null);
      setOver(null);
    }
  }, [editing]);

  const byId = useMemo(() => new Map(visible.map((w) => [w.id, w])), [visible]);

  if (visible.length === 0) return null;

  const shown = editing ? placements : placements.filter((entry) => !entry.hidden);
  const hiddenCount = placements.filter((entry) => entry.hidden).length;

  return (
    <>
      {editing && (
        <div className="tb">
          {/* WHAT EDIT MODE IS FOR, said once rather than as a tooltip on
              each of three dozen buttons. */}
          <p className="tb-l note">
            {t("dashboard.canvas.toolbar")}
            {hiddenCount > 0 ? ` ${t("dashboard.canvas.hidden", { count: hiddenCount })}` : ""}
          </p>
          <div className="tb-r">
            <Btn label={t("dashboard.canvas.reset")} icon="refresh" small onClick={reset} disabled={resetting} />
          </div>
        </div>
      )}

      {/* `role="alert"` so a save that failed is announced rather than
          discovered later by the layout not being there next time. */}
      {saveError != null && (
        <div className="notice red" role="alert">
          <Icon name={KIND_ICON.crit} size={17} />
          <div>{t("dashboard.canvas.saveError", { reason: describeSubmitError(saveError) })}</div>
        </div>
      )}

      <div className="dash-grid">
        {shown.map((placement, index) => {
          const widget = byId.get(placement.id);
          if (widget == null) return null;
          return (
            <DashboardWidgetFrame
              key={placement.id}
              placement={placement}
              title={widget.title}
              editing={editing}
              first={index === 0}
              last={index === shown.length - 1}
              dragging={dragging === placement.id}
              dropTarget={over === placement.id && dragging !== placement.id}
              onMove={(delta) => apply((current) => moveWidget(current, placement.id, delta))}
              onResize={() =>
                apply((current) => setWidgetSize(current, placement.id, nextSize(placement.size)))
              }
              onToggleHidden={() =>
                apply((current) => setWidgetHidden(current, placement.id, !placement.hidden))
              }
              onDragStart={() => setDragging(placement.id)}
              onDragOver={() => setOver(placement.id)}
              onDrop={() => {
                const held = dragging;
                setDragging(null);
                setOver(null);
                if (held == null || held === placement.id) return;
                // The index is taken from the LIVE list inside `apply`, not
                // from this render's `index`: in edit mode `shown` includes
                // hidden entries, and a drop resolved against a stale array
                // would land the widget one place off after any change that
                // arrived in between.
                apply((current) => {
                  const target = current.findIndex((entry) => entry.id === placement.id);
                  return target < 0 ? current : moveWidgetTo(current, held, target);
                });
              }}
              onDragEnd={() => {
                setDragging(null);
                setOver(null);
              }}
            >
              {widget.node}
            </DashboardWidgetFrame>
          );
        })}
      </div>
    </>
  );
}
