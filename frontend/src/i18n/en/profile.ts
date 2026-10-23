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
    subtitle: "Passwords and keys, handled where they belong",
    note: "Each of these runs one step on the sign-in pages and returns you to ITMS. Passwords, second factors and passkeys are never handled by this platform.",
    startFailed: "That step could not be started, so nothing about your sign-in was changed. {reason}",
    changePassword: {
      label: "Change password",
      hint: "You will be asked for your current password first. This platform never sees either one.",
    },
    manageMfa: {
      label: "Manage MFA device",
      hint: "Add, replace or remove your second factor.",
    },
    addPasskey: {
      label: "Add a passkey",
      hint: "A passwordless credential bound to this device.",
    },
  },
} as const;
