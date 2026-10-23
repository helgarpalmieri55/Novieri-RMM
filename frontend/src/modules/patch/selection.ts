/**
 * TURNING A FILTER INTO THE EXPLICIT LIST THE OPERATOR DECIDED ON.
 *
 * THE DEFECT THIS CLOSES. The operator, 2026-08-27: "i have to go back and
 * approve 25 updates per page, that's not ideal". The console's selection was
 * bounded by the page — `UPDATE_PAGE_DEFAULT` is 25 — so clearing 300 updates
 * was twelve page-loads of ticking, and the "select everything shown" checkbox
 * in the table header selected 25 of them.
 *
 * WHY THIS IS NOT "SEND THE FILTER TO THE SERVER".
 * `BulkApprovalCreateRequest.patch_ids` is an explicit list rather than a
 * filter, deliberately, and its docstring is the reason: "A filter ('approve
 * everything critical') is evaluated on the server AFTER the operator has read
 * the screen, so the set that gets cleared is not the set they looked at; a
 * list is exactly what they ticked." Adding a filter-shaped approve endpoint
 * would trade a real guarantee for a shorter request.
 *
 * So the filter is resolved HERE, in the browser, at the moment the operator
 * asks for it: the same `GET /patch/updates` the table is paged from, walked
 * at its 200-row ceiling until the criterion is exhausted, producing an
 * explicit id list. The count is on screen before the operator commits, the
 * confirmation repeats it, and the POST carries the list. The set approved is
 * the set they decided on; what changed is that they did not have to tick it
 * twenty-five at a time.
 *
 * THE CEILING IS ANSWERED, NOT HIDDEN. `approve_patches` refuses above
 * `BULK_APPROVAL_MAX`, and a resolver that quietly returned the first 500 of
 * 812 would be committing the operator to a number the screen never showed
 * them. `matching` and `truncated` come back so the caller can say what will
 * happen — see `selectionCeilingNotice` — and the label on the control reads
 * "Select the first 500 of 812" rather than "Select all".
 */

import type {
  PatchDeviceRollupFilters,
  PatchDeviceRollupListResponse,
  PatchUpdateFilters,
  PatchUpdateListResponse,
} from "./types";

/** The ceiling `approve_patches` enforces (`backend/app/modules/patch/approve.py`,
 *  `BULK_APPROVAL_MAX`). Mirrored rather than fetched: the console has to be
 *  able to SAY what the ceiling is before it hits it, and a number it learns
 *  from a 422 is a number it learns too late.
 *
 *  IT IS NOT RAISED HERE, and the argument for raising it does not hold. The
 *  cap is not a performance limit — it is the size of a decision. 500 rows is
 *  already more than anyone reads; the thing that made 500 unreachable was the
 *  25-row selection, and that is what this module fixes. */
export const BULK_APPROVAL_MAX = 500;

/** The page size the RESOLVER walks at — `UPDATE_PAGE_MAX`, the largest the
 *  route serves. Deliberately not the 25 the table renders: nobody reads these
 *  rows, they are being counted, so the only thing that matters is how many
 *  round trips the criterion costs. 500 at 200 a page is three. */
export const UPDATE_FETCH_PAGE = 200;

/** What the criterion actually resolved to. */
export interface MatchingSelection {
  /** The explicit ids, in the server's own sort order. Never longer than the
   *  ceiling. */
  ids: string[];
  /** How many rows the criterion matches in total, ceiling ignored. */
  matching: number;
  /** True when `matching` exceeded the ceiling, so `ids` is a prefix. */
  truncated: boolean;
}

type UpdateLister = (params: PatchUpdateFilters) => Promise<PatchUpdateListResponse>;

/**
 * Walk `GET /patch/updates` under `filters` and return the ids it matches.
 *
 * `filters.offset` and `filters.limit` are IGNORED — the caller's paging is a
 * property of the table, not of the criterion, and resolving "everything
 * matching" from page 3 would silently drop pages 1 and 2.
 *
 * TERMINATION IS ARITHMETIC, NOT A PROMISE. The walk stops at the number of
 * pages `total` implies, and again on any page that comes back empty, so a
 * server whose `total` disagrees with what it serves ends the loop rather than
 * spinning it.
 */
