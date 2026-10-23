/**
 * Permission codes as the thing they let a person do.
 *
 * A code (`devices.read`) is what the API checks and what an administrator
 * ticks on the role screen; it is not a sentence. Copy that tells someone what
 * they are missing uses these phrases, and the code, where it is shown at all,
 * is secondary monospace text.
 */

import { translate, type MessageKey } from "../i18n/core";
import { conjunctionBefore } from "./joinWords";

const PHRASES: Readonly<Record<string, MessageKey>> = {
  "agent.release.read": "lib.permission.agentReleaseRead",
  "agent.push.run": "lib.permission.agentPushRun",
  "agent.push.scope.manage": "lib.permission.agentPushScopeManage",
  "audit.read": "lib.permission.auditRead",
  "backup.read": "lib.permission.backupRead",
  "bcp.read": "lib.permission.bcpRead",
  "budget.manage": "lib.permission.budgetManage",
  "budget.read": "lib.permission.budgetRead",
  "change.approve": "lib.permission.changeApprove",
  "change.read": "lib.permission.changeRead",
  "credentials.read": "lib.permission.credentialsRead",
  "deploy.read": "lib.permission.deployRead",
  "devices.read": "lib.permission.devicesRead",
  "devices.review": "lib.permission.devicesReview",
  "enrollment.link.manage": "lib.permission.enrollmentLinkManage",
  "health.read": "lib.permission.healthRead",
  "ipam.assign": "lib.permission.ipamAssign",
  "ipam.manage": "lib.permission.ipamManage",
  "ipam.read": "lib.permission.ipamRead",
  "ipam.scope.manage": "lib.permission.ipamScopeManage",
  "ipam.sweep.run": "lib.permission.ipamSweepRun",
  "jobs.read": "lib.permission.jobsRead",
  "licenses.read": "lib.permission.licensesRead",
  "patch.read": "lib.permission.patchRead",
  "people.read": "lib.permission.peopleRead",
  "platform.errors.write": "lib.permission.platformErrorsWrite",
  "site.read": "lib.permission.siteRead",
  "sla.read": "lib.permission.slaRead",
  "subscriptions.read": "lib.permission.subscriptionsRead",
  "ticket.read": "lib.permission.ticketRead",
  "users.read": "lib.permission.usersRead",
};

/** "view devices", or the code's own words for one this map does not know. */
export function permissionPhrase(code: string): string {
  const known = PHRASES[code];
  if (known) return translate(known);
  const parts = code.split(".");
  const what = parts.slice(0, -1).join(" ").replace(/_/g, " ");
  return translate(parts[parts.length - 1] === "read" ? "lib.permission.viewOther" : "lib.permission.manageOther", { what });
}

/** "permission to view devices and view updates". */
export function permissionsSentence(codes: readonly string[]): string {
  const phrases = codes.map(permissionPhrase);
  if (phrases.length === 0) return translate("lib.permission.sentenceNone");
  if (phrases.length === 1) return translate("lib.permission.sentenceOne", { phrase: phrases[0] as string });
  const last = phrases[phrases.length - 1] as string;
  return translate("lib.permission.sentenceMany", {
    first: phrases.slice(0, -1).join(", "),
    and: conjunctionBefore(last),
    last,
  });
}
