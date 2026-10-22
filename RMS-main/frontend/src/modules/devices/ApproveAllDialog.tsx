/**
 * Approve all (GPO MSI spec 2026-09-30 §2.5): every computer waiting for
 * review, after a Group Policy rollout put three hundred of them there.
 *
 * THE LIST IS WHAT IS CONFIRMED, NOT "EVERYTHING PENDING". The dialog loads
 * the waiting computers once when it opens (at most 1,000), shows their
 * names, and sends exactly those ids with `reviewListDigest` of them. The
 * server approves only the listed ids that are still waiting; a computer that
 * joined after the list was shown stays waiting, and the result says how many.
 * The list is not refreshed while the dialog is open -- not by live events,
 * window focus or a reconnect -- so what is on screen is what is sent. It is
 * forgotten on close, so the next opening reads a fresh one, and it cannot be
 * confirmed while a read of it is in flight.
 *
 * INVARIANT 6: offered to a holder of `devices.review`; the route checks it.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { describeSubmitError } from "../../components/form";
import { Button, Dialog } from "../../components/ui";
import type { ApiClient, ApproveAllResult, Device } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { intlLocale } from "../../lib/datetime";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { Pager } from "../../shell/components";

/** The server's `APPROVE_ALL_MAX`. */
export const APPROVE_ALL_MAX = 1000;
/** The device list's own page limit (`GET /devices`, `le=500`). */
const FETCH_PAGE = 500;
const SHOWN_PER_PAGE = 20;
/** The dialog's own query key: NOT under `["devices"]`, which live events invalidate. */
const APPROVE_ALL_LIST_KEY = ["review-approve-all"] as const;

/**
 * SHA-256, hex, of the sorted, de-duplicated ids, one per line: the server's
 * `review_list_digest`, byte for byte.
 */
