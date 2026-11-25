/**
 * IDENTICAL_OK entries of wave O7 (Task 12): the catalogues `settings`, `profile`, `aiAdmin`, `aiAutomation`.
 * The wave adds and removes its own entries here and nowhere else, so the eight
 * waves that run in parallel never edit one file (redesign plan, "Archivos
 * compartidos"); `catalogue.identical.test.ts` holds each file to its catalogues.
 */
export const IDENTICAL_OK_O7: Record<string, string> = {
  "settings.page.tabs.roles": "\"Roles\" is the same word in Spanish.",
  "settings.users.columns.roles": "\"Roles\" is the same word in Spanish.",
  "settings.users.manageRoles": "\"Roles\" is the same word in Spanish.",
  "settings.email.hostPlaceholder": "An example mail server name, a technical identifier written the same in Spanish.",
  "settings.email.portPlaceholder": "An example port number, written the same in Spanish.",
  "settings.email.addressPlaceholder": "An example mail address, a technical identifier written the same in Spanish.",
  "settings.email.tenantPlaceholder": "An example Azure domain name, a technical identifier written the same in Spanish.",
  "settings.notifications.add.url": "\"URL\" is an acronym, written the same in Spanish.",
  "aiAutomation.preview.script": "\"Script\" is the loanword Spanish uses, written the same.",
  "aiAutomation.preview.no": "\"No\" is the same word in Spanish.",
};
