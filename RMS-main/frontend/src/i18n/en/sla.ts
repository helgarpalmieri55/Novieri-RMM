/**
 * Service levels: business calendars (weekly windows, holidays), response and
 * resolution policies, the policy resolver, the org settings and the SLA panel
 * on a ticket's page. Calendar and policy names, queue names and timezone names
 * are recorded data and are never catalogued. The state words (Met, Breached,
 * Paused) are the same ones the service desk shows; the calendar health and
 * target state labels live in `enums` (CalendarHealth, TargetState).
 */
export const sla = {
  common: {
    loading: "Loading…",
    anyQueue: "Any queue",
    anyPriority: "Any priority",
    any: "Any",
    remove: "Remove",
    edit: "Edit",
    delete: "Delete",
    name: "Name",
    active: "Active",
    calendar: "Calendar",
    queue: "Queue",
    priority: "Priority",
    response: "Response",
    resolution: "Resolution",
    deleteFailed: "Delete failed: {reason}",
    removeFailed: "Remove failed: {reason}",
  },
  /** Priority names as the policy form lists them (the wire word, in English). */
  priorityOption: {
    low: "low",
    normal: "normal",
    high: "high",
    urgent: "urgent",
  },
  /** A length of time: the unit symbols, and the span under a minute. */
  duration: {
    days: "{count}d",
    hours: "{count}h",
    minutes: "{count}m",
    zero: "0m",
    underMinute: "under a minute",
  },
  weekday: {
    monday: "Monday",
    tuesday: "Tuesday",
    wednesday: "Wednesday",
    thursday: "Thursday",
    friday: "Friday",
    saturday: "Saturday",
    sunday: "Sunday",
    other: "weekday {weekday}",
  },
  /** The same names inside a label ("Add window (Monday)"); Spanish writes them in lower case there. */
  weekdayInline: {
    monday: "Monday",
    tuesday: "Tuesday",
    wednesday: "Wednesday",
    thursday: "Thursday",
    friday: "Friday",
    saturday: "Saturday",
    sunday: "Sunday",
  },
  /** Why a week of working windows cannot be saved (slaState.validateWindows). */
  windowProblem: {
    weekdayRange: "weekday {weekday} out of range 0-6 (Monday=0)",
    timesFormat: "{day}: times must be HH:MM",
    notBefore:
      "{day}: {start} is not before {end} (end 00:00 means midnight at the end of the day, and is the one legal exception)",
    overlap: "{day}: overlapping windows",
  },
  health: {
    noWindowsExplanation:
      "This calendar has no working windows, so no deadline can be computed from it. Tickets resolving to a policy on this calendar get no SLA at all.",
    invalidTimezoneExplanation:
      "This calendar's timezone does not resolve, so every deadline on it fails. Tickets resolving to a policy on this calendar get no SLA at all.",
  },
  timing: {
    none: {
      headline: "No SLA target",
      detail:
        "No policy matched this ticket, or its calendar cannot produce a deadline. This is a real state, not a missing value.",
    },
    void: {
      headline: "Promise voided — neither met nor breached",
      detailUndated:
        "The ticket reached a terminal-not-resolved status, so this deadline no longer applies.",
      detail:
        "Voided {date}, when the ticket reached a terminal-not-resolved status. This deadline no longer applies.",
    },
    breached: {
      headline: "Breached",
      headlineAgo: "Breached {duration} ago",
      deadlineWas: "Deadline was {date}",
      answeredLate: "answered {date}, after the deadline",
    },
    met: {
      headline: "Met",
      headlineAgo: "Met {duration} ago",
      inside: "Met {met}, {duration} inside the {due} deadline.",
      after: "Met {met}, {duration} after the {due} deadline.",
      at: "Met {met}.",
    },
    paused: {
      headline: "Paused — clock stopped",
      headlineFor: "Paused for {duration} — clock stopped",
      since: "Paused since {date}",
      extends: "the {date} deadline extends by the working time paused when the ticket resumes",
      credited: "{duration} of earlier pause already credited",
    },
    running: {
      headline: "Running",
      remaining: "{duration} remaining",
      due: "Due {date}.",
      pastDue: "{duration} past due",
      pastDueDetail: "Due {date}; the server has not recorded a breach yet.",
    },
  },
  resolver: {
    level1: "this queue + this priority",
    level2: "this queue + any priority",
    level3: "any queue + this priority",
    level4: "any queue + any priority",
    title: "Which policy applies?",
    subtitle: "Which active policy a ticket resolves to, and why.",
    forbidden: "Policies not available (insufficient permissions).",
    failed: "Policies did not load. {reason}",
    queueId: "Queue ID",
    selectQueue: "Select a queue",
    pickQueue: "Pick a queue to see which policy a ticket in it would resolve to.",
    noMatch:
      "No policy matches {queue} + {priority}. A ticket filed with this combination gets no SLA at all: not a default and not a zero.",
    resolved:
      "Resolved at level {level} ({label}): response {response}, resolution {resolution}, on calendar {calendar}.",
    colLevel: "Level",
    colMatches: "Matches on",
    colPolicy: "Policy",
    colOutcome: "Outcome",
    policyCell: "{response} / {resolution} on {calendar}",
    applies: "Applies",
    noActive: "No active policy at this level",
    shadowed: "Shadowed by a more specific level",
  },
  admin: {
    title: "Service levels",
    description: "Response and resolution targets, by priority and calendar.",
    calendarsLabel: "Business calendars",
    calendarsZero: "No business calendar defined yet",
    allCanProduce: "All can produce a deadline",
    brokenCalendars: {
      one: "{count} cannot produce a deadline",
      other: "{count} cannot produce a deadline",
    },
    policiesLabel: "Active policies",
    policiesZero: "No policy is active, so nothing is being measured",
    policiesFoot: {
      one: "{active} of {count} configured",
      other: "{active} of {count} configured",
    },
    noSlaLabel: "Tickets with no SLA",
    noSlaFoot: "No policy matched them, or their calendar cannot produce a deadline.",
    noSlaZero: "Every ticket has an SLA",
    staleLabel: "Deadlines out of date",
    staleFoot: "A policy or calendar edit could not be applied to them. The edit itself saved.",
    staleZero: "Every deadline matches its policy",
  },
  prerequisites: {
    title: "Before a target can match a queue",
    subtitle: "What the service desk needs to hold for targets to be aimed at part of it.",
    queueLabel: "A ticket queue exists",
    queueDone: { one: "{count} ticket queue exists.", other: "{count} ticket queues exist." },
    queueMissing:
      "No ticket queue exists yet, so a target can only match every queue at once and a breach has nowhere to escalate to.",
    queueCta: "Set up queues",
  },
  settings: {
    title: "SLA settings",
    forbidden: "Settings not available (insufficient permissions).",
    failed: "Settings did not load. {reason}",
    staleNote:
      "Tickets whose deadline is out of date are still counting, measured against the configuration this one replaced.",
    neverConfigured: {
      one: "Never configured — this org is running on the default of {count} day.",
      other: "Never configured — this org is running on the default of {count} days.",
    },
    needsAdmin: "Changing SLA settings needs the SLA administrator permission.",
    daysLabel: "Auto-close resolved tickets after (days)",
    daysWhole: "Auto-close window must be a whole number of days",
    daysRange: "Auto-close window must be between 1 and {max} days",
    save: "Save settings",
    saving: "Saving…",
    saved: "Saved. The value above is what the server returned.",
    notSaved: "Settings were not saved.",
  },
  calendars: {
    title: "Business calendars",
    subtitle: "Working hours and holidays. Deadlines count only these hours.",
    add: "Add calendar",
    forbidden: "Calendars not available (insufficient permissions).",
    failed: "Calendars did not load. {reason}",
    colHealth: "Health",
    colCreated: "Created",
    hoursAndHolidays: "Hours & holidays",
    close: "Close",
    empty: "No calendars yet — a policy needs one before any ticket can get an SLA.",
    deleteTitle: "Delete business calendar",
    deleteBody:
      "Its working windows and holidays go with it. Any policy pointing at this calendar loses the thing that turns minutes into a deadline, and tickets resolving to that policy get no SLA at all. To take a calendar out of use without destroying it, clear Active on it instead.",
    deleteConfirm: "Delete calendar",
    addTitle: "Add calendar",
    editTitle: "Edit calendar",
    editHint: "Changing the timezone recomputes every still-running deadline on this calendar.",
    timezoneLabel: "Timezone (IANA name)",
    timezoneHint:
      "This is a real value and will be saved as typed — {example} is only a starting point. Type to search the IANA list.",
    timezoneShape: "Timezone must be an IANA name such as {example} — \"{value}\" is not one",
    timezoneUnknown:
      "There is no timezone named \"{value}\" — check the spelling against the IANA list",
  },
  detail: {
    forbidden: "Calendar not available (insufficient permissions).",
    failed: "This calendar could not be loaded. {reason}",
    loading: "Loading calendar…",
  },
  windows: {
    title: "Working hours",
    perWeek: "{duration} of working time per week",
    none: "No working windows. A calendar with no working time cannot produce a deadline, so every ticket resolving to a policy on it gets no SLA at all.",
    nonWorkingDay: "Non-working day",
    startAria: "{day} window {position} start",
    endAria: "{day} window {position} end",
    endsAtMidnight: "ends at midnight (24:00)",
    addWindow: "Add window",
    addWindowAria: "Add window ({day})",
    day24: "24-hour day",
    day24Aria: "Add 24-hour day ({day})",
    notSaved: "The week was not saved. {reason}",
    saving: "Saving…",
    saveWeek: "Save week",
    revert: "Revert",
    unsaved: "Unsaved changes",
    replaceNote:
      "Saving replaces the whole week and recomputes every still-running deadline on this calendar.",
  },
  holidays: {
    title: "Holidays",
    note: "A holiday removes that date's working windows entirely; a half-day is expressed by not marking the day at all.",
    colDate: "Date",
    colName: "Name",
    empty: "No holidays on this calendar.",
    removeTitle: "Remove this holiday",
    removeBody:
      "This date goes back to being a working day on this calendar, and every still-running deadline that crosses it moves earlier. Re-adding the holiday afterwards does not put those deadlines back.",
    removeConfirm: "Remove holiday",
    dateLabel: "Holiday date",
    dateShape: "Holiday date must be a calendar date such as 2026-12-25",
    nameLabel: "Holiday name",
    namePlaceholder: "Christmas Day",
    add: "Add holiday",
    adding: "Adding…",
  },
  policies: {
    title: "SLA policies",
    subtitle: "First match wins, most specific first. Blank matches any.",
    add: "Add policy",
    forbidden: "Policies not available (insufficient permissions).",
    failed: "Policies did not load. {reason}",
    colOnBreach: "On breach",
    colActive: "Active",
    empty: "No policies yet — with none configured, no ticket gets an SLA at all.",
    deleteTitle: "Delete SLA policy",
    deleteBody:
      "Tickets currently resolved by this policy lose the promise attached to them, and new tickets matching it get no SLA at all unless another policy matches. To stop a policy applying without destroying it, clear Active on it instead.",
    deleteConfirm: "Delete policy",
    addTitle: "Add policy",
    editTitle: "Edit policy",
    editHint:
      "Changing a target or the calendar recomputes every still-running deadline under this policy.",
    raisePriority: "raise priority",
    moveTo: "move to {queue}",
    notifyOnly: "notify only",
    queueSublabel: "queue: {key}",
    queueSublabelInactive: "queue: {key} · inactive",
    inactive: "inactive",
    queuesForbiddenHint:
      "Queue names need permission to view tickets, which this account does not have, so the two queue fields below can only offer their defaults. Ask an administrator if a policy has to be scoped to one queue.",
    queueLabel: "Queue (blank = any queue)",
    anyQueueSublabel: "the policy matches tickets in every queue",
    queueEmptyForbidden:
      "Queue names need permission to view tickets, which this account does not have. Leave this as Any queue, or ask an administrator.",
    queueEmpty:
      "No ticket queues exist yet — the service desk owns them, and until one exists a policy can only match Any queue.",
    priorityLabel: "Priority (blank = any priority)",
    calendarPlaceholder: "Search calendars…",
    calendarsFailed:
      "The calendar list could not be loaded, so this is not a claim that none exist. Reload before saving a policy.",
    calendarsEmpty:
      "No business calendars yet. A calendar is what turns minutes into a deadline, so add one under Business calendars before this policy can be saved.",
    noCalendars: "This organisation has no business calendars, so no policy can be saved yet.",
    addCalendar: "Add a business calendar",
    askAdmin: "Ask an SLA administrator to add one under Business calendars.",
    responseTarget: "Response target in minutes",
    resolutionTarget: "Resolution target in minutes",
    raiseOnBreach: "Raise priority on breach",
    escalateQueueLabel: "Escalate to queue on breach (blank = stay in queue)",
    stayInQueue: "Stay in the same queue",
    stayInQueueSublabel: "a breach raises no queue change",
    escalateEmptyForbidden:
      "Queue names need permission to view tickets, which this account does not have. A breach will leave the ticket where it is.",
    escalateEmpty: "No ticket queues exist yet, so there is nowhere to escalate to.",
    noRecompute:
      "A breach escalation deliberately does not recompute the deadline — a system priority bump is the engine reacting to its own output, not new information about the work.",
    activeLabel: "Active (only active policies are considered during resolution)",
    targetNegative:
      "{label} cannot be negative — a deadline before the ticket exists is not a promise",
    targetWhole: "{label} must be a whole number of minutes",
    targetZero:
      "{label} must be at least 1 minute — a zero-minute target is breached on creation",
  },
  ticket: {
    title: "SLA",
    forbidden: "SLA state not available (insufficient permissions).",
    noSlaNote:
      "No policy matched this ticket, or the calendar its policy points at cannot produce a deadline. This ticket has no response or resolution promise attached.",
    setTargets: "Set response targets",
    askAdmin: "Response and resolution targets are set by an administrator under Service levels.",
    factSource: "Policy applied because",
    factEscalated: "Escalated",
    factPolicy: "Policy",
    factHours: "Working hours",
    no: "No",
    sourceInitial: "It matched when the ticket was filed",
    sourceRetriage: "A person changed the priority or queue",
    sourceEscalation: "Breach escalation set it",
    noteRetriage: "The deadline was recomputed from the ticket's start.",
    noteEscalation: "Escalation deliberately does not recompute the deadline.",
  },
} as const;
