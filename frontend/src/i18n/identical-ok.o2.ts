/**
 * IDENTICAL_OK entries of wave O2 (Task 7): the catalogues `dashboard`, `serviceDesk`, `sla`, `emailIntake`, `notification`, `portal`.
 * The wave adds and removes its own entries here and nowhere else, so the eight
 * waves that run in parallel never edit one file (redesign plan, "Archivos
 * compartidos"); `catalogue.identical.test.ts` holds each file to its catalogues.
 */
export const IDENTICAL_OK_O2: Record<string, string> = {
  "serviceDesk.common.no": "\"No\" is the same word in Spanish.",
  "serviceDesk.nav.tickets": "\"Tickets\" is the glossary term for tickets in Spanish too (a loanword).",
  "serviceDesk.table.ticket": "\"Ticket\" is the glossary term for a ticket in Spanish too (a loanword).",
  "serviceDesk.list.title": "\"Tickets\" is the glossary term for tickets in Spanish too (a loanword).",
  "serviceDesk.list.footNormal#one": "A count and \"normal\", which is the same word in the singular in Spanish.",
  "portal.nav.aria": "\"Portal\" is the same word in Spanish.",
  "portal.myTickets.ticket": "\"Ticket\" is the glossary term for a ticket in Spanish too (a loanword).",
  "sla.ticket.title": "\"SLA\" is an acronym (service-level agreement), written the same in Spanish.",
  "sla.ticket.no": "\"No\" is the same word in Spanish.",
  "emailIntake.mailboxes.starttls": "\"STARTTLS\" is the name of the protocol extension, written the same in Spanish.",
  "emailIntake.form.starttlsOption": "The name of the protocol extension and its port number, written the same in Spanish.",
  "emailIntake.mailboxes.actionAria": "Only placeholders: an already-worded action and the mailbox address.",
  "emailIntake.microsoft.tenantPlaceholder": "An example Azure domain name, a technical identifier written the same in Spanish.",
  "emailIntake.messages.colTicket": "\"Ticket\" is the glossary term for a ticket in Spanish too (a loanword).",
  "notification.common.host": "\"Host\" is the loanword Spanish IT uses for a server address, as in \"clave de host\".",
  "notification.admin.webhookCount#one": "A count and \"webhook\", the name of the technology, written the same in the singular in Spanish.",
  "notification.channels.url": "\"URL\" is an acronym, written the same in Spanish.",
  "notification.channels.urlPlaceholder": "An example web address, a technical identifier written the same in Spanish.",
  "notification.signIn.tenantPlaceholder": "An example Azure domain name, a technical identifier written the same in Spanish.",
  "notification.allowlist.hostPlaceholder": "An example host name, a technical identifier written the same in Spanish.",
};
