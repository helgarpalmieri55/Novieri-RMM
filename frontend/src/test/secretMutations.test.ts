/**
 * NO CONSOLE MUTATION LEAVES A SECRET IN TANSTACK'S MUTATION CACHE (operator
 * decision 4, 2026-11-05, "Barrer todas ahora"; final review D2, Minor 1).
 *
 * TanStack keeps a mutation's body (`state.variables`) and answer
 * (`state.data`) while the page that ran it is mounted, and for five minutes
 * after. The rule, after 60cf695: a mutation whose body or answer carries a
 * secret is `useSecretMutation` (`lib/secretMutation.ts`: `gcTime: 0`, the
 * page's callbacks first, then `reset()` once it settles), or one of the
 * seven sites 60cf695 fixed by hand, each with its own test.
 *
 * THREE WAYS THIS GOES RED, so a new one cannot slip in:
 *
 *   1. a mutation of a SECRET-BEARING METHOD that is neither;
 *   2. a secret-bearing mutation site that is not in `SECRET_SITES` -- the
 *      list below, which IS the reviewed inventory;
 *   3. a mutation of an `ApiClient` method whose body or answer has a field
 *      named like a secret (`SECRET_FIELDS`) that nobody has classified: it
 *      goes in `SECRET_METHODS` or, with the reason, in `NOT_SECRET`.
 *
 * Read from the source, as `api.session.test.ts` and `globalWrites.test.tsx`
 * are: what is pinned is the code, before any page runs it.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = resolve(import.meta.dirname, "..");

/** The `ApiClient` methods whose body or answer carries a secret. */
const SECRET_METHODS: Record<string, string> = {
  // A secret SENT, in the body.
  changePassword: "both passwords",
  enrollTotpConfirm: "a code from the authenticator app",
  updateEmailSettings: "the relay password, or the token client secret",
  createNotificationChannel: "a webhook secret, an SMTP password or a Microsoft client secret",
  updateNotificationChannel: "a webhook secret, an SMTP password or a Microsoft client secret",
  createEmailMailbox: "the IMAP password",
  updateEmailMailbox: "the IMAP password",
  saveEmailOAuthApp: "the OAuth client secret",
  completeMailboxOAuth: "the OAuth authorization code",
  createOffsiteTarget: "the object store's secret access key",
  updateOffsiteTarget: "the object store's secret access key",
  createLicense: "the license key",
  updateLicense: "the license key",
  // Not visible to check 3: inside `post_config`, a `Record<string, unknown>`.
  createProvisionProfile: "the domain-join and local-admin passwords",
  updateProvisionProfile: "the domain-join and local-admin passwords",
  // A secret ANSWERED, shown once.
  checkoutCredential: "the vault secret",
  enrollTotpStart: "the authenticator secret, in the otpauth:// URI",
  regenerateRecoveryCodes: "ten recovery codes",
  inviteUser: "the one-time invitation link, when no mail is configured",
  resendInvite: "the one-time invitation link, when no mail is configured",
  revealLicenseKey: "the license key",
  createEnrollmentToken: "the enrollment token",
  mintAgentDeploymentToken: "the deployment's enrollment token",
  generateAgentInstallCommand: "the install command's enrollment token",
  saveInstallLink: "the install link, with its code, when one is generated",
  regenerateInstallLink: "the new install link, with its code",
};

/** Named like a secret, carrying none: reviewed, with the reason. */
const NOT_SECRET: Record<string, string> = {
  createCredential:
    "the secret is sent from the mutationFn's closure -- `create.mutate()` takes no variables -- and the answer has no secret field (D2, Minor 1)",
  downloadInstallLinkMsi:
    "the install link's code travels from the mutationFn's closure -- `download.mutate(arch)` -- and what the page keeps of the answer is the file's name and digest, never the bytes",
  createDocument: "a document's `links` name records it is linked to, not addresses",
  updateDocument: "a document's `links` name records it is linked to, not addresses",
  addDocumentLink: "`link` is a record type and id",
  registerRecoveryKey: "a PUBLIC key and the challenge for its proof; the private half never reaches the console",
  newRecoveryKeyChallenge: "a challenge for a public key already registered",
  createCostCenter: "`code` is the cost centre's accounting code",
  updateCostCenter: "`code` is the cost centre's accounting code",
  createBudgetCategory: "`key` names the category",
  updateBudgetCategory: "`key` names the category",
  createTicketCategory: "`key` names the category",
  updateTicketCategory: "`key` names the category",
  createTicketQueue: "`key` names the queue",
  updateTicketQueue: "`key` names the queue",
  createApprovedAction: "`key` names the approved action",
  updateApprovedAction: "`key` names the approved action",
  exemptApprovedActionScript: "`key` names the approved action",
  revokeApprovedActionScriptExemption: "`key` names the approved action",
  startChecklist: "`key` names a BCP checklist step",
  closeChecklist: "`key` names a BCP checklist step",
  updateChecklistStep: "`key` names a BCP checklist step",
  upsertStep: "`key` names a BCP plan step",
  exportReport: "`key` names the report",
};

