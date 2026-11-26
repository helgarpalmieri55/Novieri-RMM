/**
 * IDENTICAL_OK entries of wave O4 (Task 9): the catalogues `backup`, `bcp`, `provisioning`, `siteServers`, `enrollment`.
 * The wave adds and removes its own entries here and nowhere else, so the eight
 * waves that run in parallel never edit one file (redesign plan, "Archivos
 * compartidos"); `catalogue.identical.test.ts` holds each file to its catalogues.
 */
export const IDENTICAL_OK_O4: Record<string, string> = {
  "enrollment.tokens.token": "\"Token\" is the loanword Spanish uses for a bootstrap token, as in the console's \"Tokens de inscripción\".",
  "siteServers.roles.wake": "\"Wake-on-LAN\" is the name of the technology, written the same in Spanish.",
  "provisioning.runs.colError": "\"Error\" is the same word in Spanish.",
  "provisioning.images.sha": "Only a placeholder and the hash algorithm's name \"sha256\", written the same in Spanish.",
  "backup.window.sentence": "Only placeholders: the days and the hours, both worded where they are supplied.",
  "backup.offsite.targetBucket": "\"Bucket\" is the loanword Spanish IT uses for an S3 bucket.",
  "backup.offsite.bucket": "\"Bucket\" is the loanword Spanish IT uses for an S3 bucket.",
  "bcp.plans.rto": "Only a placeholder, the acronym \"RTO\" and the unit abbreviation \"min\", written the same in Spanish.",
  "bcp.plans.rpo": "Only a placeholder, the acronym \"RPO\" and the unit abbreviation \"min\", written the same in Spanish.",
  "bcp.detail.planFallback": "\"Plan\" is the same word in Spanish.",
  "bcp.detail.rto": "\"RTO\" is an acronym (recovery time objective), written the same in Spanish.",
  "bcp.detail.rpo": "\"RPO\" is an acronym (recovery point objective), written the same in Spanish.",
  "bcp.detail.minutes": "Only a placeholder and the unit abbreviation \"min\", written the same in Spanish.",
  "bcp.detail.planPanel": "\"Plan\" is the same word in Spanish.",
  "bcp.recorder.no": "\"No\" is the same word in Spanish.",
  "bcp.report.plan": "\"Plan\" is the same word in Spanish.",
  "bcp.report.no": "\"no\" is the same word in Spanish.",
};
