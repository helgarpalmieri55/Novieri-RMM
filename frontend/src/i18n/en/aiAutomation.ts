/**
 * AI automation: the autonomy dial (persona by risk class), the queue of
 * actions the assistant proposed, and one action's confirmation screen.
 * Catalogue names, descriptions, hostnames, parameters, script names and the
 * assistant's own rationale stay as recorded; the server's refusal reasons stay
 * the server's words.
 */
export const aiAutomation = {
  /** The three dial positions as a sentence, for a row of its own. */
  autonomy: {
    off: "Not permitted",
    suggest: "May be named in prose only",
    require_approval: "Held for a person to confirm",
  },
  /** The three dial positions as a chip label. */
  ceiling: {
    off: "Off",
    suggest: "Suggest only",
    require_approval: "Needs approval",
  },
  /** What each position permits (docs/modules/ai-automation.md §4). */
  meaning: {
    off: "The assistant will not offer actions of this risk at all; asked for one, it says it is not allowed to.",
    suggest:
      "The assistant may describe an action in its answer. Nothing is prepared, so there is nothing to confirm and nothing runs.",
    require_approval:
      "The assistant may prepare the action and hold it. It runs only after a person confirms it.",
  },
  rejection: {
    not_needed: "Not needed",
    wrong_device: "Wrong device",
    wrong_action: "Wrong action",
    needs_change_request: "Needs a change request",
    unsafe_now: "Unsafe right now",
    other: "Other",
  },
  persona: {
    end_user: "End user",
    technician: "Technician",
    admin: "Administrator",
  },
  risk: {
    low: "Low",
    medium: "Medium",
    high: "High",
  },
  /** What became of a proposal. */
  outcome: {
    previewed: "Awaiting approval",
    confirmed: "Ran",
    dispatched: "Ran",
    rejected: "Declined",
    expired: "Expired",
  },
  prerequisites: {
    title: "Before the assistant can act",
    subtitle: "What these ceilings need from elsewhere to govern anything.",
    statusLabel: "The assistant is switched on",
    statusDone: "The assistant can answer on this server.",
    statusMissing: "The assistant is off on this server, so it proposes nothing whatever is set here. {reason}",
    statusCta: "Open assistant settings",
    actionsLabel: "Approved actions exist for it to propose",
    actionsDone: {
      one: "{count} approved action is available to propose.",
      other: "{count} approved actions are available to propose.",
    },
    actionsMissing:
      "The assistant only proposes approved actions, and none exists, so these ceilings have nothing to allow.",
    actionsCta: "Open approved actions",
  },
  list: {
    title: "Assistant actions",
    description: "Everything the assistant proposed, and what happened to it.",
    empty: "Nothing is awaiting a decision.",
    action: "Action",
    whatRuns: "What runs",
    askedBy: "Asked by",
    risk: "Risk",
    state: "State",
    proposed: "Proposed {when}",
    destructive: "Destructive",
    tainted: "Prompted by untrusted content",
  },
  page: {
    title: "Confirm an action",
    description: "What will run, and why the assistant proposed it.",
    loading: "Loading…",
  },
  confirm: {
    expired: "This preview expired before it was confirmed. Ask the assistant again.",
    stale: "{sentence} Nothing was run. Reload the preview and look again.",
    refused: "{sentence} Nothing was run.",
    decline: "Decline",
    confirmAndRun: "Confirm and run",
    prepared: "Prepared for {host}. Nothing runs until you confirm it.",
    declineTitle: "Decline this action",
    declineSubtitle: "Say why. The reason is stored with the decision.",
    declineReason: "Decline reason",
    declining: "Declining…",
    sendDecline: "Send the decline",
    keepWaiting: "Keep it waiting",
    hint: "Confirming sends the job to the device. You need permission to confirm assistant actions, and permission to run this kind of job yourself.",
    settled: "This action is {state}; there is nothing left to decide.",
    showCurrent: "Show the current preview",
  },
  preview: {
    unknown: "unknown",
    expired: "expired",
    catalogueKey: "Catalogue key",
    description: "Description",
    autonomy: "Autonomy",
    script: "Script",
    redacted: "Redacted",
    rateLimit: "Rate limit",
    whatWillHappen: "What will happen",
    fromCatalogue: "From your organization's approved-action catalogue, not the assistant",
    action: "Action",
    whatRuns: "What runs",
    target: "Target",
    risk: "Risk",
    destructive: "Destructive",
    yes: "Yes",
    no: "No",
    expiresIn: "Expires in",
    parameters: "Parameters",
    noParameters: "This action takes no parameters.",
    values: {
      one: "{count} resolved value, in full",
      other: "{count} resolved values, in full",
    },
    tainted:
      "This suggestion is downstream of content that matched the injection heuristic. Read the action above rather than the argument below.",
    whyRegion: "Why it was suggested",
    whyTitle: "Why it was proposed",
    whySubtitle: "The assistant's own words. An argument, not a description of what will run.",
    noReason: "(no reason given)",
  },
  dial: {
    title: "AI automation",
    description: "How much the assistant may do on its own.",
    panelTitle: "Autonomy ceilings",
    panelSubtitle: "What each persona may prepare for each risk class",
    emptyNote: "An empty cell means Off. Nothing is saved until you press Save changes.",
    classNote:
      "Risk class comes from each approved action in your organization's catalogue, not from this page.",
    notChanged: "{persona} / {risk} risk was not changed.",
    gridStill: "The grid still shows what has not been applied.",
    persona: "Persona",
    riskColumn: "{risk} risk",
    endUserNote: "The assistant can never act for an end user, whatever is set here.",
    fixed: "Fixed",
    cellLabel: "{persona}, {risk} risk",
    unsaved: "Unsaved. Stored: {level}",
    saving: "Saving…",
    save: "Save changes",
    everyClientOnly: "Read-only: every client shares the autonomy dial, so changing it requires the ai.admin permission on every client.",
    discard: "Discard changes",
    unsavedChanges: {
      one: "{count} unsaved change: {changes}",
      other: "{count} unsaved changes: {changes}",
    },
    saved: "Saved. The grid shows what the server returned.",
    describe: "{persona} / {risk} risk: {from} → {to}",
    raiseTitle: "Raise the assistant's autonomy ceiling",
    changeTitle: "Change the assistant's autonomy ceiling",
    raiseConfirm: "Raise the ceiling",
    changeConfirm: "Apply the change",
    bodyLine: "{persona} at {risk} risk {meaning}.",
    bodyTail:
      "A ceiling can only lower what the approved-action catalogue already permits; every prepared action still requires a human confirmation, and destructive actions stay out of reach at every position. The change is written to the audit log with its old and new value.",
  },
  waiting: {
    title: "Actions waiting for approval",
    subtitle: "Prepared by the assistant and not yet answered by a person",
    allActions: "All actions",
    empty: "Nothing is waiting for a person.",
    action: "Action",
    risk: "Risk",
    sub: "{persona} · proposed {when}",
    review: "Review",
  },
} as const;
