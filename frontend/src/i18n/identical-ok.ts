import { IDENTICAL_OK_O1 } from "./identical-ok.o1";
import { IDENTICAL_OK_O2 } from "./identical-ok.o2";
import { IDENTICAL_OK_O3 } from "./identical-ok.o3";
import { IDENTICAL_OK_O4 } from "./identical-ok.o4";
import { IDENTICAL_OK_O5 } from "./identical-ok.o5";
import { IDENTICAL_OK_O6 } from "./identical-ok.o6";
import { IDENTICAL_OK_O7 } from "./identical-ok.o7";
import { IDENTICAL_OK_O8 } from "./identical-ok.o8";

/**
 * Catalogue leaves whose Spanish value is deliberately identical to the
 * English one: dot path (a plural form is `path#form`) -> why that is right.
 * `catalogue.identical.test.ts` fails on any identical leaf that is neither
 * listed here nor wholly one of SAME_IN_BOTH, and on any entry here that has
 * stopped being identical. Add an entry, with a real reason, when a term is
 * genuinely the same in both languages; never to silence pasted English.
 */

/** The catalogues each wave owns (redesign plan, "Archivos compartidos"). */
export const WAVE_NAMESPACES: Readonly<Record<string, readonly string[]>> = {
  o1: [
    "devices",
    "inventory",
    "onboarding",
    "agentPush",
    "agentReleases"
  ],
  o2: [
    "dashboard",
    "serviceDesk",
    "sla",
    "emailIntake",
    "notification",
    "portal"
  ],
  o3: [
    "health",
    "patch",
    "deploy",
    "remote",
    "jobs",
    "errorMonitor",
    "platformStatus",
    "security",
    "audit",
    "auditVocabulary"
  ],
  o4: [
    "backup",
    "bcp",
    "provisioning",
    "siteServers",
    "enrollment"
  ],
  o5: [
    "licenses",
    "subscriptions",
    "ipam"
  ],
  o6: [
    "docs",
    "change",
    "budget",
    "people",
    "credentials",
    "reporting"
  ],
  o7: [
    "settings",
    "profile",
    "aiAdmin",
    "aiAutomation"
  ],
  o8: [
    "auth",
    "setup"
  ]
};

/** The catalogues the base tasks own; their entries stay in this file. */
export const SHARED_NAMESPACES: readonly string[] = ["common", "components", "shell", "enums", "lib", "time", "validation", "clients"];

/** The shared entries (the base tasks'). */
export const IDENTICAL_OK_SHARED: Record<string, string> = {
  "common.language.english": "A language is named in its own language, in both catalogues.",
  "common.language.spanish": "A language is named in its own language, in both catalogues.",
  "shell.nav.item.tickets": "\"Tickets\" is the glossary term for tickets in Spanish too (a loanword).",
  "shell.nav.item.software": "\"Software\" is the word Spanish uses for software.",
  "shell.search.typeTicket": "\"Ticket\" is the glossary term for a ticket in Spanish too (a loanword).",
  "components.attributes.no": "\"No\" is the same word in Spanish.",
  "enums.StatusWord.error": "\"Error\" is the same word in Spanish.",
  "enums.PlatformProbe.redis": "\"Redis\" is a product name, written the same in Spanish.",
  "enums.PlatformModule.portal": "\"Portal\" is the same word in Spanish.",
  "enums.ErrorWhere.api": "\"API\" is an acronym, written the same in Spanish.",
  "enums.AuditTargetType.ticket": "\"Ticket\" is the glossary term for a ticket in Spanish too (a loanword).",
  "enums.AuditTargetType.script": "\"Script\" is the word Spanish uses for a script.",
  "enums.SeededRole.auditor": "\"Auditor\" is the same word in Spanish (the role that reads the audit log).",
  "shell.viz.heatLabel": "Only placeholders: count, label and summary, which are translated where they are supplied.",
  "enums.DeviceSource.manual": "\"manual\" is the same word in Spanish.",
  "enums.Interpreter.powershell": "\"PowerShell\" is Microsoft's product name, written the same in Spanish.",
  "enums.Interpreter.pwsh": "\"PowerShell 7\" is Microsoft's product name, written the same in Spanish.",
  "enums.Interpreter.bash": "\"Bash\" is the shell's name, written the same in Spanish.",
  "enums.Interpreter.sh": "\"Shell\" is the word Spanish uses for a command shell.",
  "enums.Interpreter.zsh": "\"Zsh\" is the shell's name, written the same in Spanish.",
  "enums.Interpreter.python": "\"Python\" is the language's name, written the same in Spanish.",
  "enums.SweepTrigger.manual": "\"Manual\" is the same word in Spanish.",
  "enums.TicketPriority.normal": "\"Normal\" is the same word in Spanish.",
  "enums.ChannelKind.webhook": "\"Webhook\" is the name of the technology, written the same in Spanish.",
  "enums.ReportSourceModule.sla": "\"SLA\" is an acronym (service-level agreement), written the same in Spanish.",
  "enums.ExpenseSource.manual": "\"Manual\" is the same word in Spanish.",
  "enums.ChangeType.normal": "\"Normal\" is the same word in Spanish.",
  "enums.AiSourceType.ticket": "\"Ticket\" is the glossary term for a ticket in Spanish too (a loanword).",
  "shell.newMenu.ticket": "\"Ticket\" is the glossary term for a ticket in Spanish too (a loanword).",
  "clients.detail.tabs.tickets": "\"Tickets\" is the glossary term for tickets in Spanish too (a loanword).",
  "clients.tickets.colTicket": "\"Ticket\" is the glossary term for a ticket in Spanish too (a loanword).",
};

/** Each wave's file, by wave. */
export const WAVE_ENTRIES: Readonly<Record<string, Record<string, string>>> = {
  o1: IDENTICAL_OK_O1,
  o2: IDENTICAL_OK_O2,
  o3: IDENTICAL_OK_O3,
  o4: IDENTICAL_OK_O4,
  o5: IDENTICAL_OK_O5,
  o6: IDENTICAL_OK_O6,
  o7: IDENTICAL_OK_O7,
  o8: IDENTICAL_OK_O8,
};

export const IDENTICAL_OK: Record<string, string> = {
  ...IDENTICAL_OK_SHARED,
  ...IDENTICAL_OK_O1,
  ...IDENTICAL_OK_O2,
  ...IDENTICAL_OK_O3,
  ...IDENTICAL_OK_O4,
  ...IDENTICAL_OK_O5,
  ...IDENTICAL_OK_O6,
  ...IDENTICAL_OK_O7,
  ...IDENTICAL_OK_O8,
};
