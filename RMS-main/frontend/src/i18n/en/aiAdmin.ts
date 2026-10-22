/**
 * The assistant: the chat panel with its recent sessions, the assistant's
 * settings page (what leaves the network, which provider it goes to, what was
 * flagged), the transcript and the tool trace. The egress disclosure and the
 * provider sentences are security-relevant: they say what data leaves the
 * network, and are worded to say exactly that in both languages.
 *
 * Conversation content (the questions asked and the model's answers), session
 * titles, record ids, tool names and the flagged excerpts are recorded data and
 * stay as they were written; the provider, its kind and its model are shown as
 * the deployment configured them.
 */
export const aiAdmin = {
  egress: {
    heading: "What leaves this network when the assistant answers",
    leaves1: "The system prompt, which is a constant.",
    leaves2: "The tool schemas: module API shapes, structural, not customer data.",
    leaves3: "The administrator's question text.",
    leaves4:
      "Tool results: real customer data. Hostnames, ticket subjects and bodies, software inventory, alert text, license and vendor names, spend figures, audit action names.",
    never1: "Vault material: stored credentials and license keys.",
    never2: "Attachment bytes, session recordings and agent certificates.",
    never3: "Anything belonging to another organization.",
    contractual:
      "No-training and no-retention terms with a hosted provider are contractual. This platform cannot enforce them and does not pretend to.",
    selfHosted:
      "A self-hosted, OpenAI-compatible provider on your own network is the default: under it, nothing leaves.",
  },
  provider: {
    offSummary:
      "The assistant is off for everyone on this server because no model provider is set up. Someone with server access has to configure one.",
    adminHeading: "What the server administrator needs to set",
    adminDetail:
      "Set ITS_AI_PROVIDERS and the matching ITS_AI_<KEY>_KIND, _BASE_URL and _MODEL on the control plane, then restart it. Exactly one provider may be named.",
  },
  settings: {
    title: "Assistant settings",
    description: "What the assistant sends out, and what it has flagged.",
    whatLeaves: "What leaves",
    whatNeverLeaves: "What never leaves",
    providerTitle: "Provider",
    providerSubtitle: "Server configuration, the same for every organization",
    loading: "Loading…",
    modelProvider: "Model provider",
    serverConfig:
      "This is server configuration and the same provider serves every organization on this deployment. It is not chosen here, and no API key or base URL is shown here or entered here. Changing it is an edit to the control plane’s environment followed by a restart.",
    otherSettings:
      "How long conversations are kept, the per-person and per-organization question limits, and the daily usage limit are under Settings → Assistant.",
  },
  chat: {
    title: "Assistant",
    description:
      "Ask about your devices, alerts and tickets. It reads what the platform already knows and never runs anything on a device.",
    checking: "Checking whether the assistant is available…",
    unavailable: "the assistant is not available here",
    settingsLink: "Assistant settings",
    openSettings: "Open assistant settings",
    turnedOnBy:
      "The assistant is turned on by whoever runs this server, not from the console. Someone with permission to manage the assistant can see how it is set up.",
    invitation: "What would you like to know?",
    examples:
      "Try “which devices are missing the September cumulative update” or “why did backup on fs-archive-01 fail”. Answers are read-only and are bounded by your own permissions.",
    askLabel: "Ask a question",
    askPlaceholder: "Ask about the fleet",
    asking: "Asking…",
    ask: "Ask",
    recentTitle: "Recent sessions",
    recentSubtitle:
      "Every question, what the assistant looked up and what it proposed is kept in the audit log",
    noSessions: "No sessions yet.",
    name: "Name",
    when: "When",
    sessionSub: {
      one: "{persona} · {count} message",
      other: "{persona} · {count} messages",
    },
  },
  messages: {
    injection:
      "Content in {records} contains text addressed to an automated reader. It was treated as data and not acted on.",
    partial: "This answer is partial: {because}.",
    partialUnknown: "This answer is partial: the turn stopped before it finished.",
    stop: {
      tool_budget: "it reached this turn's tool call budget",
      byte_budget: "it reached this turn's limit on retrieved data",
      token_budget: "it reached this turn's output token budget",
      deadline: "it reached this turn's time limit",
      malformed: "it asked for the same information incorrectly several times",
      nonce_leak: "retrieved content contained an internal marker and the turn was stopped",
      provider_error: "the language model service did not answer",
      session_expired: "your signed-in session expired while it was working",
    },
  },
  trace: {
    status: {
      forbidden: "not permitted for you",
      not_found: "no such record",
      unknown_tool: "the assistant asked for a tool that does not exist",
      invalid_arguments: "the assistant asked for it incorrectly",
      blocked: "withheld: the result may have carried secret material",
      timeout: "timed out",
      upstream_error: "failed",
      session_expired: "your session expired",
    },
    records: {
      one: "{count} record",
      other: "{count} records",
    },
    recordsTruncated: {
      one: "{count} record; more were not shown",
      other: "{count} records; more were not shown",
    },
  },
  flags: {
    title: "Flagged retrieved content",
    subtitle: "Text addressed to an automated reader, treated as data and not acted on",
    unavailable: "Flagged content is not available. {sentence}",
    empty: "No retrieved content has been flagged.",
    record: "Record",
    rule: "Rule",
    excerpt: "Excerpt",
    foundWhile: "found while {tool}",
    unnamedRule: "Unnamed rule",
  },
} as const;
