export const components = {
  // The notice a new-row form shows in place of Save while Unassigned is
  // chosen at the top (components/UnassignedRefusal.tsx).
  unassignedRefusal:
    "Unassigned is the client chosen at the top, and nothing new can be filed under it. Choose another client first.",
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
    hint: "Every client's technicians will see it. Unticked, it belongs to the client this form files it under.",
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
    failed: "The sites could not be loaded. {reason}",
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
  // The recent confirmation of the second factor (`components/stepUp.tsx`).
  stepUp: {
    title: "Confirm it's you",
    body: "Confirm your second factor to continue.",
    loading: "Loading your second factors…",
    usePasskey: "Use a passkey",
    code: "Code from your authenticator app",
    confirm: "Confirm",
    useRecovery: "Use a recovery code instead",
    recoveryCode: "Recovery code",
    recoveryHelp: {
      one: "You have {count} code left. It works once.",
      other: "You have {count} codes left. Each works once.",
    },
    confirmRecovery: "Use this code",
    noMethod: "Your account has no second factor, so this change cannot be confirmed. Ask an administrator to reset your second factors.",
    // A passkey is the only second factor, and this browser cannot use it.
    passkeyOnly:
      "This browser cannot use passkeys, and you have no other second factor to confirm with. Do this from a browser that supports passkeys.",
    cancelled: "Nothing was changed: the confirmation was closed.",
    // A client's contact without the authenticator app (own-auth phase 2, D29).
    password: "Your password",
    confirmPassword: "Confirm with your password",
    noMethodContact: "Sign out and sign in again, then make this change within 10 minutes.",
    // For that contact, `body`: the password is no second factor (C5).
    bodyPassword: "Enter your password to continue.",
    bodyPasswordOrPasskey: "Use a passkey or enter your password to continue.",
    // That contact's passkey, on a browser that cannot use it.
    passkeyUsePassword:
      "This browser cannot use passkeys, so your passkey is not offered here. Confirm with your password.",
  },
} as const;
