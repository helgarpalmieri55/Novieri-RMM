/**
 * "Before this page can do its job": what another module has to have set up,
 * whether it is, and a link to where it is fixed when it is not.
 *
 * THE OPERATOR ASKED FOR IT (2026-09-24): every console module should guide
 * them to the modules it depends on (`docs/design/cross-module-guidance.md`).
 * This is `modules/agent-push/PushReadiness.tsx`'s pattern made shared: a
 * `Panel` with a `Steps` list, one step per prerequisite.
 *
 * FIVE STATES, BECAUSE AN OPERATOR ACTS ON EACH DIFFERENTLY:
 *
 *   done       -- set up. The "Done" pill.
 *   missing    -- checked, and not set up. The only state that carries a CTA,
 *                 drawn "now" with its severity: `crit` when the page cannot
 *                 do its job without it (the server refuses), `warn` when it
 *                 works but is emptier or silently weaker.
 *   checking   -- the query has not answered. Never printed as a zero.
 *   unchecked  -- the check could not be made: this account lacks the
 *                 permission the detecting call needs, or the call failed.
 *                 NEVER rendered as missing: "you cannot look" and "there is
 *                 none" have different remedies, and conflating them sends an
 *                 operator to set up something that already exists.
 *   waiting    -- depends on an earlier item that is not done yet.
 *
 * NOTHING HERE GATES ANYTHING (invariant 6). It reads lists the server already
 * serves; the server refuses on its own checks whatever this panel says.
 *
 * Build items with `prerequisiteFromQuery` (below) or with the shared detector
 * hooks in `lib/detectors.ts`, so no caller has to remember the loading, error
 * and permission branches for itself.
 */

import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { useT } from "../i18n/context";
import { translate } from "../i18n/core";
import { ApiError } from "../lib/api";
import type { MePermissions } from "../lib/mePermissions";
import { permissionsSentence } from "../lib/permissions";
import { Panel } from "../shell/components";
import { Steps, type Kind, type StepState } from "../shell/viz";
import { describeSubmitError } from "./form";

export type PrerequisiteState = "done" | "missing" | "checking" | "unchecked" | "waiting";

/** How bad a MISSING prerequisite is on this page: `crit` blocks, `warn` advises. */
export type PrerequisiteSeverity = "crit" | "warn";

/** Where a missing prerequisite is fixed. Rendered as `<Link className="btn">`. */
export interface PrerequisiteCta {
  readonly to: string;
  readonly label: string;
}

export interface Prerequisite {
  /** What must be true, as a statement ("A device group exists"). Unique within one panel: it is the list key. */
  readonly label: string;
  readonly state: PrerequisiteState;
  /** Only read when `state` is `missing`. Defaults to `warn`. */
  readonly severity?: PrerequisiteSeverity;
  /** What is true right now, in a sentence. */
  readonly detail: ReactNode;
  /** Only rendered when `state` is `missing`; every other state keeps its pill. */
  readonly cta?: PrerequisiteCta;
}

const checkingDetail = (): string => translate("components.prerequisites.checking");

/** True when every item is done (and for an empty list). */
export function allPrerequisitesDone(items: readonly Prerequisite[]): boolean {
  return items.every((item) => item.state === "done");
}

function toStep(item: Prerequisite): {
  label: string;
  state: StepState;
  kind?: Kind;
  detail: ReactNode;
  action?: ReactNode;
} {
  switch (item.state) {
    case "done":
      return { label: item.label, state: "done", detail: item.detail };
    case "missing":
      return {
        label: item.label,
        state: "now",
        kind: item.severity ?? "warn",
        detail: item.detail,
        action: item.cta ? (
          <Link className="btn" to={item.cta.to}>
            {item.cta.label}
          </Link>
        ) : undefined,
      };
    case "unchecked":
      return { label: item.label, state: "todo", kind: "warn", detail: item.detail };
    case "checking":
    case "waiting":
      return { label: item.label, state: "todo", detail: item.detail };
  }
}

export interface PrerequisitesProps {
  title: string;
  subtitle?: string;
  items: readonly Prerequisite[];
  /**
   * Keep the panel when every item is done. Off by default: a page that is set
   * up should not carry a checklist forever. Push install keeps it, because
   * "everything a push needs is in place" is itself the answer an operator
   * comes to that page for.
   *
   * With this off, the panel is also held back while the only open items are
   * still `checking`, so a set-up page does not flash a checklist on every
   * visit. It appears as soon as one item is `missing`, `unchecked` or
   * `waiting`.
   */
  showWhenDone?: boolean;
  /** Passed to `Panel`: `3` when this sits inside another panel. */
  headingLevel?: 2 | 3;
}