export async function reviewListDigest(ids: string[]): Promise<string> {
  const text = [...new Set(ids)].sort().join("\n");
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

/**
 * The computers waiting for review, at most `APPROVE_ALL_MAX`, read through
 * the Waiting for review tab's own query. That tab also lists rejected
 * computers whose agent may still be installed; they are not approvable and
 * are left out. `more`: there were more than one call may approve.
 */
export async function pendingForReview(api: ApiClient): Promise<{ devices: Device[]; more: boolean }> {
  // Keyed by id: the pages are read by offset over a hostname-ordered list
  // while machines keep joining, so one that joins ahead of the offset pushes
  // a machine already read onto the next page too. It is counted, shown and
  // sent once.
  const pending = new Map<string, Device>();
  let offset = 0;
  let total = 0;
  do {
    const page = await api.listDevices({ state: "review", limit: FETCH_PAGE, offset });
    total = page.total;
    for (const device of page.items) {
      if (device.review_state === "pending" && !pending.has(device.id)) pending.set(device.id, device);
    }
    offset += FETCH_PAGE;
  } while (offset < total && pending.size < APPROVE_ALL_MAX);
  return {
    devices: [...pending.values()].slice(0, APPROVE_ALL_MAX),
    more: pending.size > APPROVE_ALL_MAX || offset < total,
  };
}

/**
 * The sentence the page shows after an Approve all. `capped` says the list
 * confirmed was the first `APPROVE_ALL_MAX` of more: then whoever still waits
 * was already waiting and left for the next run, not a newcomer (Codex on #92).
 */
/** What an Approve all came to, kept as data until it is drawn. */
export interface ApproveAllOutcome {
  result: ApproveAllResult;
  capped: boolean;
}

export function approveAllMessage(result: ApproveAllResult, { capped = false }: { capped?: boolean } = {}): string {
  const parts = [translate("devices.approveAll.result.approved", { count: result.approved })];
  if (result.not_pending > 0) {
    parts.push(translate("devices.approveAll.result.notPending", { count: result.not_pending }));
  }
  if (result.still_waiting > 0) {
    parts.push(
      translate(capped ? "devices.approveAll.result.stillCapped" : "devices.approveAll.result.stillJoined", {
        count: result.still_waiting,
      }),
    );
  }
  return parts.join(" ");
}

export function ApproveAllDialog({
  open,
  onClose,
  onDone,
}: {
  open: boolean;
  onClose: () => void;
  /**
   * Handed the DATA of the result, not its sentence: the page shows it after
   * the dialog closes and words it (`approveAllMessage`) when it draws, so a
   * language switch rewords it.
   */
  onDone: (outcome: ApproveAllOutcome) => void;
}) {
  const t = useT();
  const api = useApi();
  const client = useQueryClient();
  const [page, setPage] = useState(0);
  const list = useQuery({
    // NOT under ["devices"]: live events invalidate that prefix, and the list
    // being confirmed must not change under the person confirming it.
    queryKey: APPROVE_ALL_LIST_KEY,
    queryFn: () => pendingForReview(api),
    enabled: open,
    // Read once per opening and never again while it is on screen: a focus
    // or reconnect refetch would change the list under the person confirming
    // it. `close()` removes it, so the next opening reads afresh.
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    gcTime: 0,
  });
  const approve = useMutation({
    mutationFn: async (devices: Device[]) => {
      const ids = devices.map((device) => device.id);
      return api.approveAllDevices({ device_ids: ids, list_digest: await reviewListDigest(ids) });
    },
    onSuccess: (result) => {
      // `["devices"]` is a prefix of both the list and the nav count's key.
      void client.invalidateQueries({ queryKey: ["devices"] });
      onDone({ result, capped: list.data?.more ?? false });
      close();
    },
  });

  function close() {
    approve.reset();
    setPage(0);
    client.removeQueries({ queryKey: APPROVE_ALL_LIST_KEY });
    onClose();
  }

  const devices = list.data?.devices ?? [];
  const pageCount = Math.max(1, Math.ceil(devices.length / SHOWN_PER_PAGE));
  const first = page * SHOWN_PER_PAGE;
  const shown = devices.slice(first, first + SHOWN_PER_PAGE);

  return (
    <Dialog open={open} onClose={close} title={t("devices.approveAll.title")}>
      {list.isLoading ? <p className="note">{t("devices.approveAll.loading")}</p> : null}
      {list.error ? (
        <p role="alert" className="form-error">
          {describeSubmitError(list.error)}
        </p>
      ) : null}
      {list.data && devices.length === 0 ? <p className="note">{t("devices.approveAll.none")}</p> : null}
      {devices.length > 0 ? (
        <>
          <p className="note mb-3">
            {t("devices.approveAll.intro", { count: devices.length })}
          </p>
          {list.data?.more ? (
            <p className="note mb-3">
              {t("devices.approveAll.more", { max: APPROVE_ALL_MAX.toLocaleString(intlLocale()) })}
            </p>
          ) : null}
          <ul aria-label={t("devices.approveAll.listAria")}>
            {shown.map((device) => (
              <li key={device.id}>
                {device.hostname ?? device.primary_ip ?? device.id}
                {device.link_joined_from ? t("devices.approveAll.joinedFrom", { from: device.link_joined_from }) : ""}
              </li>
            ))}
          </ul>
          <Pager
            page={page}
            pageCount={pageCount}
            onPrevious={() => setPage(page - 1)}
            onNext={() => setPage(page + 1)}
            scopeName={t("devices.approveAll.scope")}
          >
            {t("devices.approveAll.showing", { from: first + 1, to: first + shown.length, total: devices.length })}
          </Pager>
        </>
      ) : null}
      {approve.error ? (
        <p role="alert" className="form-error">
          {t("devices.approveAll.nothingApproved")} {describeSubmitError(approve.error)}
        </p>
      ) : null}
      <div className="flex items-center justify-end gap-2">
        <Button type="button" variant="ghost" onClick={close}>
          {t("common.actions.cancel")}
        </Button>
        <Button
          type="button"
          disabled={devices.length === 0 || list.isFetching || approve.isPending}
          onClick={() => approve.mutate(devices)}
        >
          {t("devices.approveAll.approveN", { count: devices.length })}
        </Button>
      </div>
    </Dialog>
  );
}
