export const components = {
  ui: {
    actions: "Actions",
    scrollForMore: "Scroll for more columns",
  },
  confirm: {
    typeToConfirm: "Type <strong>{subject}</strong> to confirm",
  },
  attributes: {
    yes: "Yes",
    no: "No",
    viewRaw: "View raw",
  },
  picker: {
    search: "Search…",
    loading: "Loading…",
    noMatch: "No match.",
  },
  rowMenu: {
    moreActionsFor: "More actions for {subject}",
    moreActions: "More actions",
    actionsFor: "Actions for {subject}",
  },
  duration: {
    unit: "Unit",
    seconds: "seconds",
    minutes: "minutes",
    hours: "hours",
    days: "days",
  },
  chart: {
    category: "Category",
  },
  // A new row filed as every client's configuration (components/forEveryClient.tsx).
  forEveryClient: {
    label: "For every client",
    hint: "Every client's technicians will see it. Unticked, it belongs to your organisation's own client.",
  },
  // A new row's client, with "All my clients" (components/ClientField.tsx).
  clientField: {
    label: "Client",
    choose: "Choose a client",
    none: "None of your clients lets you do this.",
  },
  // A new row's site (components/SiteField.tsx).
  siteField: {
    label: "Site",
    choose: "Choose a site",
    chooseClientFirst: "Choose the client first.",
    loading: "Loading sites…",
    failed: "The sites could not be loaded.",
    none: "This client has no sites. Add one on the Clients page.",
  },
  prerequisites: {
    checking: "Checking…",
    cannotCheckNeeds: "This cannot be checked from this account: it needs {permissions}.",
    cannotCheck: "This cannot be checked from this account.",
    couldNotCheck: "This could not be checked. {reason}",
  },
  weekPainter: {
    day: {
      sunday: "Sunday",
      monday: "Monday",
      tuesday: "Tuesday",
      wednesday: "Wednesday",
      thursday: "Thursday",
      friday: "Friday",
      saturday: "Saturday",
    },
    dayShort: {
      sunday: "Sun",
      monday: "Mon",
      tuesday: "Tue",
      wednesday: "Wed",
      thursday: "Thu",
      friday: "Fri",
      saturday: "Sat",
    },
    dayColumn: "Day",
    rangeTo: "{from} to {to}",
    and: "and",
    nothingSelected: "Nothing selected. Paint the hours the window should open, or leave it empty for always.",
    custom: "Custom expression: the grid cannot draw it. Edit it under Advanced.",
    selected: "Selected: {summary}.",
    advanced: "Advanced",
    cronExpression: "Cron expression",
    cronHelp:
      "Five fields: minute, hour, day of month, month, day of week. The grid draws expressions whose minute is 0 and whose day of month and month are both *.",
  },
} as const;