export function Prerequisites({ title, subtitle, items, showWhenDone = false, headingLevel }: PrerequisitesProps) {
  useT();
  const open = items.some((item) => item.state !== "done" && item.state !== "checking");
  if (items.length === 0 || (!showWhenDone && !open)) return null;
  return (
    <Panel title={title} subtitle={subtitle} headingLevel={headingLevel}>
      <Steps items={items.map(toStep)} />
    </Panel>
  );
}

/* ------------------------------------------------------------------ helper */

/** The part of a TanStack `UseQueryResult` the helper reads. Any `useQuery` result fits. */
export interface QueryLike<T> {
  readonly data: T | undefined;
  readonly error: unknown;
  readonly isSuccess: boolean;
  readonly isError: boolean;
}

/**
 * Two queries as one, for a prerequisite that needs both answers (Push
 * install's site-server step reads the authorizations AND the site servers).
 * Succeeds when both have; fails with the first error when either has.
 */
export function combineQueries<A, B>(a: QueryLike<A>, b: QueryLike<B>): QueryLike<readonly [A, B]> {
  const isSuccess = a.isSuccess && b.isSuccess && a.data !== undefined && b.data !== undefined;
  return {
    data: isSuccess ? ([a.data, b.data] as readonly [A, B]) : undefined,
    error: a.isError ? a.error : b.isError ? b.error : null,
    isSuccess,
    isError: a.isError || b.isError,
  };
}

/** What a missing prerequisite says, how bad it is, and where it is fixed. */
export interface PrerequisiteMiss {
  readonly detail: ReactNode;
  readonly severity: PrerequisiteSeverity;
  readonly cta?: PrerequisiteCta;
}

export interface QueryPrerequisiteSpec<T> {
  label: string;
  /** The query whose answer decides the item. */
  query: QueryLike<T>;
  /**
   * The permission the detecting call needs, and the caller's permissions
   * (`useMePermissions()`). When `/me` has answered without the code the item
   * is `unchecked` -- "cannot be checked from this account" -- whatever the
   * query holds. While `/me` is loading or failed, the query decides.
   */
  permission?: { readonly code: string; readonly me: MePermissions };
  /**
   * Set (to the sentence saying what comes first) when this item depends on an
   * earlier one that is not done. The item is then `waiting` and the query is
   * not consulted. `null`/`undefined`/`false` means not waiting.
   */
  waitingOn?: ReactNode;
  /** The predicate: is the prerequisite in place, given the query's data? */
  isDone: (data: T) => boolean;
  /** The detail when it is. */
  done: ReactNode | ((data: T) => ReactNode);
  /** The detail, severity and CTA when it is not. */
  missing: PrerequisiteMiss | ((data: T) => PrerequisiteMiss);
}

/** The sentence for a check this account cannot make. */
export function cannotCheckDetail(code?: string): string {
  return code
    ? translate("components.prerequisites.cannotCheckNeeds", { permissions: permissionsSentence([code]) })
    : translate("components.prerequisites.cannotCheck");
}

/** The sentence for a check that failed for any reason other than permission. */
export function couldNotCheckDetail(error: unknown): string {
  return translate("components.prerequisites.couldNotCheck", { reason: describeSubmitError(error) });
}

function isMiss<T>(value: PrerequisiteMiss | ((data: T) => PrerequisiteMiss)): value is PrerequisiteMiss {
  return typeof value !== "function";
}

/**
 * One query plus a predicate, as a `Prerequisite`, with every branch a caller
 * would otherwise have to remember: waiting on an earlier step, a permission
 * the account lacks, still loading, refused (403), failed, done, missing.
 */
export function prerequisiteFromQuery<T>(spec: QueryPrerequisiteSpec<T>): Prerequisite {
  const { label, query, permission } = spec;
  if (spec.waitingOn !== undefined && spec.waitingOn !== null && spec.waitingOn !== false) {
    return { label, state: "waiting", detail: spec.waitingOn };
  }
  if (permission && permission.me.lacks(permission.code)) {
    return { label, state: "unchecked", detail: cannotCheckDetail(permission.code) };
  }
  if (query.isError) {
    if (query.error instanceof ApiError && query.error.status === 403) {
      return { label, state: "unchecked", detail: cannotCheckDetail(permission?.code) };
    }
    return { label, state: "unchecked", detail: couldNotCheckDetail(query.error) };
  }
  if (!query.isSuccess || query.data === undefined) {
    return { label, state: "checking", detail: checkingDetail() };
  }
  const data = query.data;
  if (spec.isDone(data)) {
    const detail = typeof spec.done === "function" ? (spec.done as (data: T) => ReactNode)(data) : spec.done;
    return { label, state: "done", detail };
  }
  const miss = isMiss(spec.missing) ? spec.missing : spec.missing(data);
  return {
    label,
    state: "missing",
    severity: miss.severity,
    detail: miss.detail,
    ...(miss.cta ? { cta: miss.cta } : {}),
  };
}