export async function resolveMatchingUpdateIds(
  listUpdates: UpdateLister,
  filters: PatchUpdateFilters,
  ceiling: number = BULK_APPROVAL_MAX,
): Promise<MatchingSelection> {
  const { offset: _offset, limit: _limit, ...criterion } = filters;

  const first = await listUpdates({ ...criterion, limit: UPDATE_FETCH_PAGE, offset: 0 });
  const matching = first.total;
  const wanted = Math.min(matching, ceiling);

  const ids: string[] = [];
  const seen = new Set<string>();
  const take = (page: PatchUpdateListResponse): void => {
    for (const update of page.items) {
      if (ids.length >= wanted) return;
      // DEDUPED, because the walk is several requests against a table other
      // people are also writing to: a scan completing mid-walk can shift a row
      // across a page boundary and hand us the same patch twice. A duplicate
      // id in `patch_ids` is not fatal server-side, but it makes the count on
      // screen disagree with the count in the request, and this module's whole
      // history is tile/table disagreements.
      if (seen.has(update.id)) continue;
      seen.add(update.id);
      ids.push(update.id);
    }
  };

  take(first);
  for (let offset = UPDATE_FETCH_PAGE; ids.length < wanted && offset < matching; offset += UPDATE_FETCH_PAGE) {
    const page = await listUpdates({ ...criterion, limit: UPDATE_FETCH_PAGE, offset });
    if (page.items.length === 0) break;
    take(page);
  }

  return { ids, matching, truncated: matching > ceiling };
}

/* ------------------------------------------------------------------------ *
 * THE SAME MOVE, ONE ROW-TYPE ALONG: A FILTER OF MACHINES INTO A LIST OF THEM.
 *
 * The Linux tab's unit of decision is the MACHINE (operator, 2026-08-26:
 * "Unit of decision: the machine ... apt/dnf resolve dependencies as a SET"),
 * so its selection is over `GET /patch/device-rollup` rather than
 * `GET /patch/updates`. Everything above applies unchanged: the criterion is
 * resolved in the browser at the instant it is asked for, the count is on
 * screen before anything is sent, and `POST /apply` carries an explicit
 * `device_ids` list rather than a filter the server would evaluate after the
 * operator stopped looking.
 * ------------------------------------------------------------------------ */

/**
 * The most machines one click may dispatch an install to.
 *
 * THIS IS A CONSOLE GUARD AND THE SERVER ENFORCES NOTHING LIKE IT.
 * `dispatch_apply` will happily build a signed job for every device it is
 * handed; `ApplyRequest.device_ids` has no bound. So unlike `BULK_APPROVAL_MAX`
 * — which mirrors a real 422 — this number is a limit this page is choosing,
 * and it is chosen for the same reason that one was: it is the size of a
 * decision, not a performance ceiling. An install is code running as root on
 * other people's machines, and "select all" reaching past two hundred of them
 * from a list nobody scrolled is not a decision anybody made.
 *
 * It is also exactly one round trip at `HOST_FETCH_PAGE`, which is a
 * coincidence worth keeping: past this, the operator is filtering rather than
 * selecting, and filtering is the thing that makes the set readable.
 */
export const APPLY_HOST_MAX = 200;

/** The page size the host resolver walks at — `UPDATE_PAGE_MAX`, which
 *  `device_rollup` shares. */
export const HOST_FETCH_PAGE = 200;

/** One machine, named. The id is what `POST /apply` takes; the hostname is what
 *  the confirmation has to be able to print, and re-deriving it from a page the
 *  operator may have paged away from is how a dialog comes to name the wrong
 *  machine. */
export interface SelectedHost {
  id: string;
  hostname: string;
}

export interface MatchingHosts {
  hosts: SelectedHost[];
  matching: number;
  truncated: boolean;
}

type HostLister = (
  params: PatchDeviceRollupFilters,
) => Promise<PatchDeviceRollupListResponse>;

/**
 * Walk `GET /patch/device-rollup` under `filters` and return the machines it
 * matches, worst-first — the server's own order, which is severity rank then
 * outstanding count then hostname.
 *
 * The order matters more here than it does for updates: past the ceiling this
 * returns a PREFIX, and a prefix of a worst-first list is the two hundred
 * machines most worth installing on. A prefix of an alphabetical list would be
 * every host beginning with "a".
 */
export async function resolveMatchingHostIds(
  listHosts: HostLister,
  filters: PatchDeviceRollupFilters,
  ceiling: number = APPLY_HOST_MAX,
): Promise<MatchingHosts> {
  const { offset: _offset, limit: _limit, ...criterion } = filters;

  const first = await listHosts({ ...criterion, limit: HOST_FETCH_PAGE, offset: 0 });
  const matching = first.total;
  const wanted = Math.min(matching, ceiling);

  const hosts: SelectedHost[] = [];
  const seen = new Set<string>();
  const take = (page: PatchDeviceRollupListResponse): void => {
    for (const device of page.items) {
      if (hosts.length >= wanted) return;
      if (seen.has(device.device_id)) continue;
      seen.add(device.device_id);
      hosts.push({ id: device.device_id, hostname: device.hostname });
    }
  };

  take(first);
  for (
    let offset = HOST_FETCH_PAGE;
    hosts.length < wanted && offset < matching;
    offset += HOST_FETCH_PAGE
  ) {
    const page = await listHosts({ ...criterion, limit: HOST_FETCH_PAGE, offset });
    if (page.items.length === 0) break;
    take(page);
  }

  return { hosts, matching, truncated: matching > ceiling };
}
