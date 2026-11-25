import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
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
import {
  Btn,
  ClientContext,
  DetailHead,
  FormSection,
  Panel,
  QueryState,
  SaveBar,
  StatsGrid,
  UnsavedGuard,
  useDraft,
} from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useRecordTitle } from "../../shell/title";
import { KV, Person, Tile } from "../../shell/viz";
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
//
// ONE RECORD PER MOUNT (the coordinator's ruling 12): going from one
// subscription to another keeps this route mounted, and a record already read
// draws at once, so an unsaved terms draft or a half-typed assignee would
// follow to the next record and Save would write it there. Keyed by the id,
// the next record starts from its own values.
export function SubscriptionDetail() {
  const { subscriptionId } = useParams<{ subscriptionId: string }>();
  return <SubscriptionRecord key={subscriptionId} />;
}

function SubscriptionRecord() {
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

  // The tab names the record (Receta F): "<Service> · Subscriptions · Novieri RMM".
  useRecordTitle(subscription.data?.service_name);
  // The seats held now, from the seats listed below (`null` until they are read).
  const heldSeats = seats.data ? seats.data.filter((seat) => seat.released_at == null).length : null;

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
        {/* ON THE RECORD KIT (redesign plan, Receta F; Task 10, 5.4): the way
            back to the registry, the service as the title, its status as the
            head's pill, and its context: the billing cadence and the client
            (linked to its 360 page). The actions are today's.

            THE CADENCE IS STILL NOT A STATUS (design review M14): it is an
            attribute of the record, drawn as a `.chip` -- what the ticket page
            draws its type and queue as -- in the head's context, never a
            second pill and never in the button row. "Billed monthly" rather
            than "Monthly", because a bare cadence next to a status still reads
            like one. */}
        <DetailHead
          back={{ to: "/subscriptions", label: t("subscriptions.detail.back") }}
          title={subscription.data?.service_name ?? t("subscriptions.detail.titleFallback")}
          status={
            subscription.data
              ? {
                  kind: statusKind(subscription.data.status),
                  label: humaniseEnum(subscription.data.status, "SubStatus"),
                }
              : undefined
          }
          context={
            subscription.data ? (
              <>
                <span className="chip">{billingCyclePhrase(subscription.data.billing_cycle)}</span>{" "}
                <ClientContext clientId={subscription.data.client_id} />
              </>
            ) : undefined
          }
          actions={
            canManage ? (
              <>
                <Btn
                  label={renew.isPending ? t("subscriptions.detail.recording") : t("subscriptions.detail.recordRenewal")}
                  icon="repeat"
                  disabled={renew.isPending}
                  onClick={() => setConfirmingRenew(true)}
                />
                <Btn
                  label={cancel.isPending ? t("subscriptions.detail.cancelling") : t("subscriptions.detail.cancel")}
                  icon="circle-x"
                  variant="danger"
                  disabled={cancel.isPending}
                  onClick={() => setConfirmingCancel(true)}
                />
              </>
            ) : undefined
          }
        />

        {/* THE THREE FIGURES A RENEWAL IS DECIDED ON (Task 10, 5.4): what it
            costs per cycle, its seats against the ones held, and when it
            renews. Not links: they are this record's own, and the seats are
            the table below. Details no longer repeats them (the coordinator's
            answer on the client 360: Details keeps what is nowhere else). */}
        {subscription.data ? (
          <StatsGrid columns={3}>
            <Tile
              label={t("subscriptions.detail.tileCost")}
              value={`${subscription.data.cost_per_cycle} ${subscription.data.currency}`}
              icon="credit-card"
            />
            <Tile
              label={t("subscriptions.detail.seats")}
              value={String(subscription.data.seats)}
              icon="users"
              sub={heldSeats === null ? undefined : t("subscriptions.detail.tileHeld", { count: heldSeats })}
            />
            <Tile
              label={t("subscriptions.detail.renews")}
              value={formatDate(subscription.data.renews_at, "UTC")}
              icon="calendar-event"
              sub={
                subscription.data.renews_at
                  ? subscription.data.auto_renew
                    ? t("subscriptions.detail.tileAutoRenews")
                    : t("subscriptions.detail.tileNoAutoRenew")
                  : t("subscriptions.detail.tileNoDate")
              }
            />
          </StatsGrid>
        ) : null}

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
                [t("subscriptions.detail.noticeDays"), <span className="mono">{subscription.data.notice_days}</span>],
                [t("subscriptions.detail.autoRenew"), subscription.data.auto_renew ? t("subscriptions.detail.yes") : t("subscriptions.detail.no")],
                [t("subscriptions.detail.paymentRef"), subscription.data.payment_ref ?? <NoValue label={t("subscriptions.detail.noPaymentRef")} />],
              ]}
            />
          </Panel>
        )}

        {subscription.data && canManage && subscription.data.status !== "cancelled" && (
          <RenewalTermsForm subscription={subscription.data} ownerOptions={ownerOptions} usersLoading={users.isLoading} />
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
function RenewalTermsForm({
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

  // ON THE FORM KIT (Receta FO; Task 10, 5.4): the four terms are a draft of
  // what the server holds, the page's one save bar says when they changed,
  // and leaving with a change asks first. The button that saved them is gone:
  // the bar's Save (and Enter in a field) does what it did.
  const saved = useMemo<Terms>(
    () => ({
      renews_at: subscription.renews_at ?? "",
      notice_days: String(subscription.notice_days),
      auto_renew: subscription.auto_renew,
      owner_user_id: subscription.owner_user_id ?? "",
    }),
    [subscription.renews_at, subscription.notice_days, subscription.auto_renew, subscription.owner_user_id],
  );
  const { draft, setDraft, dirty, discard, markSent } = useDraft(saved);
  const terms = draft ?? saved;
  const edit = (patch: Partial<Terms>) => setDraft({ ...terms, ...patch });
  // Populated ON SUBMIT, never while typing; held as data and worded at render.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  function submit() {
    // Nothing changed, nothing to write (the coordinator's ruling 13): an
    // unchanged PATCH would leave a false entry in the audit log.
    if (!dirty) return;
    const next: Attempt = { rules: renewalRules, values: { notice_days: terms.notice_days } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;

    markSent(terms);
    update.mutate(
      {
        id: subscription.id,
        body: {
          // Cleared means "not known" and must travel as null, not as "".
          renews_at: terms.renews_at || null,
          notice_days: Number(terms.notice_days),
          auto_renew: terms.auto_renew,
          owner_user_id: terms.owner_user_id || null,
        },
      },
      // The save failed: the change is still unsaved, and a refetch must not replace it.
      { onError: () => markSent(null) },
    );
  }

  return (
    <Form onSubmit={submit}>
      <UnsavedGuard dirty={dirty} />
      <FormSection title={t("subscriptions.detail.termsTitle")} icon="calendar-event">
      {/* ONE MEASURE FOR THE WHOLE FORM. The kit caps a `.field` at 560px, so
          "Renews on" and "Notice period" sat at that width while `EntityPicker`
          -- which carries no cap -- ran the full 1078px of the panel, and the
          panel's right edge was ragged across four rows. Nothing about an
          owner's name needs twice the measure of a date, so the form is held at
          the field measure and the picker comes in with it. Below 640 the kit
          drops the `.field` cap and this one goes with it, since `max-width` on
          the wrapper cannot make anything wider than its column. `Form` does not
          forward `style`; hence the wrapper rather than a prop. */}
        <div style={{ maxWidth: 560 }}>
          <Field label={t("subscriptions.detail.renewsOn")}>
            <input
              type="date"
              className={inputClass}
              value={terms.renews_at}
              onChange={(e) => edit({ renews_at: e.target.value })}
            />
          </Field>
          <Field label={t("subscriptions.common.noticePeriodDays")}>
            <input
              type="number"
              min={0}
              max={365}
              step={1}
              className={inputClass}
              value={terms.notice_days}
              onChange={(e) => edit({ notice_days: e.target.value })}
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
          {terms.renews_at !== "" ? (
            <p className="note" style={{ marginBottom: 12 }}>
              {t("subscriptions.common.renewsOn", { date: formatDate(`${terms.renews_at}T00:00:00Z`, "UTC") })}
            </p>
          ) : null}
          <label className="flex items-center gap-2" style={{ marginBottom: 12 }}>
            <input
              type="checkbox"
              checked={terms.auto_renew}
              onChange={(e) => edit({ auto_renew: e.target.checked })}
            />
            {t("subscriptions.common.renewsAutomatically")}
          </label>
          <EntityPicker
            label={t("subscriptions.detail.owner")}
            options={ownerOptions}
            loading={usersLoading}
            emptyHint={ownerPickerEmptyHint()}
            placeholder={t("subscriptions.common.searchPeople")}
            value={terms.owner_user_id}
            onChange={(value) => edit({ owner_user_id: value })}
          />
          <p className="note" style={{ marginBottom: 12 }}>
            {t("subscriptions.detail.termsNote")}
          </p>
        </div>
      </FormSection>
      {/* The page's one save bar, inside the form so Enter in a field saves:
          "Unsaved changes · Discard · Save", and "Renewal terms saved." once
          the server answered (never an optimistic one). */}
      <SaveBar
        dirty={dirty}
        saving={update.isPending}
        error={update.error}
        submit
        saved={update.isSuccess && !dirty}
        savedMessage={t("subscriptions.detail.termsSaved")}
        problems={attemptProblems(attempt)}
        onDiscard={() => {
          discard();
          setAttempt(null);
          update.reset();
        }}
      />
    </Form>
  );
}

/** The renewal terms as the form edits them: the four fields, as their inputs hold them. */
interface Terms {
  renews_at: string;
  notice_days: string;
  auto_renew: boolean;
  owner_user_id: string;
}
