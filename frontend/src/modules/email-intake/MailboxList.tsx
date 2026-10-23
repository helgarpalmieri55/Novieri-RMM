import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { RelTime } from "../../components/reltime";
import { ConfirmDialog } from "../../components/confirm";
import { describeSubmitError } from "../../components/form";
import { NoValue } from "../../components/emptystate";
import { Table, Td, Th } from "../../components/ui";
import { RowMenu } from "../../components/rowmenu";
import { Icon } from "../../shell/icons";
import { Btn, Panel, StatusPill } from "../../shell/components";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { ApiError } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import {
  useDeleteEmailMailboxMutation,
  useDisconnectMailboxOAuthMutation,
  useEmailMailboxes,
  useStartMailboxOAuthMutation,
  useTestEmailMailboxMutation,
} from "./api";
import { browser } from "./browser";
import { MailboxForm } from "./MailboxForm";
import type { MailboxOut } from "./types";

// Spec §10's mailbox surface. Reads are `email_intake.read`-gated, every
// write AND the connection test are `email_intake.admin`-gated server-side;
// `canAdmin` here is presentation only -- the server rejects an unauthorized
// call regardless (invariant #6).
//
// `last_polled_at` AND `last_error` ARE COLUMNS, NOT A DETAIL VIEW. A poison
// message stops its own mailbox by design (poller.py: the poll aborts, the
// failing stage is recorded, the sweep moves on to the next mailbox), so
// these two fields are the ONLY signal an operator ever gets that a mailbox
// has gone quiet. Burying them behind a click is the same defect as not
// rendering them.

function transportLabel(useSsl: boolean): string {
  return translate(useSsl ? "emailIntake.mailboxes.implicitTls" : "emailIntake.mailboxes.starttls");
}

/**
 * A Microsoft 365 mailbox's sign-in, as a state with its action. An expired
 * sign-in is NOT drawn as an error: it has one fix (Reconnect), and a red
 * alert teaches the operator to ignore the row rather than press the button.
 *
 * The state is a code, not a sentence: the words are looked up when the row is
 * drawn (`stateLabel`, `actionLabel`), so a language switch re-words them.
 */
type MicrosoftAction = "connect" | "reconnect" | "disconnect";

function microsoftState(m: MailboxOut): {
  label: string;
  kind: "ok" | "warn" | "mut";
  action: MicrosoftAction;
} {
  if (!m.has_credential) {
    return { label: translate("emailIntake.mailboxes.notConnected"), kind: "mut", action: "connect" };
  }
  if (m.credential_state === "needs_reconsent") {
    return { label: translate("emailIntake.mailboxes.signInExpired"), kind: "warn", action: "reconnect" };
  }
  return { label: translate("emailIntake.mailboxes.connected"), kind: "ok", action: "disconnect" };
}

function actionLabel(action: MicrosoftAction): string {
  return translate(
    action === "connect"
      ? "emailIntake.mailboxes.connect"
      : action === "reconnect"
        ? "emailIntake.mailboxes.reconnect"
        : "emailIntake.mailboxes.disconnect",
  );
}

