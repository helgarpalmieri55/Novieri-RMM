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
    passwordHint: "You will be sent a link to choose a password. No password is entered here.",
    unreachable: "The server could not be reached, so nothing was created. {reason}",
  },
  claim: {
    title: "Setup complete",
    belongsTo: "This installation now belongs to your organization, and <b>{email}</b> is its administrator.",
    checkInbox: "Check that inbox for a link to choose a password, then sign in.",
    invitationFailed: "The invitation email could not be sent, so no sign-in link has arrived.",
    goToSignIn: "Go to sign in",
    signInFailed: "Sign-in could not be started. {reason}",
  },
} as const;
