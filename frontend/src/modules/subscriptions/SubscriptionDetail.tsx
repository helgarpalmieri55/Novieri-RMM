import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router-dom";
import { ConfirmDialog } from "../../components/confirm";
import { NoValue } from "../../components/emptystate";
import { attemptProblems, describeSubmitError, Form, FormActions } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { EntityPicker } from "../../components/picker";
import type { PickerOption } from "../../components/picker";
import { Button, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDate, formatDateTime } from "../../lib/datetime";
import {
  useAssignSeatMutation,
  useCancelSubscriptionMutation,
  useOrgUsers,
  useReleaseSeatMutation,
  useRenewSubscriptionMutation,
  useSeats,
  useSubscription,
  useSubscriptionEvents,
  useUpdateSubscriptionMutation,
} from "./api";
import { humaniseEnum } from "../../lib/labels";
import { ownerName, ownerPickerEmptyHint, ownerPickerOptions } from "./pickerOptions";
import { initialsOf, personIndex } from "../../lib/people";
import { billingCyclePhrase, statusKind } from "./subscriptionState";
import type { SubscriptionOut } from "./types";
import { PageHead, Panel, QueryState, StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { KV, Person } from "../../shell/viz";
import { PersonLink } from "../people/links";

/** The glyph and kind an event in the history carries. */
function eventGlyph(type: string): { icon: string; tone: "" | "green" | "red" | "gold" } {
  if (type === "cancelled") return { icon: "circle-x", tone: "red" };
  if (type === "renewed") return { icon: "repeat", tone: "green" };
  if (type === "seat_released") return { icon: "unlink", tone: "gold" };
  if (type === "seat_assigned") return { icon: "user", tone: "" };
  if (type === "created") return { icon: "plus", tone: "" };
  return { icon: "history", tone: "" };
}

/**
 * What a seat assignment may not be created without.
 *
 * `disabled={assignSeat.isPending || !assigneeName || !assigneeEmail}` was
 * the silent no-op: a click with either box empty moved nothing and named
 * neither field. The rules say which, on submit. Email gets a shape check for
 * the same reason the money fields elsewhere in this module do -- the backend
 * validates it, and the 422 that came back was rendered as `String(error)`.
 */
// Built when asked, never held: the labels and sentences follow the language.
const seatRules = (): readonly FieldRule[] => [
  { name: "assignee_name", label: translate("subscriptions.rules.assigneeName") },
  { name: "assignee_email", label: translate("subscriptions.rules.assigneeEmail"), validate: emailAddress },
];

/**
 * What the renewal-deadline edit may not contain.
 *
 * The three fields here are the ones Auditor B found had no control anywhere,
 * and the two tiles this module leads with are computed from them. `renews_at`
 * stays optional -- "renewal date not known" is a real state -- but a notice
 * period that is not a plausible number of days is not.
 */
const renewalRules = (): readonly FieldRule[] => [
  { name: "notice_days", label: translate("subscriptions.rules.noticePeriod"), validate: noticeDays },
];

function noticeDays(value: string): string | null {
  if (!/^\d+$/.test(value)) return translate("subscriptions.rules.noticeWhole");
  if (Number(value) > 365) return translate("subscriptions.rules.noticeMax");
  return null;
}

function emailAddress(value: string): string | null {
  // Deliberately loose -- one @, something either side, no whitespace. The
  // server owns the real rule; this only catches what is obviously not an
  // address before a round trip.
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)) {
    return translate("subscriptions.rules.emailShape");
  }
  return null;
}

