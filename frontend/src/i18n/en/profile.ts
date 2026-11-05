export const profile = {
  page: {
    title: "Your profile",
    description: "How this console shows you, and where you are.",
    loading: "Loading…",
    signedInAs:
      "Signed in as {email}. Your sign-in address is not changed here: changing it needs the re-authentication and verification this console does not perform. Ask an administrator.",
    displayName: "Display name",
    phone: "Phone",
    timezone: "Timezone",
    timezonePlaceholder: "e.g. Europe/Lisbon",
    smsNote: "Nothing on this platform sends SMS. A phone number here is directory information, not a second factor.",
    noChanges: "There are no unsaved changes",
    languageNotMirrored: "Saved. The sign-in pages could not be switched to this language and may still appear in the previous one.",
  },
  security: {
    title: "Sign-in and security",
    subtitle: "Your password, your second factors and the computers you trust",
    note: "Your password, passkeys, authenticator app and recovery codes have a page of their own. Every change there asks you to confirm your second factor first.",
    open: "Manage sign-in and security",
  },
} as const;
