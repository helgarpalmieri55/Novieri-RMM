/**
 * Credential types, as a person reads them.
 *
 * WHY THIS FILE (UX remediation cycle 6's neighbour — the missing enum
 * presentation layer). Auditor A, [MEDIUM], /credentials → New credential →
 * Type: "The options are `windows_admin`, `ssh_key`, `domain_join` — unmapped
 * database enum values with underscores ... Here it also hides meaning that
 * matters: an operator choosing between 'ssh_key' and 'domain_join' gets no
 * explanation of what each type implies for how the secret is used or where it
 * can be checked out."
 *
 * So the map carries a DESCRIPTION as well as a label. A pretty-printer that
 * only turned `ssh_key` into "SSH key" would close the typography half of the
 * finding and none of the half the auditor called out as the one that matters.
 *
 * UNKNOWN VALUES ARE HUMANISED, NOT DROPPED. The server owns this enum and may
 * grow it (`docs/03-security-model.md` "Credential vault"); a console that
 * rendered a blank for a type it had not been taught would hide which
 * credential a row is, which is worse than the snake_case it replaces.
 */

import { translate } from "../../i18n/core";
import { humaniseEnum } from "../../lib/labels";

/**
 * The types the console has words for. The union is read by
 * `enums.coverage.test.ts`, which holds `enums.CredentialType.<value>` to
 * exist in both languages. The label and the description live in the
 * catalogue (`enums.CredentialType`, `credentials.types`), looked up when a
 * label is drawn and never held here, so a language switch re-words them.
 */
export type CredentialType = "windows_admin" | "ssh_key" | "domain_join" | "push_install" | "windows_domain";

export interface CredentialTypeOption {
  value: CredentialType;
}

// `push_install` was added with agent push install cycle B1 (`CLAUDE.md`
// invariant 2a, `docs/modules/agent-distribution/spec-push-install.md` §6.2).
//
// ITS DESCRIPTION CARRIES A WARNING BECAUSE THE PLATFORM CANNOT CARRY THE
// CONTROL. It cannot check what rights an account holds in somebody else's
// directory, and §6.2 makes "not a domain admin" a precondition for the
// feature existing at all. SCCM's client-push account is a documented
// lateral-movement enabler precisely because everybody made it a domain admin
// and pointed it at every subnet. What this console can do is refuse to
// pretend the choice does not matter, at the moment the choice is made.
//
// `windows_domain` (WinRM push, design §2.5) carries the same standing
// warning for the same reason: a Domain Admin used here means a compromised
// site server during a push is a compromised domain (design §2.3 "What
// remains"). What the console can do is say so at the point of choice.
export const CREDENTIAL_TYPES: readonly CredentialTypeOption[] = [
  { value: "windows_admin" },
  { value: "ssh_key" },
  { value: "domain_join" },
  { value: "push_install" },
  { value: "windows_domain" },
];

/**
 * The Kerberos realm a `windows_domain` account names ON ITS OWN, upper-cased,
 * or null when it names none (design §2.5, "the realm is derived from the
 * domain and shown for confirmation").
 *
 * THIS MIRRORS THE AGENT'S `RealmOfAccount`, AND THAT IS WHY `DOMAIN\user` IS
 * NULL. A UPN (`user@corp.example.com`) carries its realm. A down-level
 * `CORP\user` carries a NetBIOS name, which is NOT a Kerberos realm -- and
 * since review I1 such an account is refused rather than given the realm of
 * each target's DNS name. Showing "CORP" here would ask the operator to
 * confirm a realm no push would ever use.
 */
export function windowsDomainRealm(account: string): string | null {
  const trimmed = account.trim();
  const at = trimmed.indexOf("@");
  if (at < 0) return null;
  const domain = trimmed.slice(at + 1);
  return domain ? domain.toUpperCase() : null;
}

const isKnown = (value: string): value is CredentialType => CREDENTIAL_TYPES.some((type) => type.value === value);

/** `ssh_key` -> "SSH key"; an unknown `smart_card` -> "Smart card". */
export function credentialTypeLabel(value: string): string {
  return humaniseEnum(value, "CredentialType");
}

/** The type as a noun in the middle of a sentence ("this ssh key"). */
export function credentialTypeNoun(value: string): string {
  if (isKnown(value)) return translate(`credentials.types.${value}.noun`);
  return credentialTypeLabel(value).toLowerCase();
}

export function credentialTypeDescription(value: string): string | null {
  return isKnown(value) ? translate(`credentials.types.${value}.description`) : null;
}
