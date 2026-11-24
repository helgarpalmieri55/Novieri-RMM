/**
 * IDENTICAL_OK entries of wave O3 (Task 8): the catalogues `health`, `patch`, `deploy`, `remote`, `jobs`, `errorMonitor`, `platformStatus`, `security`, `audit`, `auditVocabulary`.
 * The wave adds and removes its own entries here and nowhere else, so the eight
 * waves that run in parallel never edit one file (redesign plan, "Archivos
 * compartidos"); `catalogue.identical.test.ts` holds each file to its catalogues.
 */
export const IDENTICAL_OK_O3: Record<string, string> = {
  "platformStatus.dependencies.no": "\"no\" is the same word in Spanish.",
  "audit.events.actor": "\"Actor\" is the same word in Spanish.",
  "patch.cron.sentence": "Only placeholders: the days and the hours, both worded where they are supplied.",
  "patch.decision.selectAllAria": "Only placeholders: an already-worded button label and the scope it acts on.",
  "patch.hostBar.subject#one": "Only a placeholder: the single selected machine's hostname, a technical name.",
  "patch.devicesPanel.range": "Only placeholders: a hostname and the already-worded range of rows shown.",
  "patch.policy.no": "\"No\" is the same word in Spanish.",
  "remote.start.unreachableLine": "Only placeholders: a device name and the already-worded reason it cannot be reached.",
  "remote.screen.factControl": "\"Control\" is the same word in Spanish.",
  "remote.screen.monitorLabel": "\"Monitor\" is the same word in Spanish.",
  "remote.screen.monitorOption": "\"Monitor\" is the same word in Spanish, followed by a number and the resolution.",
  "deploy.nav.scripts": "\"Scripts\" is the word Spanish uses for scripts.",
  "deploy.scripts.title": "\"Scripts\" is the word Spanish uses for scripts.",
  "deploy.schedules.ruleScript": "\"Script\" is the word Spanish uses for a script.",
  "deploy.schedules.fieldScript": "\"Script\" is the word Spanish uses for a script.",
  "deploy.runOnDevice.rule": "\"Script\" is the word Spanish uses for a script.",
  "deploy.cron.sentence": "Only placeholders: the days and the hours, both worded where they are supplied.",
  "health.statusCleared": "Only placeholders: the state and the reason it was cleared, both worded where they are supplied.",
  "health.subject.cpu": "\"cpu\" is an acronym, written the same in Spanish.",
  "health.subject.script": "\"script\" is the word Spanish uses for a script.",
  "health.detail.factTicket": "\"Ticket\" is the glossary term for a ticket in Spanish too (a loanword).",
  "health.actions.colScript": "\"Script\" is the word Spanish uses for a script.",
  "health.editor.matrixSelector": "\"Selector\" is the same word in Spanish.",
};
