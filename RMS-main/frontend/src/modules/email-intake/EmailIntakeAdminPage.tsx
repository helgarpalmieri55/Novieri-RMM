import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo } from "react";
import { useLocation, useSearchParams } from "react-router-dom";
import { useT } from "../../i18n/context";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { useEmailIntakeMessages, useEmailMailboxes } from "./api";
import { EmailIntakePrerequisites } from "./EmailIntakePrerequisites";
import { MailboxList } from "./MailboxList";
import { MessageLog } from "./MessageLog";
import { MicrosoftConnection } from "./MicrosoftConnection";
import { PageHead, StatCard, StatsGrid } from "../../shell/components";

// Spec §10's admin surface, gated on `email_intake.read` with every mutation
// (and the connection test) gated on `email_intake.admin`. Both permissions
// are seeded to super-admin/org-admin only (spec §9), so a technician sees
// nothing here at all -- App.tsx's Shell nav omits the link entirely when
// `email_intake.read` is absent.
//
// The UI never gates security (invariant #6): `canRead`/`canAdmin` below only
// decide what renders. The server's `require_permission` on every route is
// the actual authority and rejects an unauthorized call regardless.
//
// `email_intake.admin` IS A HIGH-PRIVILEGE GRANT -- it sets which mailbox the
// platform authenticates to and which category inbound tickets land in --
// which is why this page says so rather than presenting mailbox config as
// ordinary settings.
//
// WHAT THE DESIGN ASKS FOR THAT NO ENDPOINT SERVES (UI port spec §7). The
// prototype's stat row reads "Converted to tickets 71", "Rejected 3", "Median
// conversion 18s" -- none of them exist here. `MessageLogOut` carries a
// `quarantined_count` computed server-side over the whole scope (messages.py,
// deliberately not narrowed by the `disposition` filter -- see MessageLog),
// but there is no equivalent aggregate for "converted" or "rejected", and no
// endpoint records how long a message took to become a ticket. Producing
// either would mean one request per disposition value or scanning every row
// client-side, which is the client-side aggregation this port's other
// modules forbid. The cards below use the two counts the backend actually
// computes -- messages in the current scope and quarantined within it -- plus
// the mailbox counts `MailboxList` already fetches in full (mailboxes are not
// paginated).
/**
 * The stat row's two halves: where a tile points, and making it land.
 *
 * WHERE IT POINTS. `/email-intake` is this module's only route in App.tsx, so every
 * tile above links to a view OF this page — which is honest, because the rows
 * behind each number are on this screen. `link()` PRESERVES whatever is
 * already in the query string and only overwrites the keys it is given: a tile
 * is a place to look, not a reset button, and a tile that rebuilt the URL from
 * scratch would silently discard the reader's current filter or selection on
 * the way past.
 *
 * MAKING IT LAND. React Router pushes a fragment and stops there — it does not
 * scroll, and `App.tsx` wires no scroll restoration. Without the effect below,
 * a fragment tile navigates and moves nothing on screen, which is the dead
 * tile the operator asked us to remove, wearing an href.
 */