/**
 * THE SECRET-BEARING MUTATION SITES, by file and method: the inventory. A new
 * one fails check 2 until it is added here, and check 1 until it is safe.
 * `reviewed` marks the seven 60cf695 fixed by hand, `useMutation` with
 * `gcTime: 0` and a `reset()` in the page, each pinned by the test named.
 */
const SECRET_SITES: Record<string, { reviewed?: string }> = {
  "modules/profile/api.ts changePassword": { reviewed: "modules/profile/SecurityPage.test.tsx" },
  "modules/profile/api.ts enrollTotpStart": { reviewed: "modules/profile/SecurityPage.test.tsx" },
  "modules/profile/api.ts enrollTotpConfirm": { reviewed: "modules/profile/SecurityPage.test.tsx" },
  "modules/profile/api.ts regenerateRecoveryCodes": { reviewed: "modules/profile/SecurityPage.test.tsx" },
  "modules/settings/api.ts inviteUser": { reviewed: "modules/settings/__tests__/users.test.tsx" },
  "modules/settings/api.ts resendInvite": { reviewed: "modules/settings/__tests__/users.test.tsx" },
  "modules/credentials/CredentialsPage.tsx checkoutCredential": { reviewed: "modules/credentials/CredentialsPage.test.tsx" },
  "modules/settings/api.ts updateEmailSettings": {},
  "modules/settings/api.ts createNotificationChannel": {},
  "modules/settings/api.ts updateNotificationChannel": {},
  "modules/notification/api.ts createNotificationChannel": {},
  "modules/notification/api.ts updateNotificationChannel": {},
  "modules/email-intake/api.ts createEmailMailbox": {},
  "modules/email-intake/api.ts updateEmailMailbox": {},
  "modules/email-intake/api.ts saveEmailOAuthApp": {},
  "modules/email-intake/api.ts completeMailboxOAuth": {},
  "modules/backup/api.ts createOffsiteTarget": {},
  "modules/backup/api.ts updateOffsiteTarget": {},
  "modules/licenses/api.ts createLicense": {},
  "modules/licenses/api.ts updateLicense": {},
  "modules/licenses/api.ts revealLicenseKey": {},
  "modules/provisioning/api.ts createProvisionProfile": {},
  "modules/provisioning/api.ts updateProvisionProfile": {},
  "modules/enrollment/EnrollmentPage.tsx createEnrollmentToken": {},
  "modules/agent-releases/ManagedDeploymentDialog.tsx mintAgentDeploymentToken": {},
  "modules/agent-releases/InstallLinkPanel.tsx saveInstallLink": {},
  "modules/agent-releases/InstallLinkPanel.tsx regenerateInstallLink": {},
  "modules/agent-releases/AgentReleasesPage.tsx generateAgentInstallCommand": {},
};

/** Field names that may hold a secret, in a body or an answer. */
const SECRET_FIELDS = new Set([
  "password",
  "current",
  "new",
  "smtp_password",
  "secret",
  "client_secret",
  "microsoft_client_secret",
  "token_client_secret",
  "secret_access_key",
  "token",
  "key",
  "code",
  "url",
  "link",
  "command",
  "recovery_codes",
  "otpauth_uri",
]);

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    return /\.tsx?$/.test(name) && !/\.test\./.test(name) ? [path] : [];
  });
}

/** The text between the bracket at `open` and the one that closes it. */
function balanced(text: string, open: number, pair: "()" | "{}"): string {
  let depth = 0;
  for (let index = open; index < text.length; index += 1) {
    if (text[index] === pair[0]) depth += 1;
    else if (text[index] === pair[1]) {
      depth -= 1;
      if (depth === 0) return text.slice(open + 1, index);
    }
  }
  return text.slice(open + 1);
}

interface Site {
  file: string;
  hook: "useMutation" | "useSecretMutation";
  options: string;
  methods: string[];
}

const FILES = sources(SRC);

