/**
 * Security logs: the event search (stat tiles, filters, the event list and the
 * raw-fields panel), the evidence export, and the prerequisites strip. Event
 * types are wire values and read through `enums.SecurityEventType`; usernames,
 * hostnames, sources and the stored fields are recorded data and are never
 * catalogued. The time-window phrases ("the last 7 days") are written so that
 * they sit after "in", "covers" and "across" without agreeing with anything.
 */
export const security = {
  page: {
    title: "Security logs",
    description: "Sign-ins, sign-outs and use of admin rights, as each device's agent reported them.",
    exportEvidence: "Export evidence CSV ({range})",
    copyLink: "Copy link to this view",
    csvCovers:
      "The CSV covers {window} and carries the username and event-type filters on this screen, so the file is what you are looking at.",
    linkCopied: "Link copied. It reopens this search with the same filters.",
    exportFailed: "The export failed. {reason}",
    window24h: "the last 24 hours",
    window7d: "the last 7 days",
    window30d: "the last 30 days",
    loadedNote: {
      one: "Across the {count} event loaded",
      other: "Across the {count} events loaded",
    },
    tiles: {
      events: "Events in window",
      eventsZero: "Nothing was collected in this window",
      failed: "Failed logins",
      failedZero: "No failed logon in this window",
      accounts: "Accounts seen",
      accountsZero: "No account appears in this window",
    },
    filters: {
      usernamePlaceholder: "Username",
      usernameLabel: "Search by username",
      eventTypeLabel: "Filter by event type",
      allEventTypes: "All event types",
      timeRange: "Time range",
    },
    emptyFiltered: {
      title: "No events match these filters",
      body: "Nothing in {window} matches {filters}. The filter may be narrower than the evidence, or spelled differently from the name the endpoint reported.",
      filterUsername: "username “{username}”",
      filterEventType: "event type “{eventType}”",
      widen: "Widen to {range}",
      clear: "Clear filters",
    },
    empty: {
      title: "No events in this window",
      body: "No security event reached this tenant in {window}. An empty log and an agent not collecting one look identical here.",
    },
    columns: {
      event: "Event",
      host: "Host",
      user: "User",
      when: "When",
    },
    noAccount: "No account recorded",
    raw: {
      title: "Raw event",
      subtitle: "Selected row's stored fields",
      loading: "Loading…",
      noFields: "This event carries no stored fields.",
      noneTitle: "No event selected",
      noneBody: "Choose a row in the event list to see the fields stored with it.",
    },
  },
  prerequisites: {
    title: "Before security events can be collected",
    label: "A Windows or Mac computer runs the agent",
    done: {
      one: "{windows} Windows and {mac} Mac computer runs the agent.",
      other: "{windows} Windows and {mac} Mac computers run the agent.",
    },
    missing:
      "The agent collects these events on Windows and Mac computers only, and none runs the agent yet, so nothing will arrive here.",
    cta: "Add a Windows or Mac computer",
  },
} as const;
