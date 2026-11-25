export const setup = {
  page: {
    title: "Set up this installation",
    intro:
      "Nobody administers this installation yet. Enter the install code from the server log, and the first administrator account will be created for you.",
    installCode: "Install code",
    yourName: "Your name",
    yourEmail: "Your email address",
    organizationName: "Organization name",
    settingUp: "Setting up…",
    finish: "Finish setup",
    // Finish setup pressed with a field empty (coordinator ruling 8a: the button stays on);
    // {fields} is the empty fields' labels as the language lists them (ruling 8: named).
    incomplete: {
      one: "Fill in this field: {fields}.",
      other: "Fill in these fields: {fields}.",
    },
    passwordHint: "The first administrator signs in with this address and this password, and adds a second factor at the first sign-in.",
    unreachable: "The server could not be reached, so nothing was created. {reason}",
  },
} as const;