/** Every mutation in the console, with the `ApiClient` methods its options call. */
const SITES: Site[] = FILES.flatMap((path) => {
  const text = readFileSync(path, "utf8");
  const file = relative(SRC, path);
  return [...text.matchAll(/\b(useMutation|useSecretMutation)\s*(?:<[^(]*?>)?\s*\(/g)].map((match) => {
    const options = balanced(text, (match.index ?? 0) + match[0].length - 1, "()");
    const direct = [...options.matchAll(/\bapi\.(\w+)\(/g)].map((m) => m[1]!);
    // A module's own wrapper, named after the method it calls: `createEnrollmentToken(api, ...)`.
    const wrapped = [...options.matchAll(/\b(\w+)\(api[,)]/g)].map((m) => m[1]!);
    return { file, hook: match[1] as Site["hook"], options, methods: [...new Set([...direct, ...wrapped])] };
  });
});

/** Every interface and object type in the console, by name. */
const TYPES = new Map<string, string[]>();
for (const path of FILES) {
  const text = readFileSync(path, "utf8");
  for (const match of text.matchAll(/\binterface (\w+)(?:<[^>{]*>)?(?: extends ([^{]+))?\s*\{/g)) {
    const body = balanced(text, (match.index ?? 0) + match[0].length - 1, "{}");
    TYPES.set(match[1]!, [...(TYPES.get(match[1]!) ?? []), `${body} ${match[2] ?? ""}`]);
  }
  for (const match of text.matchAll(/\btype (\w+)(?:<[^>=]*>)? = \{/g)) {
    const body = balanced(text, (match.index ?? 0) + match[0].length - 1, "{}");
    TYPES.set(match[1]!, [...(TYPES.get(match[1]!) ?? []), body]);
  }
  // `Original as Alias`, in an import list.
  for (const match of text.matchAll(/\b(\w+) as (\w+)\b/g)) {
    if (!TYPES.has(match[2]!) && /^[A-Z]/.test(match[1]!)) TYPES.set(match[2]!, [match[1]!]);
  }
}

/** The field names a type text declares, inline or by name, two levels down. */
function fieldsOf(typeText: string, seen = new Set<string>(), depth = 0): Set<string> {
  const plain = typeText.replace(/\/\*[\s\S]*?\*\/|\/\/[^\n]*/g, "");
  const found = new Set([...plain.matchAll(/(?:^|[{;,(\n])\s*(?:readonly\s+)?(\w+)\??\s*:/g)].map((m) => m[1]!));
  if (depth > 2) return found;
  for (const name of new Set([...plain.matchAll(/\b([A-Z]\w+)\b/g)].map((m) => m[1]!))) {
    if (seen.has(name)) continue;
    seen.add(name);
    for (const body of TYPES.get(name) ?? []) for (const field of fieldsOf(body, seen, depth + 1)) found.add(field);
  }
  return found;
}

/** `ApiClient`'s methods, each with its parameter list and its answer's type. */
const API = readFileSync(join(SRC, "lib/api.ts"), "utf8");
const METHODS = new Map<string, string>(
  [...API.matchAll(/^ {2}(?:async )?(\w+)\(([\s\S]*?)\)(?::\s*Promise<([\s\S]*?)>)?\s*\{$/gm)].map((match) => [
    match[1]!,
    `(${match[2] ?? ""}) ${match[3] ?? ""}`,
  ]),
);

const siteName = (site: Site, method: string) => `${site.file} ${method}`;
const secretSites = SITES.flatMap((site) =>
  site.methods.filter((method) => method in SECRET_METHODS).map((method) => ({ site, method })),
);

describe("the secret-bearing mutations", () => {
  it("are found at all: the scan reads the console's mutations", () => {
    // Non-vacuity: a regex that matched nothing would pass every check below.
    expect(SITES.length).toBeGreaterThan(250);
    expect(METHODS.size).toBeGreaterThan(400);
    expect(secretSites.length).toBe(Object.keys(SECRET_SITES).length);
  });

  it("are the listed sites, and no other (check 2)", () => {
    expect([...new Set(secretSites.map(({ site, method }) => siteName(site, method)))].sort()).toEqual(
      Object.keys(SECRET_SITES).sort(),
    );
  });

  it("each forget their secret: useSecretMutation, or 60cf695's gcTime 0 and reset with its test (check 1)", () => {
    const unsafe = secretSites
      .filter(({ site, method }) => {
        if (site.hook === "useSecretMutation") return false;
        const reviewed = SECRET_SITES[siteName(site, method)]?.reviewed;
        return reviewed === undefined || !/\bgcTime:\s*0\b/.test(site.options);
      })
      .map(({ site, method }) => siteName(site, method));
    expect(unsafe).toEqual([]);
  });

  it("name a test for every site fixed by hand, and that test reads the mutation cache", () => {
    for (const { reviewed } of Object.values(SECRET_SITES)) {
      if (!reviewed) continue;
      expect(readFileSync(join(SRC, reviewed), "utf8")).toMatch(/mutationCacheText|getMutationCache\(\)/);
    }
  });

  it("are every mutation whose body or answer is named like a secret, or that one is reviewed (check 3)", () => {
    const called = new Set(SITES.flatMap((site) => site.methods).filter((method) => METHODS.has(method)));
    const unclassified = [...called]
      .filter((method) => [...fieldsOf(METHODS.get(method)!)].some((field) => SECRET_FIELDS.has(field)))
      .filter((method) => !(method in SECRET_METHODS) && !(method in NOT_SECRET))
      .sort();
    expect(unclassified).toEqual([]);
  });

  it("keep both lists honest: every entry is a method a mutation calls", () => {
    const called = new Set(SITES.flatMap((site) => site.methods));
    expect(Object.keys(SECRET_METHODS).filter((method) => !called.has(method))).toEqual([]);
    expect(Object.keys(NOT_SECRET).filter((method) => !called.has(method))).toEqual([]);
  });
});
