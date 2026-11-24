/**
 * IDENTICAL_OK entries of wave O6 (Task 11): the catalogues `docs`, `change`, `budget`, `people`, `credentials`, `reporting`.
 * The wave adds and removes its own entries here and nowhere else, so the eight
 * waves that run in parallel never edit one file (redesign plan, "Archivos
 * compartidos"); `catalogue.identical.test.ts` holds each file to its catalogues.
 */
export const IDENTICAL_OK_O6: Record<string, string> = {
  "people.holdings.signIn.roles": "\"Roles\" is the same word in Spanish.",
  "people.holdings.subscriptions.plan": "\"Plan\" is the same word in Spanish.",
  "people.holdings.tickets.ticket": "\"Ticket\" is the glossary term for a ticket in Spanish too (a loanword).",
  "docs.page.foot.recordType.other#one": "Only placeholders: a count and an already-worded record type.",
  "docs.editor.markdown": "\"Markdown\" is the name of the format, written the same in Spanish.",
  "docs.article.version": "Only placeholders: a version number and an already-formatted date.",
  "reporting.view.no": "\"No\" is the same word in Spanish.",
  "reporting.figures.sla.unit": "\"tickets\" is the glossary term for tickets in Spanish too (a loanword).",
  "reporting.fields.tickets": "\"Tickets\" is the glossary term for tickets in Spanish too (a loanword).",
  "change.detail.no": "\"No\" is the same word in Spanish.",
};
