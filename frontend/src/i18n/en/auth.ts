/**
 * The pages a person reaches without a session (own-auth design §2, §7):
 * signing in, the second factor, the first factor, the recovery codes, the
 * two password links and the invitation. `modules/auth/`.
 */
export const auth = {
  common: {
    email: "Email address",
    password: "Password",
    working: "Working…",
    startOver: "Start again",
    backToSignIn: "Back to sign-in",
    tooMany: "Too many attempts. Wait a minute and try again.",
    wait: {
      one: "Too many attempts. Wait {count} second and try again.",
      other: "Too many attempts. Wait {count} seconds and try again.",
    },
    timedOut: "This sign-in has timed out. Start again from the sign-in page.",
  },
  signIn: {
    title: "Sign in",
    intro: "Use the email address and password of your account. Your second factor comes next.",
    submit: "Continue",
    rejected: "That email address and password do not match an account here.",
    forgot: "Forgot your password?",
  },
  portalSignIn: {
    title: "Sign in to the portal",
    intro: "Use a passkey, or your email address and password.",
    usePasskey: "Sign in with a passkey",
    passkeyRejected: "That passkey was not accepted. Try again, or use your email address and password.",
  },
  factor: {
    title: "Confirm it is you",
    intro: "Use your second factor to finish signing in.",
    passkeyTitle: "Passkey",
    passkeyHint: "The passkey on this device, on your phone or on a security key.",
    usePasskey: "Use a passkey",
    totpTitle: "Authenticator app",
    totpLabel: "Six-digit code from your authenticator app",
    totpSubmit: "Verify code",
    showRecovery: "Use a recovery code instead",
    recoveryTitle: "One of your recovery codes",
    recoveryLabel: "Recovery code",
    recoveryHint: "One of the ten codes you saved when you set up your second factor. Each one works once.",
    recoverySubmit: "Use recovery code",
    remember: {
      one: "Do not ask again on this computer for {count} day",
      other: "Do not ask again on this computer for {count} days",
    },
    rememberHint: "Leave this unticked on a computer other people use.",
    rejected: "That was not accepted. Try again.",
  },
  enroll: {
    title: "Add a second factor",
    intro:
      "Your account needs a second factor before the console opens. A passkey is the quickest; an authenticator app works on any phone.",
    passkeyTitle: "Passkey (recommended)",
    passkeyHint: "Kept on this device, on your phone or on a security key, and unlocked with your fingerprint, face or PIN.",
    passkeyName: "Name for this passkey",
    passkeyDefaultName: "My passkey",
    createPasskey: "Create a passkey",
    totpTitle: "Authenticator app",
    totpHint: "Any authenticator app that shows six-digit codes that change every 30 seconds.",
    startTotp: "Set up an authenticator app",
    setupKey: "In your app, add an account and enter this setup key:",
    openInApp: "Open in an authenticator app",
    totpLabel: "Six-digit code the app now shows",
    confirmTotp: "Confirm code",
    rejected: "That code was not accepted. Check the time on your phone and enter the newest code.",
    passkeyRejected: "That passkey was not accepted. Try again, or use an authenticator app.",
    factorSaved: "Your second factor is saved. The recovery codes could not be made yet.",
    retryFinish: "Try again",
    // `enroll/finish` answered 409 `already_signed_in`: its first answer was lost after it succeeded.
    alreadySignedIn:
      "You are already signed in: the first answer was lost on its way. Your recovery codes were made but cannot be shown again; make new ones in Your security.",
    // `enroll/finish` answered 409 `enroll_required`: more than 15 minutes since the factor was saved.
    finishExpired:
      "The time to finish this step has run out. Start again from the sign-in page: you will sign in with the second factor you just added, and can make recovery codes in Your security.",
  },
  codes: {
    title: "Save your recovery codes",
    intro:
      "If you lose your second factor, each of these codes signs you in once. Keep them somewhere safe, away from this computer: they are shown only now.",
    list: "Your recovery codes",
    copy: "Copy the codes",
    copied: "The codes are on the clipboard.",
    copyFailed: "The browser would not copy them. Select the codes and copy them yourself.",
    saved: "I have saved these codes somewhere safe",
    continue: "Continue to the console",
    none: "There are no codes to show. Recovery codes appear once, right after they are made; you can make new ones in Sign-in and security.",
  },
  forgot: {
    title: "Reset your password",
    intro: "Enter the address you sign in with. If it belongs to an account here, a link to choose a new password is sent to it.",
    submit: "Send the link",
    sent: "If that address belongs to an account here, a link is on its way. It works once, for 30 minutes. Your second factor is still asked for afterwards.",
  },
  reset: {
    title: "Choose a new password",
    submit: "Change password",
    done: "Your password was changed. Sign in with it; your second factor is still asked for.",
    signIn: "Sign in",
    invalid: "This link has expired or was already used.",
    askAgain: "Ask for a new link",
  },
  invite: {
    heading: "Your invitation",
    loading: "Opening your invitation…",
    title: "Welcome, {name}",
    intro: "You were invited to this console as <b>{email}</b>. Choose a password; adding your second factor comes next.",
    submit: "Set password and continue",
    invalid: "This invitation has expired, was already used, or was cancelled. Ask the person who invited you for a new one.",
    introContact: "You were invited to the support portal as <b>{email}</b>. Choose a password, or use a passkey.",
    usePasskey: "Use a passkey instead",
    passkeyName: "Name for this passkey",
    or: "or",
  },
  recover: {
    heading: "Account recovery",
    loading: "Opening your recovery link…",
    title: "Recover your account",
    intro: "This link was made on the server for <b>{email}</b>.",
    what: "Recovering removes this account's second factors and recovery codes. You then add a new second factor and save new recovery codes before the console opens.",
    // The details said an emailed link set the password (operator decision 2026-11-05).
    newPassword:
      "Your password was last set with an emailed link. Choose a new password to finish recovering your account; the old one stops working.",
    // Every other recovery offers one (operator decision 2026-11-05, «Ofrecerla siempre, aplicar»).
    optionalPassword:
      "Optional: set a new password now, and the old one stops working. Leave both fields empty to keep your current password.",
    submit: "Recover my account",
    invalid: "This recovery link has expired or was already used. Someone with access to the server can make a new one.",
  },
  password: {
    new: "New password",
    confirm: "The same password again",
    rules: "At least {min} characters, any you like, spaces included. A phrase you can remember works well.",
    mismatch: "The two passwords are not the same.",
    too_short: "Use at least {min} characters.",
    too_long: "Use at most 128 characters.",
  },
  passkey: {
    unsupported: "This browser cannot use passkeys. Use an authenticator app instead.",
    // The account has a passkey this browser cannot use, so the option is not
    // shown (`passkey.ts::passkeyHiddenAdvice`): why, and what is left.
    hiddenUseAppOrCode:
      "This browser cannot use passkeys, so your passkey is not offered here. Use your authenticator app or one of your recovery codes.",
    hiddenUseApp: "This browser cannot use passkeys, so your passkey is not offered here. Use your authenticator app.",
    hiddenUseCode:
      "This browser cannot use passkeys, so your passkey is not offered here. Use one of your recovery codes, or a browser that supports passkeys.",
    hiddenNoOther:
      "This browser cannot use passkeys, and your account has no other second factor. Sign in from a browser that supports passkeys, or ask an administrator to reset your second factors.",
    cancelled: "The passkey prompt was closed or timed out, so nothing was used.",
    alreadyRegistered: "This device already holds a passkey for your account.",
    noUserVerification:
      "This passkey or device cannot confirm it is you with a fingerprint, face or PIN, which is required. Use another passkey or an authenticator app.",
    aborted: "Another passkey request replaced this one. Try again.",
    generic: "The passkey could not be used on this device. Try again, or use another method.",
  },
} as const;
