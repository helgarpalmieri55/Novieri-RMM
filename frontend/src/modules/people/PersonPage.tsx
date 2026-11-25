/**
 * A person: what they hold, their sign-in, their tickets and their starter
 * and leaver checklists.
 *
 * ON THE RECORD KIT (redesign Task 11, 6.7; Receta F). The head is the kit's
 * `DetailHead`: the name, the status, job title and department as context,
 * and "Edit" with the two checklists as its actions. Three tiles say what
 * they hold, each a link to the tab where it lives. The seven panels that
 * were stacked one under another are grouped in `?tab=` tabs, so a tab
 * reloads and links: computers (the default, with the details and the
 * sign-in beside them), licences and subscriptions, tickets, and history.
 * An open checklist stays above the tabs: it is the work in progress, on
 * whichever tab is open.
 *
 * The tab says "<name> · Person · Novieri RMM" (`useRecordTitle`): the page
 * lives under Devices in the rail, as `shell.nav.page.person`.
 */
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ConfirmDialog } from "../../components/confirm";
import { NoValue } from "../../components/emptystate";
import { describeSubmitError, Form, FormActions } from "../../components/form";
import { Button, Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import { Trans } from "../../i18n/Trans";
import { formatDate, formatDateTime } from "../../lib/datetime";
import { humaniseStatus } from "../../lib/labels";
import { roleChipName } from "../settings/types";
import { ticketStatusLabel } from "../service-desk/serviceDeskState";
import {
  DetailGrid,
  DetailHead,
  Panel,
  QueryState,
  StatCard,
  StatsGrid,
  UrlTabs,
  useUrlTab,
} from "../../shell/components";
import { useRecordTitle } from "../../shell/title";
import { KV } from "../../shell/viz";
import {
  useCan,
  useMe,
  usePerson,
  useReleaseLicence,
  useReleaseSeat,
  useStartChecklist,
  useTurnOffSignIn,
  useUpdatePerson,
} from "./api";
import { ChecklistPanel } from "./ChecklistPanel";
import {
  CHECKLIST_LABEL,
  STATUS_KIND,
  STATUS_LABEL,
  accountStatusLabel,
  lifecycleLabel,
  linkSourceLabel,
} from "./labels";
import type { AccountOut, ChecklistKind, PersonDetailOut, PersonOut } from "./types";
import { useEverywherePermissions } from "../../lib/mePermissions";

/** What a destructive button is about to do, for the confirmation. */
type Pending =
  | { kind: "seat"; id: string; name: string }
  | { kind: "licence"; id: string; name: string }
  | { kind: "sign-in"; id: string; name: string; account: AccountOut["kind"] };

/** The record's tabs; computers are the default and write nothing to the URL. */
const TABS = ["equipment", "licenses", "tickets", "history"] as const;
type Tab = (typeof TABS)[number];

const TAB_LABEL: Record<Tab, MessageKey> = {
  equipment: "people.person.tabs.equipment",
  licenses: "people.person.tabs.licenses",
  tickets: "people.person.tabs.tickets",
  history: "people.person.tabs.history",
};

export function PersonPage() {
  const t = useT();
  const { personId } = useParams<{ personId: string }>();
  const person = usePerson(personId);
  const me = useMe();
  const zone = me.data?.timezone;
  const canManage = useCan("people.manage");
  const canOffboard = useCan("people.offboard");
  const [editing, setEditing] = useState(false);
  const [starting, setStarting] = useState<ChecklistKind | null>(null);
  const start = useStartChecklist(personId ?? "");
  const [tab, setTab] = useUrlTab<Tab>(TABS, "equipment");
  const data = person.data;
  useRecordTitle(data?.person.display_name);

  if (!personId) return null;
  const here = `/people/${personId}`;

  return (
    <div>
      <QueryState
        loading={person.isLoading}
        error={person.error}
        empty={!person.isLoading && !person.error && data == null}
        emptyLabel={t("people.person.notFound")}
      >
        {data ? (
          <>
            <DetailHead
              // No "All people": that list left on 2026-09-23 and its URL
              // redirects to Devices, which is where the rail keeps this page.
              back={{ to: "/devices", label: t("people.person.back") }}
              title={data.person.display_name}
              status={{ kind: STATUS_KIND[data.person.status], label: STATUS_LABEL[data.person.status] }}
              context={[data.person.job_title, data.person.department].filter(Boolean).join(" · ") || undefined}
              actions={
                <>
                  {canManage ? (
                    <Button variant="outline" onClick={() => setEditing(true)}>
                      {t("people.person.edit")}
                    </Button>
                  ) : null}
                  {canManage && data.checklist == null && data.person.status !== "left" ? (
                    <Button variant="outline" onClick={() => setStarting("starter")}>
                      {t("people.person.startStarter")}
                    </Button>
                  ) : null}
                  {canManage && canOffboard && data.checklist == null && data.person.status !== "left" ? (
                    <Button variant="outline" onClick={() => setStarting("leaver")}>
                      {t("people.person.startLeaver")}
                    </Button>
                  ) : null}
                </>
              }
            />

            <HoldingTiles data={data} here={here} />

            {start.isError && (
              <p role="alert" className="form-error">
                {describeSubmitError(start.error)}
              </p>
            )}

            {data.checklist ? (
              <ChecklistPanel
                personId={personId}
                personName={data.person.display_name}
                checklist={data.checklist}
                canEdit={canManage && (data.checklist.kind === "starter" || canOffboard)}
              />
            ) : null}

            <UrlTabs
              label={t("people.person.tabs.label")}
              items={TABS.map((key) => ({ key, label: t(TAB_LABEL[key]) }))}
              active={tab}
              onSelect={setTab}
            >
              <Holdings data={data} zone={zone} tab={tab} />
            </UrlTabs>

            {/* Mounted only while open, so the form starts from the saved values. */}
            {editing ? (
              <EditPersonDialog open person={data.person} onClose={() => setEditing(false)} />
            ) : null}
            <ConfirmDialog
              open={starting != null}
              title={t(starting === "leaver" ? "people.person.startLeaverTitle" : "people.person.startStarterTitle")}
              subject={data.person.display_name}
              body={t(starting === "leaver" ? "people.person.startLeaverBody" : "people.person.startStarterBody")}
              confirmLabel={t("people.person.start")}
              onCancel={() => setStarting(null)}
              onConfirm={() => {
                const kind = starting;
                setStarting(null);
                if (kind) start.mutate(kind);
              }}
            />
          </>
        ) : null}
      </QueryState>
    </div>
  );
}

/**
 * What they hold, as three numbers that each lead to the tab listing them. A
 * section the person may not see (`null` from the server) has no tile: its
 * count is not known, and a zero would be invented (the Absent Zero Rule).
 */
function HoldingTiles({ data, here }: { data: PersonDetailOut; here: string }) {
  const t = useT();
  return (
    <StatsGrid columns={3}>
      <StatCard label={t("people.person.tiles.computers")} value={String(data.devices.length)} icon="devices" to={here} />
      {data.licences != null ? (
        <StatCard
          label={t("people.person.tiles.licences")}
          value={String(data.licences.length)}
          icon="license"
          to={`${here}?tab=licenses`}
        />
      ) : null}
      {data.seats != null ? (
        <StatCard
          label={t("people.person.tiles.seats")}
          value={String(data.seats.length)}
          icon="subscription"
          to={`${here}?tab=licenses`}
        />
      ) : null}
    </StatsGrid>
  );
}

/** The details of the person: who they are, beside what they hold. */
function DetailsPanel({ data }: { data: PersonDetailOut }) {
  const t = useT();
  return (
    <Panel title={t("people.person.details.title")}>
      <KV
        items={[
          [t("people.person.details.email"), data.person.email ?? <NoValue label={t("people.person.details.noEmail")} />],
          [
            t("people.person.details.department"),
            data.person.department ?? <NoValue label={t("people.person.details.notRecorded")} />,
          ],
          [
            t("people.person.details.jobTitle"),
            data.person.job_title ?? <NoValue label={t("people.person.details.notRecorded")} />,
          ],
          [t("people.person.details.started"), formatDate(data.person.start_date, "UTC")],
          [t("people.person.details.left"), formatDate(data.person.end_date, "UTC")],
          [
            t("people.person.details.signsInAs"),
            data.logins.length ? (
              <span className="mono">{data.logins.map((l) => l.login).join(", ")}</span>
            ) : (
              <NoValue label={t("people.person.details.loginsLearned")} />
            ),
          ],
        ]}
      />
    </Panel>
  );
}

/** The checklists that have finished or been cancelled. */
function PastChecklists({ data, zone }: { data: PersonDetailOut; zone: string | null | undefined }) {
  const t = useT();
  return (
    <Panel title={t("people.person.past.title")}>
      {data.past_checklists.length === 0 ? (
        <p className="note">{t("people.person.past.none")}</p>
      ) : (
        <ul>
          {data.past_checklists.map((c) => (
            <li key={c.id}>
              {t(c.status === "completed" ? "people.person.past.finished" : "people.person.past.cancelled", {
                kind: CHECKLIST_LABEL[c.kind as ChecklistKind] ?? humaniseStatus(c.kind),
                date: formatDateTime(c.closed_at, zone),
              })}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

/** Sign-in, computers, licences, subscriptions and tickets. */
/**
 * Sign-in, computers, licences, subscriptions and tickets, on the tab that
 * shows them. One component for every tab, so the release and turn-off
 * confirmations and their failures are the same wherever they start.
 */
function Holdings({ data, zone, tab }: { data: PersonDetailOut; zone: string | null | undefined; tab: Tab }) {
  const t = useT();
  const personId = data.person.id;
  // Staff are switched off in Settings, through a route of configuration every
  // client shares (`POST /users/{id}/disable`): `users.manage` on EVERY client
  // (phase 3 design §2). A client's contact is switched off as a contact.
  const everywhere = useEverywherePermissions();
  const canTurnOffContact = useCan("clients.manage");
  const canTurnOff =
    data.account?.kind === "contact" ? canTurnOffContact : everywhere.has("users.manage");
  const canLicences = useCan("licenses.manage");
  const canSeats = useCan("subscriptions.manage");
  const releaseSeat = useReleaseSeat(personId);
  const releaseLicence = useReleaseLicence(personId);
  const turnOff = useTurnOffSignIn(personId);
  const [pending, setPending] = useState<Pending | null>(null);
  const failure = releaseSeat.error ?? releaseLicence.error ?? turnOff.error;

  const signIn = (
    <Panel
      title={t("people.holdings.signIn.title")}
      actions={
        canTurnOff && data.account?.status === "active" ? (
          <Button
            variant="outline"
            onClick={() =>
              data.account &&
              setPending({
                kind: "sign-in",
                id: data.account.user_id,
                name: data.account.email,
                account: data.account.kind,
              })
            }
          >
            {t("people.holdings.signIn.turnOff")}
          </Button>
        ) : undefined
      }
    >
      {data.account ? (
        <KV
          items={[
            [t("people.holdings.signIn.account"), data.account.email],
            [t("people.holdings.signIn.status"), accountStatusLabel(data.account.status)],
            [t("people.holdings.signIn.lastSignedIn"), formatDateTime(data.account.last_login_at, zone)],
            [
              t("people.holdings.signIn.uses"),
              t(data.account.console ? "people.holdings.signIn.usesBoth" : "people.holdings.signIn.usesPortal"),
            ],
            ...(data.account.roles
              ? ([
                  [t("people.holdings.signIn.roles"), data.account.roles.map(roleChipName).join(", ") || t("people.holdings.signIn.noRoles")],
                ] as const)
              : []),
          ]}
        />
      ) : (
        <p className="note">
          <Trans
            k="people.holdings.signIn.none"
            components={{ link: <Link className="underline" to="/settings" /> }}
          />
        </p>
      )}
    </Panel>
  );
  const computers = (
    <Panel title={t("people.holdings.computers.title")} bare>
      {data.devices.length === 0 ? (
        <p className="note" style={{ padding: 16 }}>
          {t("people.holdings.computers.empty")}
        </p>
      ) : (
        <Table label={t("people.holdings.computers.title")}>
          <thead>
            <tr>
              <Th>{t("people.holdings.computers.computer")}</Th>
              <Th>{t("people.holdings.computers.state")}</Th>
              <Th>{t("people.holdings.computers.group")}</Th>
              <Th>{t("people.holdings.computers.howWeKnow")}</Th>
            </tr>
          </thead>
          <tbody>
            {data.devices.map((device) => (
              <tr key={device.device_id}>
                <Td>
                  <Link to={`/devices/${device.device_id}`}>{device.hostname ?? t("people.holdings.computers.unnamed")}</Link>
                </Td>
                <Td>{lifecycleLabel(device.lifecycle)}</Td>
                <Td>{device.group_name ?? <NoValue label={t("people.holdings.computers.noGroup")} />}</Td>
                <Td>{linkSourceLabel(device.source)}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Panel>
  );
  const licences = (
    <Panel title={t("people.holdings.licences.title")} bare>
      {data.licences == null ? (
        <p className="note" style={{ padding: 16 }}>{t("people.holdings.licences.hidden")}</p>
      ) : data.licences.length === 0 ? (
        <p className="note" style={{ padding: 16 }}>{t("people.holdings.licences.empty")}</p>
      ) : (
        <Table label={t("people.holdings.licences.title")}>
          <thead>
            <tr>
              <Th>{t("people.holdings.licences.product")}</Th>
              <Th>{t("people.holdings.licences.held")}</Th>
              <Th>{t("people.holdings.licences.since")}</Th>
              <Th>{canLicences ? t("people.holdings.licences.takeBack") : ""}</Th>
            </tr>
          </thead>
          <tbody>
            {data.licences.map((licence) => (
              <tr key={licence.allocation_id}>
                <Td>
                  <Link to={`/licenses/${licence.license_id}`}>{licence.product_name}</Link>
                </Td>
                <Td>{t(licence.through_device_id ? "people.holdings.licences.throughComputer" : "people.holdings.licences.byThem")}</Td>
                <Td>{formatDateTime(licence.allocated_at, zone)}</Td>
                <Td>
                  {canLicences ? (
                    <Button
                      variant="ghost"
                      onClick={() =>
                        setPending({
                          kind: "licence",
                          id: licence.allocation_id,
                          name: licence.product_name,
                        })
                      }
                    >
                      {t("people.holdings.licences.release")}
                    </Button>
                  ) : null}
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Panel>
  );
  const subscriptions = (
    <Panel title={t("people.holdings.subscriptions.title")} bare>
      {data.seats == null ? (
        <p className="note" style={{ padding: 16 }}>{t("people.holdings.subscriptions.hidden")}</p>
      ) : data.seats.length === 0 ? (
        <p className="note" style={{ padding: 16 }}>{t("people.holdings.subscriptions.empty")}</p>
      ) : (
        <Table label={t("people.holdings.subscriptions.title")}>
          <thead>
            <tr>
              <Th>{t("people.holdings.subscriptions.service")}</Th>
              <Th>{t("people.holdings.subscriptions.plan")}</Th>
              <Th>{t("people.holdings.subscriptions.since")}</Th>
              <Th>{canSeats ? t("people.holdings.subscriptions.takeBack") : ""}</Th>
            </tr>
          </thead>
          <tbody>
            {data.seats.map((seat) => (
              <tr key={seat.seat_id}>
                <Td>
                  <Link to={`/subscriptions/${seat.subscription_id}`}>{seat.service_name}</Link>
                </Td>
                <Td>{seat.plan || <NoValue label={t("people.holdings.subscriptions.noPlan")} />}</Td>
                <Td>{formatDateTime(seat.assigned_at, zone)}</Td>
                <Td>
                  {canSeats ? (
                    <Button
                      variant="ghost"
                      onClick={() =>
                        setPending({ kind: "seat", id: seat.seat_id, name: seat.service_name })
                      }
                    >
                      {t("people.holdings.subscriptions.release")}
                    </Button>
                  ) : null}
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Panel>
  );
  const tickets = (
    <Panel title={t("people.holdings.tickets.title")} bare>
      {data.tickets == null ? (
        <p className="note" style={{ padding: 16 }}>{t("people.holdings.tickets.hidden")}</p>
      ) : data.tickets.length === 0 ? (
        <p className="note" style={{ padding: 16 }}>{t("people.holdings.tickets.empty")}</p>
      ) : (
        <Table label={t("people.holdings.tickets.title")}>
          <thead>
            <tr>
              <Th>{t("people.holdings.tickets.ticket")}</Th>
              <Th>{t("people.holdings.tickets.subject")}</Th>
              <Th>{t("people.holdings.tickets.status")}</Th>
              <Th>{t("people.holdings.tickets.raised")}</Th>
            </tr>
          </thead>
          <tbody>
            {data.tickets.map((ticket) => (
              <tr key={ticket.ticket_id}>
                <Td>
                  <Link to={`/service-desk/tickets/${ticket.ticket_id}`}>{ticket.number}</Link>
                </Td>
                <Td>{ticket.subject}</Td>
                <Td>{ticketStatusLabel(ticket.status)}</Td>
                <Td>{formatDateTime(ticket.created_at, zone)}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Panel>
  );

  return (
    <>
      {failure ? (
        <p role="alert" className="form-error">
          {describeSubmitError(failure)}
        </p>
      ) : null}

      {tab === "equipment" ? (
        <DetailGrid
          main={computers}
          aside={
            <>
              <DetailsPanel data={data} />
              {signIn}
            </>
          }
        />
      ) : tab === "licenses" ? (
        <>
          {licences}
          {subscriptions}
        </>
      ) : tab === "tickets" ? (
        tickets
      ) : (
        <PastChecklists data={data} zone={zone} />
      )}

      <ConfirmDialog
        open={pending != null}
        title={
          pending?.kind === "sign-in"
            ? t("people.holdings.confirm.turnOffTitle")
            : pending?.kind === "seat"
              ? t("people.holdings.confirm.releaseSeatTitle")
              : t("people.holdings.confirm.releaseLicenceTitle")
        }
        subject={pending ? `${data.person.display_name}: ${pending.name}` : ""}
        body={
          pending?.kind === "sign-in"
            ? t(
                pending.account === "contact"
                  ? "people.holdings.confirm.turnOffBodyContact"
                  : "people.holdings.confirm.turnOffBody",
              )
            : t("people.holdings.confirm.releaseBody")
        }
        confirmLabel={
          pending?.kind === "sign-in"
            ? t("people.holdings.confirm.turnOff")
            : t("people.holdings.confirm.release")
        }
        destructive
        onCancel={() => setPending(null)}
        onConfirm={() => {
          const act = pending;
          setPending(null);
          if (!act) return;
          if (act.kind === "seat") releaseSeat.mutate(act.id);
          else if (act.kind === "licence") releaseLicence.mutate(act.id);
          else turnOff.mutate({ user_id: act.id, kind: act.account });
        }}
      />
    </>
  );
}

function EditPersonDialog({
  open,
  person,
  onClose,
}: {
  open: boolean;
  person: PersonOut;
  onClose: () => void;
}) {
  const t = useT();
  const update = useUpdatePerson(person.id);
  const [name, setName] = useState(person.display_name);
  const [email, setEmail] = useState(person.email ?? "");
  const [department, setDepartment] = useState(person.department ?? "");
  const [jobTitle, setJobTitle] = useState(person.job_title ?? "");
  const [startDate, setStartDate] = useState(person.start_date ?? "");
  const [endDate, setEndDate] = useState(person.end_date ?? "");

  return (
    <Dialog open={open} onClose={onClose} title={t("people.edit.title")}>
      <Form
        onSubmit={() =>
          update.mutate(
            {
              display_name: name.trim(),
              email: email.trim() || null,
              department: department.trim() || null,
              job_title: jobTitle.trim() || null,
              start_date: startDate || null,
              end_date: endDate || null,
            },
            { onSuccess: onClose },
          )
        }
      >
        <div className="form-grid">
          <Field label={t("people.edit.name")}>
            <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label={t("people.edit.email")}>
            <input className={inputClass} type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
          </Field>
          <Field label={t("people.edit.department")}>
            <input className={inputClass} value={department} onChange={(e) => setDepartment(e.target.value)} />
          </Field>
          <Field label={t("people.edit.jobTitle")}>
            <input className={inputClass} value={jobTitle} onChange={(e) => setJobTitle(e.target.value)} />
          </Field>
          <Field label={t("people.edit.startDate")}>
            <input className={inputClass} type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
          </Field>
          <Field label={t("people.edit.leavingDate")}>
            <input className={inputClass} type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
          </Field>
        </div>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("people.edit.cancel")}
          </Button>
          <FormActions label={t("people.edit.save")} pending={update.isPending} error={update.error} />
        </div>
      </Form>
    </Dialog>
  );
}
