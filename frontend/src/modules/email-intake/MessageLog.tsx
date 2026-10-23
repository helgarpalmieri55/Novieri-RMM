import { Link, useSearchParams } from "react-router-dom";
import { NoValue } from "../../components/emptystate";
import { RelTime } from "../../components/reltime";
import { describeSubmitError } from "../../components/form";
import { Badge, Table, Td, Th } from "../../components/ui";
import { Icon } from "../../shell/icons";
import { FilterBar, Panel } from "../../shell/components";
import { useT } from "../../i18n/context";
import { ApiError } from "../../lib/api";
import { useEmailIntakeMessages } from "./api";
import { dispositionLabel } from "./intakeLabels";
import type { Disposition } from "./types";
import { DISPOSITIONS } from "./types";

const PAGE_LIMIT = 50;

// Spec §10: "the quarantined count is surfaced prominently -- quarantined
// mail is mail a human expected to become a ticket and which did not, and it
// is invisible unless shown."
//
// THE COUNT COMES FROM `quarantined_count`, NEVER FROM THE ROWS ON SCREEN.
// A page-local tally reads "3 quarantined" when the answer is 300, and the
// number would then silently depend on `limit`/`offset` -- worth nothing as
// an alarm. It is a `SELECT count(*)` over the whole scope, server-side.
//
// AND IT IS LABELLED "in this scope", NOT "below". `disposition` narrows the
// LIST but deliberately NOT the count (messages.py's asymmetry): filtering to
// `created` still reports every quarantined message, because a count that
// went to zero the moment an operator looked at something else would read as
// "nothing is quarantined" at exactly the wrong time. An alarm that silences
// when you look away is worse than no alarm. The label is what makes the
// number honest rather than baffling.
//
// THE FILTER IS A SERVER REQUEST. Selecting a disposition changes the query
// key, which refetches with `?disposition=...`; it does not filter the rows
// already fetched. A client-side filter over one page of 50 is not a filter
// at all -- it hides rows from a page that was already truncated, and the
// operator has no way to reach the rest.
// `zone` is the signed-in user's profile zone (`/me`.timezone), handed down
// from EmailIntakeAdminPage which already holds `me`. "Recorded" is the
// column an operator correlates against a user saying "I emailed you at
// half past two", so rendering it in a zone neither of them is in is the
// five-hour defect lib/datetime.ts exists to close.
export function MessageLog({ zone }: { zone?: string | null }) {
  const t = useT();
  // THE FILTER LIVES IN THE QUERY STRING, not in component state. The
  // "Quarantined" stat tile above points at
  // `/email-intake?disposition=quarantined#messages`, and with the selection
  // held locally there was nothing for it to point AT: the count was surfaced
  // prominently, per spec §10, and then the only route to the rows behind it
  // was a dropdown further down the page. A quarantined view is also the one
  // thing here worth pasting to a colleague.
  const [searchParams, setSearchParams] = useSearchParams();
  const raw = searchParams.get("disposition") ?? "";
  // An unrecognised `?disposition=` shows everything rather than filtering to
  // a value the server does not know: an empty table reads as "no mail has
  // ever arrived", which is the opposite of the truth.
  const disposition: Disposition | "" = (DISPOSITIONS as readonly string[]).includes(raw)
    ? (raw as Disposition)
    : "";
  const setDisposition = (next: Disposition | "") => {
    setSearchParams(
      (current) => {
        const params = new URLSearchParams(current);
        if (next) params.set("disposition", next);
        else params.delete("disposition");
        return params;
      },
      // Replace, not push: narrowing a log refines one view. Back should leave
      // the page, not retrace every dropdown selection.
      { replace: true },
    );
  };

  const messages = useEmailIntakeMessages(
    disposition ? { disposition, limit: PAGE_LIMIT } : { limit: PAGE_LIMIT },
  );

  const forbidden = messages.error instanceof ApiError && messages.error.status === 403;
  const failed = messages.error != null && !forbidden;
  const quarantined = messages.data?.quarantined_count ?? 0;

  return (
    // The Quarantined and Messages stat tiles link to `/email-intake#messages`.
    <div id="messages">
      <Panel
        title={t("emailIntake.messages.title")}
        subtitle={t("emailIntake.messages.subtitle")}
        bare
        actions={
          !forbidden &&
          messages.isSuccess && (
            <span className="flex items-center gap-2">
              <Badge
                variant={quarantined > 0 ? "destructive" : "muted"}
                aria-label={t("emailIntake.messages.quarantinedCount")}
              >
                {t("emailIntake.messages.quarantined", { count: quarantined })}
              </Badge>
              <span className="note">{t("emailIntake.messages.inScope")}</span>
            </span>
          )
        }
      >
        <FilterBar>
          <select
            className="select-field"
            aria-label={t("emailIntake.messages.filterAria")}
            value={disposition}
            onChange={(e) => setDisposition(e.target.value as Disposition | "")}
          >
            <option value="">{t("emailIntake.messages.allDispositions")}</option>
            {DISPOSITIONS.map((d) => (
              <option key={d} value={d}>
                {dispositionLabel(d)}
              </option>
            ))}
          </select>
          <span className="filter-spacer" />
          {messages.data && (
            <span className="mono cell-sub">
              {t("emailIntake.messages.showing", { shown: messages.data.messages.length, count: messages.data.total })}
            </span>
          )}
        </FilterBar>

        {messages.isLoading ? (
          <p className="note" style={{ padding: "16px 18px" }}>{t("emailIntake.common.loading")}</p>
        ) : forbidden ? (
          <p className="note" style={{ padding: "16px 18px" }}>{t("emailIntake.messages.forbidden")}</p>
        ) : failed ? (
          <p role="alert" className="notice red" style={{ margin: 14 }}>
            {t("emailIntake.messages.failed", { reason: describeSubmitError(messages.error) })}
          </p>
        ) : (
          <Table>
            <thead>
              <tr>
                {/* DISPOSITION SITS SECOND, AND THAT IS THE FIX. It is the
                    column this panel exists for -- quarantined / refused /
                    created -- and at 390px it was the third of six, outside
                    the viewport inside the table's horizontal scroll, so a
                    phone showed MESSAGE and RECORDED and nothing that said
                    what had happened to the mail. `Reason` folds into it,
                    where it belongs: the reason is why the disposition is
                    what it is, and as its own column it was one more thing
                    between the point and the reader.

                    BODY FOLDS INTO THE SAME CELL, for the second half of the
                    same fault. As its own trailing column it was off screen
                    at 390px inside the table's horizontal scroll, and yet it
                    set the height of the row: one long message drew a ~200px
                    row carrying nothing but a two-line subject, which reads
                    as a rendering fault rather than as a long email. Under
                    the subject and the sender it is on screen at every width
                    -- the widest cell in the row -- and clamped to two lines,
                    so no single message can decide how tall the log is. */}
                <Th>{t("emailIntake.messages.colMessage")}</Th>
                <Th>{t("emailIntake.messages.colDisposition")}</Th>
                <Th>{t("emailIntake.messages.colRecorded")}</Th>
                <Th>{t("emailIntake.messages.colTicket")}</Th>
              </tr>
            </thead>
            <tbody>
              {messages.data?.messages.map((m) => (
                <tr key={m.id}>
                  <Td
                    lead={<Icon name="mail" size={18} />}
                    gutter={m.disposition === "quarantined" ? "warn" : undefined}
                  >
                    <div className="cell-title">{m.subject}</div>
                    <div className="cell-sub mono">{m.from_address}</div>
                    {m.body_text ? (
                      // The list carries a 4 KiB excerpt, not the whole body
                      // (messages.BODY_EXCERPT_CHARS); two lines of it is
                      // enough to tell one message from another, and `title`
                      // keeps the rest of the excerpt reachable in place.
                      // The excerpt's own line breaks are NOT honoured here:
                      // a blank line inside the message would spend one of
                      // the two lines on nothing, so a mail whose second
                      // paragraph is where the fault is described would show
                      // an ellipsis instead. The whole excerpt is still in
                      // `title`, with its breaks.
                      <div
                        className="cell-sub"
                        title={m.body_text}
                        style={{
                          display: "-webkit-box",
                          WebkitBoxOrient: "vertical",
                          WebkitLineClamp: 2,
                          overflow: "hidden",
                          wordBreak: "break-word",
                        }}
                      >
                        {m.body_text.replace(/\s+/g, " ").trim()}
                      </div>
                    ) : (
                      // A message with no text part at all is a gap worth
                      // naming: it is why a `refused_parse` row exists. IN
                      // WORDS, not a dash: the dash had a column header to
                      // give it meaning and no longer does, which is the
                      // exact trap the reason line one cell over was written
                      // around.
                      <div className="cell-sub">{t("emailIntake.messages.noText")}</div>
                    )}
                    {/* Saying so is what stops the excerpt masquerading as
                        the whole message. */}
                    {m.body_truncated && (
                      <Badge variant="warning" style={{ marginTop: 4 }}>
                        {t("emailIntake.messages.truncated")}
                      </Badge>
                    )}
                  </Td>
                  <Td>
                    {/* `title` keeps the wire value reachable for anyone
                        correlating a row against the API or the logs. */}
                    <Badge
                      variant={m.disposition === "quarantined" ? "destructive" : "muted"}
                      title={m.disposition}
                    >
                      {dispositionLabel(m.disposition)}
                    </Badge>
                    {/* The reason line appears where a reason is the point:
                        under the pill it explains, and never as a bare em
                        dash under a `created` row. As its own column an
                        absent reason had a HEADER to give it meaning; folded
                        in here it would be an unlabelled dash on every
                        successful row, so absence is named only where it is a
                        gap -- mail held back or refused with nothing on
                        record saying why. */}
                    {m.reason ? (
                      <div className="cell-sub" style={{ marginTop: 4 }}>
                        {m.reason}
                      </div>
                    ) : m.disposition === "quarantined" || m.disposition.startsWith("refused") ? (
                      <div style={{ marginTop: 4 }}>
                        <NoValue label={t("emailIntake.messages.noReason")} />
                      </div>
                    ) : null}
                  </Td>
                  {/* `created_at` -- when this platform recorded the row --
                      is what the list is ordered and filtered on, because
                      `received_at` is the sender's own `Date:` header. */}
                  <Td>
                    <RelTime iso={m.created_at} zone={zone} />
                  </Td>
                  <Td>
                    {/* NOT THE RAW `ticket_id`. This cell printed the wire's
                        primary key as the link's text -- "tkt-1" over the
                        fixtures, a 36-character UUID against a real database
                        -- in a console that calls a ticket "INC-4127"
                        everywhere else, including in the subject one column to
                        the left. `InboundMessageOut` carries no ticket number
                        (backend `email_intake/schema.py`), so the number
                        cannot be shown from here; what CAN be fixed is
                        printing an identifier no person uses. The link names
                        its destination and the id stays reachable in `title`
                        for anyone correlating against the API; the accessible
                        name carries the subject, so fifty rows are not fifty
                        identical links. */}
                    {m.ticket_id ? (
                      <Link
                        to={`/service-desk/tickets/${m.ticket_id}`}
                        title={m.ticket_id}
                        aria-label={t("emailIntake.messages.openTicketAria", { subject: m.subject })}
                        style={{ whiteSpace: "nowrap" }}
                      >
                        {t("emailIntake.messages.openTicket")}
                      </Link>
                    ) : (
                      <NoValue label={t("emailIntake.messages.noTicket")} />
                    )}
                  </Td>
                </tr>
              ))}
              {messages.data && messages.data.messages.length === 0 && (
                <tr>
                  <Td colSpan={4}>
                    <span className="note">{t("emailIntake.messages.empty")}</span>
                  </Td>
                </tr>
              )}
            </tbody>
          </Table>
        )}
      </Panel>
    </div>
  );
}