function useStatLinks(route: string) {
  const { hash } = useLocation();
  const [searchParams] = useSearchParams();

  useEffect(() => {
    if (!hash) return;
    const target = document.getElementById(decodeURIComponent(hash.slice(1)));
    // Guarded: jsdom does no layout and defines no `scrollIntoView`, and a
    // TypeError here would take the whole page down.
    if (target && typeof target.scrollIntoView === "function") {
      target.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [hash]);

  return useCallback(
    (fragment: string, params: Readonly<Record<string, string>> = {}) => {
      const next = new URLSearchParams(searchParams);
      for (const [key, value] of Object.entries(params)) next.set(key, value);
      const query = next.toString();
      return `${route}${query ? `?${query}` : ""}#${fragment}`;
    },
    [route, searchParams],
  );
}

export function EmailIntakeAdminPage() {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canAdmin = permissions.includes("email_intake.admin");

  // The mailbox list is fetched in full (no pagination), so counting over it
  // is a real total, not a page tally. `{ limit: 50 }` is the SAME query key
  // `MessageLog` issues by default (no disposition filter), so this reads
  // from the same cache entry rather than doubling the request.
  const mailboxes = useEmailMailboxes();
  const messages = useEmailIntakeMessages({ limit: 50 });
  const mailboxesWithErrors = mailboxes.data
    ? mailboxes.data.filter((m) => m.last_error !== null).length
    : undefined;

  const link = useStatLinks("/email-intake");

  // WHAT EACH FIGURE IS MADE OF. Every tile here drew a caption and a number
  // and nothing else, so each one left an empty band under the figure (`.tile`
  // has a 92px floor) -- most visible on the two tinted tiles, where the gap
  // sits against the wash. `zeroLabel` fills that line only at zero, which is
  // the one count these tiles rarely show. Each line below is a second
  // dimension of the SAME rows the figure counts, read off data this page
  // already holds (no extra request), and each is `undefined` when the figure
  // is a zero or an unknown, so the zero label keeps the line to itself.
  //
  // The memos below hold COUNTS, never sentences: a memoised sentence would keep
  // the language the page was in, so the words are put around the counts below.
  const mailboxCounts = useMemo(() => {
    const boxes = mailboxes.data;
    if (!boxes || boxes.length === 0) return undefined;
    const polling = boxes.filter((m) => m.active).length;
    return { polling, off: boxes.length - polling };
  }, [mailboxes.data]);
  const mailboxBreakdown = mailboxCounts
    ? [
        mailboxCounts.polling > 0 ? t("emailIntake.admin.polling", { count: mailboxCounts.polling }) : null,
        mailboxCounts.off > 0 ? t("emailIntake.admin.switchedOff", { count: mailboxCounts.off }) : null,
      ]
        .filter((part): part is string => part !== null)
        .join(" · ")
    : undefined;

  const errorCounts = useMemo(() => {
    const boxes = mailboxes.data;
    if (!boxes || mailboxesWithErrors === undefined || mailboxesWithErrors === 0) return undefined;
    return { errors: mailboxesWithErrors, total: boxes.length };
  }, [mailboxes.data, mailboxesWithErrors]);
  const errorBreakdown = errorCounts
    ? t("emailIntake.admin.errorsFoot", { errors: errorCounts.errors, count: errorCounts.total })
    : undefined;

  // WHAT BECAME OF THE MAIL, and only when this page holds all of it. The
  // list is one page of 50; tallying dispositions over it would read "6
  // reached a ticket" where the answer is 600, which is the page-local tally
  // `MessageLog`'s own header refuses for the quarantined count. When the
  // page IS the whole scope the tally is the truth; when it is not, the line
  // says what is actually on screen instead of guessing.
  const messageCounts = useMemo(() => {
    const data = messages.data;
    if (!data || data.total === 0) return undefined;
    if (data.messages.length < data.total) return { kind: "listed" as const, listed: data.messages.length };
    const reached = data.messages.filter((m) =>
      m.disposition === "created" || m.disposition === "commented" || m.disposition === "reopened",
    ).length;
    return { kind: "tally" as const, reached, held: data.total - reached };
  }, [messages.data]);
  const messageBreakdown = !messageCounts
    ? undefined
    : messageCounts.kind === "listed"
      ? t("emailIntake.admin.listedBelow", { count: messageCounts.listed })
      : [
          messageCounts.reached > 0 ? t("emailIntake.admin.reachedTicket", { count: messageCounts.reached }) : null,
          messageCounts.held > 0 ? t("emailIntake.admin.heldBack", { count: messageCounts.held }) : null,
        ]
          .filter((part): part is string => part !== null)
          .join(" · ");

  const quarantinedCounts = useMemo(() => {
    const data = messages.data;
    if (!data || data.quarantined_count === 0) return undefined;
    return { quarantined: data.quarantined_count, total: data.total };
  }, [messages.data]);
  const quarantinedBreakdown = quarantinedCounts
    ? t("emailIntake.admin.quarantinedFoot", { quarantined: quarantinedCounts.quarantined, count: quarantinedCounts.total })
    : undefined;


  // A PENDING OR FAILED QUERY IS NOT A ZERO. `?? 0` printed "0 quarantined"
  // for the whole of the first paint and for every failed load, so "nothing
  // is quarantined" and "we have not been told" looked identical — the same
  // defect `zeroLabel` closes one level down. An em dash for absent data means
  // a zero on screen is always a measured zero.
  const count = (n: number | undefined): string => (n === undefined ? "—" : String(n));

  return (
    <div>
      <PageHead
        title={t("emailIntake.admin.title")}
        description={t("emailIntake.admin.description")}
      />

      <EmailIntakePrerequisites />

      {/* COLOUR IS CATEGORY, WEIGHT IS URGENCY. Two of these are counts of
          what exists; two are counts of mail that a human expected to become
          a ticket and which did not, and only those two carry weight. They
          are deliberately NOT the same hue: a mailbox that stopped polling is
          a broken integration (red — fix the mailbox), while quarantined mail
          is a triage queue that is working exactly as designed (orange —
          review it). Giving both the alarm colour would say the platform is
          failing every time a stranger emails the helpdesk. */}
      <StatsGrid columns={4}>
        <StatCard
          label={t("emailIntake.admin.mailboxesLabel")}
          value={count(mailboxes.data?.length)}
          icon="mail"
          to={link("mailboxes")}
          tone="blue"
          foot={mailboxBreakdown}
          /* Not an alarm, but not nothing either: no mailbox means no email
             ever becomes a ticket, and the label has to say that rather than
             leave a bare 0 reading as "fine". */
          zeroLabel={t("emailIntake.admin.mailboxesZero")}
        />
        <StatCard
          label={t("emailIntake.admin.errorsLabel")}
          value={count(mailboxesWithErrors)}
          /* THE GLYPH IS THE KIND, NOT THE HUE. `tone="red" urgent` draws
             this tile crit, and the plan's severity table pairs crit with
             `circle-x`; `alert-triangle` belongs to warn. With the warn glyph
             here and on the Quarantined tile below, the two tiles differed by
             colour alone -- exactly what the severity icon is for. */
          icon="circle-x"
          to={link("mailboxes")}
          tone="red"
          urgent
          foot={errorBreakdown}
          zeroLabel={t("emailIntake.admin.errorsZero")}
        />
        <StatCard
          label={t("emailIntake.admin.messagesLabel")}
          value={count(messages.data?.total)}
          icon="logs"
          to={link("messages")}
          tone="blue"
          foot={messageBreakdown}
          zeroLabel={t("emailIntake.admin.messagesZero")}
        />
        <StatCard
          label={t("emailIntake.admin.quarantinedLabel")}
          value={count(messages.data?.quarantined_count)}
          /* Warn's own glyph. `shield-lock` is not in the kind table at all,
             so a warn-washed tile was wearing a glyph that named no severity
             -- and quarantine IS the warn story: mail held for a human to
             look at, not a broken integration. */
          icon="alert-triangle"
          /* The one tile here that reaches a genuinely different view: the
             message log narrowed to the quarantined rows. `?disposition=` is
             the param `MessageLog` reads. */
          to={link("messages", { disposition: "quarantined" })}
          tone="orange"
          urgent
          foot={quarantinedBreakdown}
          zeroLabel={t("emailIntake.admin.quarantinedZero")}
        />
      </StatsGrid>

      {/* The profile zone (`/me`.timezone, cycle 2) reaches both panels that
          render a time. This page already holds `me` for its permission
          gate, so nothing new is fetched. */}
      <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}>
        <MailboxList canAdmin={canAdmin} zone={me.data?.timezone} />
        <MicrosoftConnection canAdmin={canAdmin} />
        <MessageLog zone={me.data?.timezone} />
      </div>
    </div>
  );
}
