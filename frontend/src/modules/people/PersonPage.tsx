import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ConfirmDialog } from "../../components/confirm";
import { NoValue } from "../../components/emptystate";
import { describeSubmitError, Form, FormActions } from "../../components/form";
import { Button, Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { Trans } from "../../i18n/Trans";
import { formatDate, formatDateTime } from "../../lib/datetime";
import { humaniseStatus } from "../../lib/labels";
import { roleChipName } from "../settings/types";
import { ticketStatusLabel } from "../service-desk/serviceDeskState";
import { PageHead, Panel, QueryState, StatusPill } from "../../shell/components";
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

  if (!personId) return null;
  const data = person.data;

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
            <PageHead
              title={data.person.display_name}
              description={[data.person.job_title, data.person.department, data.person.email]
                .filter(Boolean)
                .join(" · ")}
              actions={
                <>
                  {/* No "All people": that list left on 2026-09-23 and its
                      URL redirects to Devices, which the crumb already names. */}
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
            <div className="flex flex-wrap items-center gap-2" style={{ margin: "-8px 0 14px" }}>
              <StatusPill
                value={data.person.status}
                label={STATUS_LABEL[data.person.status]}
                kind={STATUS_KIND[data.person.status]}
              />
            </div>

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

            <Holdings data={data} zone={zone} />

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

            {data.past_checklists.length > 0 ? (
              <Panel title={t("people.person.past.title")}>
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
              </Panel>
            ) : null}

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

/** Sign-in, computers, licences, subscriptions and tickets. */
function Holdings({ data, zone }: { data: PersonDetailOut; zone: string | null | undefined }) {
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

  return (
    <>
      {failure ? (
        <p role="alert" className="form-error">
          {describeSubmitError(failure)}
        </p>
      ) : null}

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