export function MailboxList({ canAdmin, zone }: { canAdmin: boolean; zone?: string | null }) {
  const t = useT();
  const api = useApi();
  const mailboxes = useEmailMailboxes();
  const remove = useDeleteEmailMailboxMutation();
  const test = useTestEmailMailboxMutation();
  const start = useStartMailboxOAuthMutation();
  const disconnect = useDisconnectMailboxOAuthMutation();
  const [pendingDisconnect, setPendingDisconnect] = useState<MailboxOut | null>(null);

  function connect(id: string) {
    start.mutate(id, { onSuccess: (data) => browser.assign(data.authorize_url) });
  }

  // A cross-module read (`ticket.read`), independent of this page's
  // `email_intake.read`/`admin` gate -- degrades to a free-text category-id
  // field rather than blocking the form on a permission this module does not
  // itself require (the notification module's QueueMembersPanel precedent).
  const categories = useQuery({
    queryKey: ["email-intake-ticket-categories"],
    queryFn: () => api.listTicketCategories(),
    retry: false,
  });
  const categoriesForbidden =
    categories.error instanceof ApiError && categories.error.status === 403;

  // `null` = closed. A non-null value MOUNTS the form, so each open starts
  // from fresh state and a password typed into an abandoned dialog cannot
  // survive into the next one.
  const [form, setForm] = useState<{ mailbox: MailboxOut | null } | null>(null);
  const [testedId, setTestedId] = useState<string | null>(null);
  // The row awaiting confirmation. THE ROW, not its id: the dialog has to
  // name the mailbox address, which is the only thing that lets an operator
  // notice they clicked one line too far down.
  const [pendingDelete, setPendingDelete] = useState<MailboxOut | null>(null);

  const forbidden = mailboxes.error instanceof ApiError && mailboxes.error.status === 403;
  const failed = mailboxes.error != null && !forbidden;

  function runTest(id: string) {
    setTestedId(id);
    test.mutate(id);
  }

  return (
    // Both mailbox stat tiles link to `/email-intake#mailboxes`.
    <div id="mailboxes">
      <Panel
        title={t("emailIntake.mailboxes.title")}
        subtitle={t("emailIntake.mailboxes.subtitle")}
        bare
        actions={
          canAdmin ? (
            <Btn label={t("emailIntake.mailboxes.add")} icon="plus" variant="primary" onClick={() => setForm({ mailbox: null })} />
          ) : undefined
        }
      >
        {mailboxes.isLoading ? (
          <p className="note" style={{ padding: "16px 18px" }}>{t("emailIntake.common.loading")}</p>
        ) : forbidden ? (
          <p className="note" style={{ padding: "16px 18px" }}>{t("emailIntake.mailboxes.forbidden")}</p>
        ) : failed ? (
          <p role="alert" className="notice red" style={{ margin: 14 }}>
            {t("emailIntake.mailboxes.failed", { reason: describeSubmitError(mailboxes.error) })}
          </p>
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>{t("emailIntake.mailboxes.colAddress")}</Th>
                {/* WHETHER IT IS WORKING SITS SECOND, AND THAT IS THE FIX.
                    `Active` was the third of six columns and `Last error` the
                    fifth, so at 390px both fell outside the viewport inside
                    the table's horizontal scroll and a phone showed an
                    address and a credential state -- everything except
                    whether the mailbox is polling and what stopped it. They
                    are one story, so they are now one column: the pill, and
                    under it the reason it is not working. */}
                <Th>{t("emailIntake.mailboxes.colStatus")}</Th>
                {/* Auditor A (MEDIUM): "The Mailboxes table exposes a PASSWORD
                    column ... A column headed PASSWORD in a list view is a
                    design smell regardless of its contents: it invites
                    shoulder-surfing, it will appear in screenshots and screen
                    shares, and it implies the value is retrievable." It never
                    was -- `MailboxOut` has no field that could carry one -- so
                    the header was promising something the cells could not
                    deliver. The audit's own fix, verbatim: a Credentials status
                    column. */}
                <Th>{t("emailIntake.mailboxes.colCredentials")}</Th>
                <Th>{t("emailIntake.mailboxes.colLastPolled")}</Th>
                {canAdmin && <Th>{""}</Th>}
              </tr>
            </thead>
            <tbody>
              {mailboxes.data?.map((m) => (
                <tr key={m.id}>
                  {/* v3 leads the row with the mail glyph; the server, port,
                      transport and folder sit under the address in mono. */}
                  <Td lead={<Icon name="mail" size={18} />} gutter={m.last_error ? "crit" : undefined}>
                    <div className="cell-title">{m.address}</div>
                    <div className="cell-sub mono">
                      {m.host}:{m.port} · {transportLabel(m.use_ssl)} · {m.folder}
                    </div>
                  </Td>
                  <Td>
                    {/* Humanised by the one table that humanises: the column
                        printed the wire's "active"/"inactive" in lower case in
                        a console whose every other pill is sentence case. */}
                    <StatusPill value={m.active ? "active" : "inactive"} />
                    {/* The fault, under the state it explains. As its own
                        column an absent error had a HEADER giving the em dash
                        its meaning; folded in here a bare dash on every
                        healthy row would say nothing, so it appears only
                        where there is something to say -- an error on record,
                        or a mailbox that is switched off and has none. */}
                    {m.last_error ? (
                      <div className="form-error" style={{ marginTop: 4 }}>
                        {m.last_error}
                      </div>
                    ) : !m.active ? (
                      <div style={{ marginTop: 4 }}>
                        <NoValue label={t("emailIntake.mailboxes.noErrorRecorded")} />
                      </div>
                    ) : null}
                  </Td>
                  {/* The FACT that a credential is configured, and nothing
                      more -- never the password, never `credential_id`. The
                      header stays the word `Credentials`: a column headed
                      PASSWORD implies the value is retrievable, and it never
                      was. */}
                  <Td>
                    {m.auth_kind === "microsoft_oauth" ? (
                      (() => {
                        const state = microsoftState(m);
                        return (
                          <>
                            <StatusPill value={state.kind} label={state.label} kind={state.kind} />
                            <div className="cell-sub">{t("emailIntake.mailboxes.microsoft365")}</div>
                            {canAdmin ? (
                              <div style={{ marginTop: 4 }}>
                                <Btn
                                  small
                                  variant={state.action === "disconnect" ? "ghost" : "primary"}
                                  label={actionLabel(state.action)}
                                  ariaLabel={t("emailIntake.mailboxes.actionAria", {
                                    action: actionLabel(state.action),
                                    address: m.address,
                                  })}
                                  disabled={start.isPending || disconnect.isPending}
                                  onClick={() =>
                                    state.action === "disconnect"
                                      ? setPendingDisconnect(m)
                                      : connect(m.id)
                                  }
                                />
                              </div>
                            ) : null}
                          </>
                        );
                      })()
                    ) : m.has_credential ? (
                      t("emailIntake.mailboxes.credentialSet")
                    ) : (
                      t("emailIntake.mailboxes.credentialNotSet")
                    )}
                  </Td>
                  {/* Was the raw ISO string, and `"Never polled"` for null.
                      `formatDateTime` renders it in the operator's profile zone
                      and NO_DATE ("—") when there is no timestamp -- the same
                      em dash the status cell's absent-error line uses, which
                      is the point of having one renderer. */}
                  <Td>
                    <RelTime iso={m.last_polled_at} zone={zone} />
                  </Td>
                  {canAdmin && (
                    <Td>
                      {/* ONE WEIGHT FOR THE WHOLE CLUSTER. Edit and Test were
                          borderless ghosts and Delete a full-size bordered
                          button, so the cluster read at two weights AND wrapped
                          -- Delete dropped onto a second line and every mailbox
                          row was two lines tall for its actions alone. All
                          three are `Btn small` now: the same box, and Delete
                          differs only in the ink and edge its `danger` variant
                          paints, which is the distinction the auditor asked for
                          and the only one that should be visible here. */}
                      <div className="flex flex-wrap gap-2">
                        <Btn
                          small
                          label={t("emailIntake.mailboxes.edit")}
                          ariaLabel={t("emailIntake.mailboxes.editAria", { address: m.address })}
                          onClick={() => setForm({ mailbox: m })}
                        />
                        <Btn
                          small
                          label={test.isPending && testedId === m.id ? t("emailIntake.mailboxes.testing") : t("emailIntake.mailboxes.test")}
                          ariaLabel={t("emailIntake.mailboxes.testAria", { address: m.address })}
                          onClick={() => runTest(m.id)}
                          disabled={test.isPending}
                        />
                        {/* THE FINDING THIS ROW EXISTS TO CLOSE. Auditor B:
                            "Clicking Delete ... removed the row instantly. No
                            confirm dialog, no 'are you sure', no undo ... Delete
                            sits directly beside Edit in the same row with
                            identical size and no destructive colour, so a
                            mis-click is one pixel of travel away from
                            irreversible data loss." Both halves: it no longer
                            fires on the click, and it no longer looks like Edit
                            and Test. */}
                        <RowMenu
                          subject={m.address}
                          items={[
                            {
                              label: t("emailIntake.mailboxes.delete"),
                              destructive: true,
                              disabled: remove.isPending,
                              onSelect: () => setPendingDelete(m),
                            },
                          ]}
                        />
                      </div>
                      {testedId === m.id && test.isSuccess && (
                        <p className={test.data.ok ? "form-saved" : "form-error"} style={{ marginTop: 4 }}>
                          {test.data.detail}
                        </p>
                      )}
                      {/* `String(test.error)` printed "Error: " for a bodyless
                          503: the button stopped saying "Testing…" and a label
                          with no sentence appeared, which reads as a test that
                          finished rather than one that never ran. */}
                      {testedId === m.id && test.isError && (
                        <p role="alert" className="form-error" style={{ marginTop: 4 }}>
                          {describeSubmitError(test.error)}
                        </p>
                      )}
                    </Td>
                  )}
                </tr>
              ))}
              {mailboxes.data && mailboxes.data.length === 0 && (
                <tr>
                  <Td colSpan={canAdmin ? 5 : 4}>
                    <span className="note">{t("emailIntake.mailboxes.empty")}</span>
                  </Td>
                </tr>
              )}
            </tbody>
          </Table>
        )}

        {/* DELETING A MAILBOX IS DESTRUCTIVE -- it drops the vault secret, the
            Credential row and every inbound_message for it, server-side and in
            one transaction (api.ts). `String(remove.error)` rendered "Error: "
            for a bodyless 503, so a refused delete left the row on screen with
            a label beside it and nothing else: exactly Auditor A's BLOCKER
            shape, a failed destructive call reading as success. */}
        {start.isError && (
          <p role="alert" className="notice red" style={{ margin: 14 }}>
            {t("emailIntake.mailboxes.startFailed", { reason: describeSubmitError(start.error) })}
          </p>
        )}
        {disconnect.isError && (
          <p role="alert" className="notice red" style={{ margin: 14 }}>
            {t("emailIntake.mailboxes.disconnectFailed", { reason: describeSubmitError(disconnect.error) })}
          </p>
        )}
        <ConfirmDialog
          open={pendingDisconnect !== null}
          title={t("emailIntake.mailboxes.disconnectTitle")}
          subject={pendingDisconnect?.address ?? ""}
          body={t("emailIntake.mailboxes.disconnectBody")}
          confirmLabel={t("emailIntake.mailboxes.disconnect")}
          destructive
          onCancel={() => setPendingDisconnect(null)}
          onConfirm={() => {
            if (!pendingDisconnect) return;
            disconnect.mutate(pendingDisconnect.id);
            setPendingDisconnect(null);
          }}
        />

        {remove.isError && (
          <p role="alert" className="notice red" style={{ margin: 14 }}>
            {t("emailIntake.mailboxes.deleteFailed", { reason: describeSubmitError(remove.error) })}
          </p>
        )}

        {/* The body states the THREE things the server does in one transaction
            (api.ts's hook comment): the mailbox row, the vault secret and its
            Credential row, and every inbound_message recorded for it. Losing the
            message log is the consequence an operator is least likely to have
            in mind and the one they cannot get back.

            NOT `requireTyping`. components/confirm.tsx reserves that for a
            misclick that is unrecoverable AND wide; this is one named mailbox on
            one screen, and spending the typed-confirmation on it is how people
            learn to copy the word without reading it. */}
        <ConfirmDialog
          open={pendingDelete !== null}
          title={t("emailIntake.mailboxes.deleteTitle")}
          subject={pendingDelete?.address ?? ""}
          body={t("emailIntake.mailboxes.deleteBody")}
          confirmLabel={t("emailIntake.mailboxes.deleteConfirm")}
          destructive
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => {
            if (!pendingDelete) return;
            remove.mutate(pendingDelete.id);
            setPendingDelete(null);
          }}
        />

        {form !== null && (
          <MailboxForm
            mailbox={form.mailbox}
            categories={categories.data ?? []}
            categoriesForbidden={categoriesForbidden}
            onClose={() => setForm(null)}
          />
        )}
      </Panel>
    </div>
  );
}
