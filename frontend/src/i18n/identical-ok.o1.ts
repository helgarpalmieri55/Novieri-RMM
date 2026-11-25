/**
 * IDENTICAL_OK entries of wave O1 (Task 6): the catalogues `devices`, `inventory`, `onboarding`, `agentPush`, `agentReleases`.
 * The wave adds and removes its own entries here and nowhere else, so the eight
 * waves that run in parallel never edit one file (redesign plan, "Archivos
 * compartidos"); `catalogue.identical.test.ts` holds each file to its catalogues.
 */
export const IDENTICAL_OK_O1: Record<string, string> = {
  "devices.addAsset.namePlaceholder": "An example hostname, a technical identifier written the same in Spanish.",
  "devices.pushDetails.dnsPlaceholder": "An example fully qualified domain name, a technical identifier written the same in Spanish.",
  "devices.candidates.mac": "\"MAC\" is an acronym (media access control), written the same in Spanish.",
  "devices.suggestions.ip": "\"IP\" is an acronym, written the same in Spanish.",
  "devices.groups.editor.no": "\"No\" is the same word in Spanish.",
  "devices.groups.editor.parentOfClient": "Only placeholders (the group's name and its client's) and a separator, the same in Spanish.",
  "agentReleases.platform.darwinArm64": "\"Apple silicon\" is Apple's name for its processors, written the same in Spanish.",
  "agentReleases.platform.darwinAmd64": "\"Intel\" is a company name, written the same in Spanish.",
  "agentReleases.managed.intune": "\"Intune\" is a Microsoft product name, written the same in Spanish.",
  "agentReleases.newRollout.versionOption": "Only placeholders: a version and its channel word, translated where they are supplied.",
  "agentPush.preflight.winrm": "\"WinRM\" is the protocol's name (Windows Remote Management), written the same in Spanish.",
  "agentPush.preflight.https": "A protocol and port with a placeholder for the already-translated answer: the same in Spanish.",
  "agentPush.preflight.http": "A protocol and port with a placeholder for the already-translated answer: the same in Spanish.",
  "agentPush.run.outcomeDetail": "Only placeholders: the outcome and the server's detail, both worded where they are supplied.",
  "inventory.hardware.total": "\"Total\" is the same word in Spanish.",
  "inventory.detail.tabSoftware": "\"Software\" is the word Spanish uses for software.",
  "inventory.detail.tabHardware": "\"Hardware\" is the word Spanish uses for hardware.",
  "inventory.detail.factCpu": "\"CPU\" is an acronym, written the same in Spanish.",
};