// The seats panel below has WORKING Assign/Release controls wired to the
// real mutation hooks -- a read-only allocations table was the Phase-11
// lesson (top follow-up on that PR), not to be repeated here.
export function SubscriptionDetail() {
  const t = useT();
  const { subscriptionId } = useParams<{ subscriptionId: string }>();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  // Gating here is presentational only -- the server enforces
  // `subscriptions.manage` on every write route (api.py's
  // require_permission dependencies); hiding a control here never
  // substitutes for that server-side check.
  const canManage = (me.data?.permissions ?? []).includes("subscriptions.manage");

  const subscription = useSubscription(subscriptionId);
  const seats = useSeats(subscriptionId);
  const events = useSubscriptionEvents(subscriptionId);
  // Read only to put a name on `owner_user_id` below. `users.read` is not
  // implied by `subscriptions.read`, so this query may 403 -- which is why the
  // resolver falls back to the id rather than to a blank.
  const users = useOrgUsers();
  // Not memoised: the sublabels are worded, and a held list would keep the
  // language it was built in.
  const ownerOptions = ownerPickerOptions(users.data?.users ?? []);

  const cancel = useCancelSubscriptionMutation();
  const renew = useRenewSubscriptionMutation();
  const assignSeat = useAssignSeatMutation();
  const releaseSeat = useReleaseSeatMutation();

  const [assigneeName, setAssigneeName] = useState("");
  const [assigneeEmail, setAssigneeEmail] = useState("");
  // Cycle 8. All three of these fired on one click with no confirmation:
  // cancelling a paid contract, recording a renewal that moves the renewal
  // date and writes an immutable event, and releasing somebody's seat. Only
  // the last is undoable, and only by re-assigning it.
  const [confirmingCancel, setConfirmingCancel] = useState(false);
  const [confirmingRenew, setConfirmingRenew] = useState(false);
  const [releasing, setReleasing] = useState<{ id: string; name: string } | null>(null);
  // Populated ON SUBMIT, never while typing; held as data and worded at render.
  const [seatAttempt, setSeatAttempt] = useState<Attempt | null>(null);

  // The operator's own zone (cycle 5). `/me` is already fetched on this route
  // for the permission check, so this costs no request; a null timezone falls
  // the helper back to the browser zone.
  const zone = me.data?.timezone;

  function assign() {
    const next: Attempt = {
      rules: seatRules,
      values: { assignee_name: assigneeName, assignee_email: assigneeEmail },
    };
    setSeatAttempt(next);
    if (attemptProblems(next).length > 0 || !subscriptionId) return;

    assignSeat.mutate(
      {
        subscriptionId,
        body: { assignee_name: assigneeName.trim(), assignee_email: assigneeEmail.trim() },
      },
      {
        onSuccess: () => {
          setAssigneeName("");
          setAssigneeEmail("");
          setSeatAttempt(null);
        },
      },
    );
  }

  if (!subscriptionId) return null;

  return (
    <div>
      <QueryState
        loading={subscription.isLoading}
        error={subscription.error}
        empty={!subscription.isLoading && !subscription.error && subscription.data == null}
        emptyLabel={t("subscriptions.detail.notFound")}
      >
        <PageHead
          title={subscription.data?.service_name ?? t("subscriptions.detail.titleFallback")}
          description={t("subscriptions.detail.description")}
          actions={
            <>
              {canManage && (
                <Button
                  variant="outline"
                  disabled={renew.isPending}
                  onClick={() => setConfirmingRenew(true)}
                >
                  {renew.isPending ? t("subscriptions.detail.recording") : t("subscriptions.detail.recordRenewal")}
                </Button>
              )}
              {canManage && (
                <Button
                  variant="outline"
                  disabled={cancel.isPending}
                  onClick={() => setConfirmingCancel(true)}
                >
                  {cancel.isPending ? t("subscriptions.detail.cancelling") : t("subscriptions.detail.cancel")}
                </Button>
              )}
            </>
          }
        />

        {/* THE STATE AND THE CADENCE BELONG UNDER THE TITLE, NOT IN THE BUTTON
            ROW.

            Both used to be the first children of `actions`, so `.ph-act` -- a
            row whose whole job is "the things you can press here" -- opened
            with a pill and a sentence, neither of which can be pressed, sitting
            immediately left of "Record renewal" and "Cancel subscription"
            (design review M14). Every other record page in this console puts
            its state in a pill row directly under the head:
            /service-desk/tickets/:id, /portal/tickets/:id, /devices/:id. This
            is that row, with the same three utilities and the same negative top
            margin pulling it under the description.

            THE CADENCE IS STILL NOT A STATUS, and that distinction survives the
            move intact. It was once a second pill of the same shape carrying
            the muted minus glyph on a live monthly contract -- two pills side by
            side meaning two different kinds of thing. It is an attribute of the
            record, so the ticket page's own vocabulary for exactly that applies:
            `.chip`, which is what /service-desk/tickets/:id draws its type and
            queue as, beside pills it deliberately does not use for them.
            "Billed monthly" rather than "Monthly", because a bare cadence next
            to a status still reads like one.

            `flex items-center` IS GONE WITH THE REASON FOR IT: it was there
            because `.ph-act` stretches its items, and this row is not
            `.ph-act`. */}
        {subscription.data && (
          <div className="flex flex-wrap items-center gap-2" style={{ margin: "-8px 0 14px" }}>
            <StatusPill
              value={subscription.data.status}
              label={humaniseEnum(subscription.data.status, "SubStatus")}
              kind={statusKind(subscription.data.status)}
            />
            <span className="chip">{billingCyclePhrase(subscription.data.billing_cycle)}</span>
          </div>
        )}

        {/* CANCEL IS THE ONE THAT ENDS A CONTRACT. The subject is the service
            name and its cost per cycle, because an estate carries several
            subscriptions to near-identically-named services and the money is
            what identifies the row on the invoice. No `requireTyping`: this
            ends ONE subscription, the record survives as `cancelled`, and
            typing-to-confirm on every privileged control is how people learn
            to copy the word without reading the sentence above it. */}
        <ConfirmDialog
          open={confirmingCancel}
          title={t("subscriptions.detail.cancel")}
          subject={
            subscription.data
              ? t("subscriptions.detail.cancelSubject", {
                  name: subscription.data.service_name,
                  cost: subscription.data.cost_per_cycle,
                  currency: subscription.data.currency,
                  cycle: humaniseEnum(subscription.data.billing_cycle, "BillingCycle").toLowerCase(),
                })
              : ""
          }
          body={t("subscriptions.detail.cancelBody")}
          confirmLabel={t("subscriptions.detail.cancel")}
          destructive
          onCancel={() => setConfirmingCancel(false)}
          onConfirm={() => {
            setConfirmingCancel(false);
            cancel.mutate(subscriptionId);
          }}
        />

        {/* Recording a renewal ADVANCES the renewal date by one billing cycle
            (service.py's `record_renewal` → `advance_renewal`) and writes an
            immutable event. Pressed by mistake, it moves the deadline this
            module exists to catch out by a whole cycle and there is no undo --
            but it destroys nothing, so it is confirmed without the red. */}
        <ConfirmDialog
          open={confirmingRenew}
          title={t("subscriptions.detail.renewTitle")}
          subject={subscription.data?.service_name ?? ""}
          body={t("subscriptions.detail.renewBody")}
          confirmLabel={t("subscriptions.detail.recordRenewal")}
          onCancel={() => setConfirmingRenew(false)}
          onConfirm={() => {
            setConfirmingRenew(false);
            renew.mutate(subscriptionId);
          }}
        />

        {/* A released seat is re-assignable, so this is not destructive -- but
            it is somebody's access, and the row it sits in is one of many
            identical-looking ones. The subject is the assignee, which is
            exactly the mistake this catches: releasing the seat below the one
            intended. */}
        <ConfirmDialog
          open={releasing != null}
          title={t("subscriptions.detail.releaseTitle")}
          subject={releasing?.name ?? ""}
          body={t("subscriptions.detail.releaseBody")}
          confirmLabel={t("subscriptions.detail.releaseTitle")}
          onCancel={() => setReleasing(null)}
          onConfirm={() => {
            if (releasing) releaseSeat.mutate({ seatId: releasing.id, subscriptionId });
            setReleasing(null);
          }}
        />

        {/* `String(cancel.error)` rendered "ApiError: " and nothing after it
            for a bodyless 503 -- the exact shape of Auditor A's BLOCKER, on a
            control that ends a paid contract -- and it was in no live region,
            so a screen reader was told nothing at all. `describeSubmitError`
            has a sentence for every status, and it says "Nothing was changed"
            where that is true. */}
        {cancel.isError && (
          <p role="alert" className="notice red">
            <Icon name="circle-x" size={16} />
            <span>{describeSubmitError(cancel.error)}</span>
          </p>
        )}
        {renew.isError && (
          <p role="alert" className="notice red">
            <Icon name="circle-x" size={16} />
            <span>{describeSubmitError(renew.error)}</span>
          </p>
        )}

        {subscription.data && (
          <Panel title={t("subscriptions.detail.details")}>
            {/* v3's `kv`. Money is a Decimal on the wire; rendered verbatim in
                the subscription's own recorded currency, never assumed USD.
                The owner is A NAME, NOT A UUID (cycle 6): `ownerName` falls
                back to the id rather than to a blank, because an id whose
                account this caller may not read is still more than nothing.
                CALENDAR DATES, NOT INSTANTS: `started_at` and `renews_at` are
                `date` in schema.py, pinned to UTC so the day shown is the day
                recorded. */}
            <KV
              items={[
                [t("subscriptions.detail.plan"), subscription.data.plan || <NoValue label={t("subscriptions.detail.noPlan")} />],
                [t("subscriptions.detail.category"), subscription.data.category || <NoValue label={t("subscriptions.detail.noCategory")} />],
                [
                  t("subscriptions.detail.cost"),
                  <span className="mono">{`${subscription.data.cost_per_cycle} ${subscription.data.currency}`}</span>,
                ],
                [t("subscriptions.detail.seats"), <span className="mono">{subscription.data.seats}</span>],
                [
                  t("subscriptions.detail.owner"),
                  (() => {
                    const owner = ownerName(ownerOptions, subscription.data.owner_user_id);
                    return owner && subscription.data.owner_user_id ? (
                      <Person
                        initials={initialsOf(owner)}
                        index={personIndex(subscription.data.owner_user_id)}
                        name={owner}
                      />
                    ) : (
                      <NoValue label={t("subscriptions.detail.noOwner")} />
                    );
                  })(),
                ],
                [t("subscriptions.detail.started"), formatDate(subscription.data.started_at, "UTC")],
                [t("subscriptions.detail.renews"), formatDate(subscription.data.renews_at, "UTC")],
                [t("subscriptions.detail.noticeDays"), <span className="mono">{subscription.data.notice_days}</span>],
                [t("subscriptions.detail.autoRenew"), subscription.data.auto_renew ? t("subscriptions.detail.yes") : t("subscriptions.detail.no")],
                [t("subscriptions.detail.paymentRef"), subscription.data.payment_ref ?? <NoValue label={t("subscriptions.detail.noPaymentRef")} />],
              ]}
            />
          </Panel>
        )}

        {subscription.data && canManage && subscription.data.status !== "cancelled" && (
          <RenewalTermsPanel subscription={subscription.data} ownerOptions={ownerOptions} usersLoading={users.isLoading} />
        )}

        <Panel title={t("subscriptions.detail.seatsPanel")}>
          <QueryState
            loading={seats.isLoading}
            error={seats.error}
            empty={(seats.data ?? []).length === 0}
            emptyLabel={t("subscriptions.detail.seatsEmpty")}
            emptyIcon="users"
          >
            <Table>
              <thead>
                <tr>
                  <Th>{t("subscriptions.detail.colAssignee")}</Th>
                  <Th>{t("subscriptions.detail.colEmail")}</Th>
                  <Th>{t("subscriptions.detail.colAssigned")}</Th>
                  <Th>{t("subscriptions.detail.colReleased")}</Th>
                  {canManage && <Th />}
                </tr>
              </thead>
              <tbody>
                {(seats.data ?? []).map((seat) => (
                  <tr key={seat.id}>
                    {/* A row that names a person shows the person. */}
                    <Td>
                      {seat.person_id ? (
                        <PersonLink personId={seat.person_id}>
                          <Person
                            initials={initialsOf(seat.assignee_name)}
                            index={personIndex(seat.assignee_email)}
                            name={seat.assignee_name}
                          />
                        </PersonLink>
                      ) : (
                        <Person
                          initials={initialsOf(seat.assignee_name)}
                          index={personIndex(seat.assignee_email)}
                          name={seat.assignee_name}
                        />
                      )}
                    </Td>
                    <Td>{seat.assignee_email}</Td>
                    {/* Instants -- `assigned_at`/`released_at` are
                        `datetime` in schema.py -- so these DO take the
                        operator's zone. `formatDateTime` returns NO_DATE for
                        a seat still held, so the `? :` goes with it. */}
                    <Td>{formatDateTime(seat.assigned_at, zone)}</Td>
                    <Td>{formatDateTime(seat.released_at, zone)}</Td>
                    {canManage && (
                      <Td>
                        {seat.released_at == null && (
                          /* A ROW CONTROL IS A CONTROL, NOT A LINK. `ghost`
                             paints as bare accent text, which is the shape a
                             link has; every other row action in this wave --
                             Edit/Delete on /sla, Tests on /bcp, Open on /ipam
                             -- is the bordered `outline` button, and this was
                             the one list that read differently. */
                          <Button
                            variant="outline"
                            disabled={releaseSeat.isPending}
                            onClick={() =>
                              setReleasing({ id: seat.id, name: seat.assignee_name })
                            }
                          >
                            {t("subscriptions.detail.release")}
                          </Button>
                        )}
                      </Td>
                    )}
                  </tr>
                ))}
              </tbody>
            </Table>
          </QueryState>

          {canManage && (
            <div style={{ marginTop: 16 }}>
              {/* Form does not forward `style`; the gap above it is the
                  wrapper's, in the kit's 16px step. */}
              {/* STACKED, NOT IN THE DIALOG GRID. `.form-grid` is two fixed
                  columns with no reflow rule, and it is the kit's DIALOG field
                  container -- in a dialog it never gets narrower than the
                  dialog. Sitting in a panel it inherits the page's width, so at
                  390 these two boxes were about 125px each and an address in
                  "Assignee email" could not be read back. Stacked, each field
                  takes the kit's own 560px field measure at 1440 and the full
                  column on a handset, which is what every other form in a panel
                  on this route does (Renewal terms above). The two-column grid
                  still needs its narrow-viewport rule for the dialogs that use
                  it, and the kit has it now -- `.form-grid` folds at 640px.
                  This panel stays stacked at every width, which is a different
                  claim: two boxes side by side in a PANEL are half a page
                  wide, not half a dialog. */}
              <Form onSubmit={assign}>
                <Field label={t("subscriptions.detail.assigneeName")}>
                  <input
                    className={inputClass}
                    value={assigneeName}
                    onChange={(e) => setAssigneeName(e.target.value)}
                  />
                </Field>
                <Field label={t("subscriptions.detail.assigneeEmail")}>
                  <input
                    className={inputClass}
                    value={assigneeEmail}
                    onChange={(e) => setAssigneeEmail(e.target.value)}
                  />
                </Field>
                {/* Enabled with the boxes empty ON PURPOSE, and the failure
                    goes through `describeSubmitError` in a `role="alert"`. */}
                <FormActions
                  label={t("subscriptions.detail.assignSeat")}
                  pendingLabel={t("subscriptions.detail.assigning")}
                  pending={assignSeat.isPending}
                  error={assignSeat.error}
                  problems={attemptProblems(seatAttempt)}
                />
              </Form>
            </div>
          )}
          {releaseSeat.isError && (
            <p role="alert" className="notice red">
              <Icon name="circle-x" size={16} />
              <span>{describeSubmitError(releaseSeat.error)}</span>
            </p>
          )}
        </Panel>

        <Panel title={t("subscriptions.detail.events")}>
          <QueryState
            loading={events.isLoading}
            error={events.error}
            empty={(events.data ?? []).length === 0}
            emptyLabel={t("subscriptions.detail.eventsEmpty")}
            emptyIcon="history"
          >
            {/* The kit's mini-list: a glyph, the event, the instant under it.
                `created_at` is a `datetime`: an instant, in the operator's
                zone and the product's one format. */}
            <ul className="mini-list" style={{ listStyle: "none", margin: 0, padding: 0 }}>
              {(events.data ?? []).map((event) => {
                const glyph = eventGlyph(event.type);
                return (
                  <li key={event.id} className="mini-row">
                    <span className={`mini-icon ${glyph.tone}`.trim()} aria-hidden="true">
                      <Icon name={glyph.icon} size={16} />
                    </span>
                    <div className="mini-copy">
                      <div className="mini-title">{humaniseEnum(event.type, "SubscriptionEventType")}</div>
                      <div className="mini-sub">{formatDateTime(event.created_at, zone)}</div>
                    </div>
                  </li>
                );
              })}
            </ul>
          </QueryState>
        </Panel>
      </QueryState>
    </div>
  );
}

