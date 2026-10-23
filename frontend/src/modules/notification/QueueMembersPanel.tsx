import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { EmptyState } from "../../components/emptystate";
import { describeSubmitError } from "../../components/form";
import { EntityPicker } from "../../components/picker";
import { Button, Field, inputClass, Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { ApiError } from "../../lib/api";
import { Panel } from "../../shell/components";
import { Person } from "../../shell/viz";
import { initialsOf, personIndex } from "../../lib/people";
import { useApi } from "../../lib/ApiContext";
import { userPickerEmptyHint, userPickerOptions } from "../service-desk/pickerOptions";
import {
  useAddNotificationQueueMemberMutation,
  useNotificationQueueMembers,
  useRemoveNotificationQueueMemberMutation,
} from "./api";

// Spec §2.4/§11: "Queue members -- add/remove users per queue." Queue
// membership decides routing's human recipients (spec §0.3) but the queues
// themselves are service-desk's (`ticket.read`), a cross-module read
// independent of this page's `notification.read`/`notification.admin` gate
// -- degrades to a free-text queue-id field on a 403 rather than blocking
// this panel on a permission this module does not itself require (mirrors
// RulesPanel's queue-scope field).
export function QueueMembersPanel({ canAdmin }: { canAdmin: boolean }) {
  const t = useT();
  const api = useApi();
  const queues = useQuery({
    queryKey: ["notification-queue-members-ticket-queues"],
    queryFn: () => api.listTicketQueues(),
    retry: false,
  });
  const queuesForbidden = queues.error instanceof ApiError && queues.error.status === 403;

  const [queueId, setQueueId] = useState("");
  const [userId, setUserId] = useState("");

  const members = useNotificationQueueMembers(queueId || undefined);
  const add = useAddNotificationQueueMemberMutation();
  const remove = useRemoveNotificationQueueMemberMutation();

  const forbidden = members.error instanceof ApiError && members.error.status === 403;
  const failed = members.error != null && !forbidden;

  // Cycle 6: "Add user by ID" was a free-text box that took a raw UUID. Nobody
  // knows a colleague's UUID, so in practice the field is either left blank or
  // filled with something copied out of an address bar -- and queue membership
  // decides WHO GETS NOTIFIED AT ALL (spec §0.3), so the failure of a wrong one
  // is silence for the person who should have been paged.
  //
  // `/users` carries its own permission, independent of `notification.admin`,
  // so this degrades the same way the queue list above does: a 403 leaves the
  // free-text field as the fallback rather than removing the only way to add
  // somebody.
  const users = useQuery({
    queryKey: ["notification-queue-members-users"],
    queryFn: () => api.listUsers(),
    retry: false,
  });
  const usersForbidden = users.error instanceof ApiError && users.error.status === 403;

  // The sublabel is the email plus a non-active status. Two colleagues share a
  // display name far more often than an address, and adding a `disabled` or
  // `deleted` account to a notification queue is routing alerts into a mailbox
  // nobody reads.
  //
  // The same people-as-options the service desk uses (the status word is
  // catalogued there). Not memoised: the sublabel is worded, and a memo would
  // keep the language the page was in.
  const userOptions = userPickerOptions(users.data?.users ?? []);

  function userName(id: string): string {
    return userOptions.find((o) => o.id === id)?.label ?? id;
  }

  function submitAdd() {
    if (!queueId || !userId) return;
    add.mutate({ queueId, userId }, { onSuccess: () => setUserId("") });
  }

  return (
    <Panel title={t("notification.queueMembers.title")} subtitle={t("notification.queueMembers.subtitle")}>
      <Field label={t("notification.common.queue")}>
        {queuesForbidden || (queues.data && queues.data.length === 0) ? (
          <input
            className={inputClass}
            placeholder={t("notification.common.queueId")}
            value={queueId}
            onChange={(e) => setQueueId(e.target.value)}
          />
        ) : (
          <select className={inputClass} value={queueId} onChange={(e) => setQueueId(e.target.value)}>
            <option value="">{t("notification.common.selectQueue")}</option>
            {queues.data?.map((q) => (
              <option key={q.id} value={q.id}>
                {q.name}
              </option>
            ))}
          </select>
        )}
      </Field>

      {/* THE RESTING STATE IS A STATE, NOT A BLANK. With no queue chosen this
          panel was a heading, a subtitle, one select and about 60px of empty
          card -- a surface that has not been used and a surface that is broken
          look the same when both are white (design review M6). Every sibling
          on this console answers that with `EmptyState`: /software's "No
          software selected", /bcp/report's "No period chosen", /security's
          "Select an event". This is the same shape and the same component.

          `headingLevel` STAYS 3: this panel HAS a title, so the panel's `h2`
          is the rung above and `h3` is the next one down. An UNTITLED panel
          would need `2` here -- there would be no `h2` for the `h3` to follow
          and axe would report `heading-order`. */}
      {!queueId && (
        <EmptyState
          icon="users"
          title={t("notification.queueMembers.noneSelected")}
          body={t("notification.queueMembers.noneSelectedBody")}
        />
      )}

      {queueId && (
        <>
          {members.isLoading ? (
            <p className="note">{t("notification.common.loading")}</p>
          ) : forbidden ? (
            <p className="note">{t("notification.queueMembers.forbidden")}</p>
          ) : failed ? (
            <p role="alert" className="notice red">
              {t("notification.queueMembers.failed", { reason: describeSubmitError(members.error) })}
            </p>
          ) : (
            <Table>
              <thead>
                <tr>
                  <Th>{t("notification.queueMembers.colMember")}</Th>
                  {canAdmin && <Th>{""}</Th>}
                </tr>
              </thead>
              <tbody>
                {members.data?.map((m) => (
                  <tr key={m.user_id}>
                    {/* A UUID column tells a reader nothing about who is on
                        the receiving end of this queue's notifications. Falls
                        back to the id rather than to blank when the caller
                        cannot list users. */}
                    <Td>
                      <span title={m.user_id}>
                        <Person initials={initialsOf(userName(m.user_id))} index={personIndex(m.user_id)} name={userName(m.user_id)} />
                      </span>
                    </Td>
                    {canAdmin && (
                      <Td>
                        <Button
                          variant="outline"
                          aria-label={t("notification.queueMembers.removeAria", { name: userName(m.user_id) })}
                          onClick={() => remove.mutate({ queueId, userId: m.user_id })}
                        >
                          {t("notification.queueMembers.remove")}
                        </Button>
                      </Td>
                    )}
                  </tr>
                ))}
                {members.data && members.data.length === 0 && (
                  <tr>
                    <Td colSpan={canAdmin ? 2 : 1}>
                      <span className="note">{t("notification.queueMembers.empty")}</span>
                    </Td>
                  </tr>
                )}
              </tbody>
            </Table>
          )}

          {canAdmin && (
            <div className="mt-3 flex items-end gap-2">
              {usersForbidden ? (
                <Field label={t("notification.queueMembers.addById")}>
                  <input
                    className={inputClass}
                    value={userId}
                    onChange={(e) => setUserId(e.target.value)}
                  />
                </Field>
              ) : (
                <EntityPicker
                  label={t("notification.queueMembers.addMember")}
                  options={userOptions}
                  value={userId}
                  onChange={setUserId}
                  loading={users.isLoading}
                  emptyHint={userPickerEmptyHint()}
                  placeholder={t("notification.queueMembers.searchPeople")}
                />
              )}
              <Button onClick={submitAdd} disabled={add.isPending || !userId}>
                {add.isPending ? t("notification.queueMembers.adding") : t("notification.queueMembers.addMember")}
              </Button>
            </div>
          )}
          {/* Queue membership decides who is notified at all (spec §0.3), so
              a Remove that quietly failed leaves someone still receiving --
              and a bodyless 503 through `String(error)` printed "Error: ",
              which is the failure presenting as success. */}
          {(add.isError || remove.isError) && (
            <p role="alert" className="notice red">
              {t("notification.queueMembers.notChanged", { reason: describeSubmitError(add.error ?? remove.error) })}
            </p>
          )}
        </>
      )}
    </Panel>
  );
}