/**
 * The renewal terms, editable (cycle 7).
 *
 * Auditor B, [HIGH]: "There is no renewal date, no term end, no notice period
 * and no owner field ... Auto-renewing SaaS contracts lapsing unnoticed is
 * precisely the business risk this module exists to prevent." Adding those
 * fields to the create form fixes tomorrow's subscriptions and nothing already
 * recorded, so the edit belongs here too --
 * `PATCH /subscriptions/{subscription_id}` already served it.
 *
 * DELIBERATELY NOT A FULL EDIT FORM. Cost, currency, billing cycle and seats
 * are left out: changing them re-values spend roll-ups that this panel would
 * have to explain, and the finding is specifically about the deadline inputs.
 * A narrower control that says what it does beats a wide one that invites an
 * edit nobody meant.
 */
function RenewalTermsPanel({
  subscription,
  ownerOptions,
  usersLoading,
}: {
  subscription: SubscriptionOut;
  ownerOptions: readonly PickerOption[];
  usersLoading: boolean;
}) {
  const t = useT();
  const update = useUpdateSubscriptionMutation();

  const [renewsAt, setRenewsAt] = useState(subscription.renews_at ?? "");
  const [noticeDaysValue, setNoticeDaysValue] = useState(String(subscription.notice_days));
  const [autoRenew, setAutoRenew] = useState(subscription.auto_renew);
  const [ownerUserId, setOwnerUserId] = useState(subscription.owner_user_id ?? "");
  // Populated ON SUBMIT, never while typing; held as data and worded at render.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  function submit() {
    const next: Attempt = { rules: renewalRules, values: { notice_days: noticeDaysValue } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;

    update.mutate({
      id: subscription.id,
      body: {
        // Cleared means "not known" and must travel as null, not as "".
        renews_at: renewsAt || null,
        notice_days: Number(noticeDaysValue),
        auto_renew: autoRenew,
        owner_user_id: ownerUserId || null,
      },
    });
  }

  return (
    <Panel title={t("subscriptions.detail.termsTitle")}>
      {/* ONE MEASURE FOR THE WHOLE FORM. The kit caps a `.field` at 560px, so
          "Renews on" and "Notice period" sat at that width while `EntityPicker`
          -- which carries no cap -- ran the full 1078px of the panel, and the
          panel's right edge was ragged across four rows. Nothing about an
          owner's name needs twice the measure of a date, so the form is held at
          the field measure and the picker comes in with it. Below 640 the kit
          drops the `.field` cap and this one goes with it, since `max-width` on
          the wrapper cannot make anything wider than its column. `Form` does not
          forward `style`; hence the wrapper rather than a prop. */}
      <Form onSubmit={submit}>
        <div style={{ maxWidth: 560 }}>
          <Field label={t("subscriptions.detail.renewsOn")}>
            <input
              type="date"
              className={inputClass}
              value={renewsAt}
              onChange={(e) => setRenewsAt(e.target.value)}
            />
          </Field>
          <Field label={t("subscriptions.common.noticePeriodDays")}>
            <input
              type="number"
              min={0}
              max={365}
              step={1}
              className={inputClass}
              value={noticeDaysValue}
              onChange={(e) => setNoticeDaysValue(e.target.value)}
            />
          </Field>
          {/* THE DATE, READ BACK IN THE CONSOLE'S OWN FORMAT -- the same echo
              `agent-push/PushAuthorizationsPage` draws under its expiry field,
              made consistent across every date input in the console (design
              review M1).

              WHY AN ECHO AND NOT A DIFFERENT CONTROL. A native date input's
              mask is drawn by the user agent from ITS locale, and no CSS
              reorders it: /settings can say "Day/month/year" and this field
              will still read `09/22/2026` to a US-locale browser while the row
              above it renders `22 Sept 2026`. Replacing the control with a
              text field that parses one chosen format was considered and
              refused -- the native input is the one date widget every
              keyboard, screen reader and locale already agrees on.

              UNDER THE FIELD, NOT INSIDE IT. `Field` wraps its control in the
              `<label>`, so text placed inside one becomes part of the field's
              NAME rather than its description; the accounting is at
              `agent-push/PushAuthorizationsPage`.

              "UTC", NOT THE OPERATOR'S ZONE, and it matches the read-only
              "Renews" row above: `renews_at` is a calendar DATE, not an
              instant, so rendering it through a zone would move it a day for
              anyone west of Greenwich. */}
          {renewsAt !== "" ? (
            <p className="note" style={{ marginBottom: 12 }}>
              {t("subscriptions.common.renewsOn", { date: formatDate(`${renewsAt}T00:00:00Z`, "UTC") })}
            </p>
          ) : null}
          <label className="flex items-center gap-2" style={{ marginBottom: 12 }}>
            <input
              type="checkbox"
              checked={autoRenew}
              onChange={(e) => setAutoRenew(e.target.checked)}
            />
            {t("subscriptions.common.renewsAutomatically")}
          </label>
          <EntityPicker
            label={t("subscriptions.detail.owner")}
            options={ownerOptions}
            loading={usersLoading}
            emptyHint={ownerPickerEmptyHint()}
            placeholder={t("subscriptions.common.searchPeople")}
            value={ownerUserId}
            onChange={setOwnerUserId}
          />
          <p className="note" style={{ marginBottom: 12 }}>
            {t("subscriptions.detail.termsNote")}
          </p>
          <FormActions
            label={t("subscriptions.detail.saveTerms")}
            pending={update.isPending}
            error={update.error}
            saved={update.isSuccess}
            savedMessage={t("subscriptions.detail.termsSaved")}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Panel>
  );
}
