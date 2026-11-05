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
 * FIVE WAYS THIS GOES RED, so a new one cannot slip in:
 *
 *   1. a mutation of a SECRET-BEARING METHOD that is neither;
 *   2. a secret-bearing mutation site that is not in `SECRET_SITES` -- the
 *      list below, which IS the reviewed inventory;
 *   3. a mutation of an `ApiClient` method whose body or answer has a field
 *      named like a secret BY RULE (`SECRET_FIELD`: a password, passphrase,
 *      secret, token, key, code, pin, psk or otp suffix, or `private`) that
 *      nobody has classified: it goes in `SECRET_METHODS` or, with the
 *      reason, in `NOT_SECRET`;
 *   4. a mutation whose options hand `api` to a function the scan cannot see
 *      into, or call no `ApiClient` method at all (`OPAQUE_SITES`): a secret
 *      method behind a local helper is invisible to 1 and 3;
 *   5. a mutation of a method whose body the scan cannot read to the end --
 *      `Record<…>`, `unknown`, a type it finds no declaration of -- unless it
 *      is reviewed with what that body holds (`OPAQUE_BODIES`).
 *
 * The reviewer's probes that went through the earlier guard (final
 * re-review, I2) are kept as its own tests, run through the same `audit`.
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
  acceptRecovery: "the recovery link's token, and a new password while an emailed link set the current one",
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
  // Named like a secret by the suffix rule (final re-review, I2); none is one.
  confirmExpense: "`period_key` names a budget period",
  createExpense: "`period_key` names a budget period",
  updateExpense: "`period_key` names a budget period",
  voidExpense: "`period_key` names a budget period",
  uploadExpenseAttachment: "`period_key` names a budget period; the file is a receipt",
  createAiSession: "`total_input_tokens` and `total_output_tokens` count a model's tokens",
  createAiTurn: "`input_tokens` and `output_tokens` count a model's tokens",
  createRole: "`codes` are permission codes",
  updateRole: "`codes` are permission codes",
  setRolePermissions: "`codes` are permission codes",
  proveRecoveryKey: "a PUBLIC key, and the answer to a one-time challenge that proves the private half, which never reaches the console",
  rejectAiAction: "`approved_action_key` names an approved action",
  testNotificationChannel: "`status_code` is the HTTP status a test send got back",
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
  "modules/auth/RecoverPage.tsx acceptRecovery": {},
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

/**
 * A FIELD THAT MAY HOLD A SECRET, BY RULE, NOT BY LIST (final re-review, I2).
 * The list it replaces had to name `smtp_password`, `client_secret` and
 * `secret_access_key` one by one, and let `api_key`, `access_token`,
 * `private_key` and `passphrase` -- the commonest names for a secret --
 * through. A suffix rule on snake_case names, plural allowed, and `private`
 * anywhere.
 */
const SECRET_FIELD = /(?:^|_)(?:password|passphrase|secret|token|key|code|pin|psk|otp)s?$|(?:^|_)private(?:_|$)/;

/**
 * Mutation sites whose options hand `api` to nothing the scan can follow, or
 * call no `ApiClient` method at all -- reviewed, with the reason. A new one
 * fails until it is here (check 4): a secret method called through a local
 * helper is invisible to checks 1 and 3.
 */
const OPAQUE_SITES: Record<string, string> = {
  "lib/secretMutation.ts": "the wrapper itself: its options are the caller's",
};

/**
 * Methods called by a mutation whose body names a type the scan cannot read
 * -- `Record<…>`, `unknown`, or a type it finds no declaration of -- reviewed,
 * with what the body holds (check 5). A secret inside such a body is
 * invisible to check 3.
 */
const OPAQUE_BODIES: Record<string, string> = {
  applyIpamImport: "a CSV file and its column mapping (`Record<string, string>`: a column to a field name)",
  previewIpamImport: "a CSV file and its column mapping (`Record<string, string>`: a column to a field name)",
  createBackupRepository: "`dedup_params`: the repository's deduplication settings; no credential travels in this body",
  createChange: "`affected`: the records a change touches, by type and id",
  updateChange: "`affected`: the records a change touches, by type and id",
  createDocRecord:
    "`fields`: a documentation record's own fields (service, vendor, location), typed as JSON; credentials are the vault's (`createCredential`), not documentation",
  createOsImage: "`params`: the console sends none (`ImagesPage`: family, name and kind only)",
  createPackage: "`detection_rule` and `uninstall_rule`: how a deployed package is found and removed",
  createPlan: "`dependencies`: the BCP plan's other plans, by id",
  updatePlan: "`dependencies`: the BCP plan's other plans, by id",
  createVendor: "`contacts`: names, emails and phones of a vendor's people",
  updateVendor: "`contacts`: names, emails and phones of a vendor's people",
  dispatchJob: "`params`: a job type's parameters; a job that needs a credential names it by id, and the vault holds it (invariant 3)",
  restoreBackupPoint: "`target`: where a backup point is restored to",
  startProvisionRun: "`wake_on_lan`: the target's hardware and broadcast address",
  updateCurrentOrg: "`settings`: the organization's settings, each a value of `OrgSettings`",
  upsertBackupPolicy: "`sources` and `retention_gfs`: what a policy backs up and how long it keeps it",
  enrollPasskey: "a WebAuthn attestation (`@simplewebauthn/browser`'s type): public-key material",
};

/** Type names that are the language's or the platform's, not the console's. */
const BUILTIN_TYPES = new Set([
  "Promise", "Record", "Partial", "Pick", "Omit", "Readonly", "ReadonlyArray", "Array", "Blob", "File", "FormData",
  "Date", "Response", "Request", "RequestInit", "HeadersInit", "BodyInit", "AbortSignal", "URLSearchParams",
  "Uint8Array", "ArrayBuffer", "Set", "Map", "Exclude", "Extract", "NonNullable", "Required", "ReturnType",
  "Parameters",
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

interface Source {
  /** Relative to `src/`. */
  file: string;
  text: string;
}

interface Site {
  file: string;
  hook: "useMutation" | "useSecretMutation";
  options: string;
  /** The `ApiClient` methods its options call: `api.<m>(`, or a module wrapper `<m>(api, …)` named after one. */
  methods: string[];
  /** Functions it hands `api` to that are not `ApiClient` methods: a helper the scan cannot follow. */
  handedTo: string[];
}

interface Audit {
  sites: Site[];
  methods: Map<string, string>;
  /** Check 1: secret sites that neither use `useSecretMutation` nor are a reviewed 60cf695 site. */
  unsafe: string[];
  /** Check 2: secret sites found, by name. */
  secretSiteNames: string[];
  /** Check 3: called methods whose fields read like a secret, in neither list. */
  unclassified: string[];
  /** Check 4: sites the scan cannot follow, not in `OPAQUE_SITES`. */
  opaqueSites: string[];
  /** Check 5: called methods whose body does not resolve, in no list. */
  unresolved: string[];
  /** Why each method was flagged: the fields that read like a secret, or the type that did not resolve. */
  why: Map<string, string>;
}

/** The whole guard, over the sources given -- the console's own, or a probe's. */
function audit(files: readonly Source[], api: string): Audit {
  const methods = new Map<string, string>(
    [...api.matchAll(/^ {2}(?:async )?(\w+)\(([\s\S]*?)\)(?::\s*Promise<([\s\S]*?)>)?\s*\{$/gm)].map((match) => [
      match[1]!,
      `(${match[2] ?? ""}) ${match[3] ?? ""}`,
    ]),
  );

  const types = new Map<string, string[]>();
  const add = (name: string, body: string) => types.set(name, [...(types.get(name) ?? []), body]);
  for (const { text } of [...files, { file: "lib/api.ts", text: api }]) {
    for (const match of text.matchAll(/\binterface (\w+)(?:<[^>{]*>)?(?: extends ([^{]+))?\s*\{/g)) {
      add(match[1]!, `${balanced(text, (match.index ?? 0) + match[0].length - 1, "{}")} ${match[2] ?? ""}`);
    }
    for (const match of text.matchAll(/\btype (\w+)(?:<[^>=]*>)? = ([^;]+);/g)) {
      const rest = match[2]!;
      add(match[1]!, rest.trimStart().startsWith("{") ? balanced(text, (match.index ?? 0) + match[0].indexOf("{"), "{}") : rest);
    }
    // `Original as Alias`, in an import list.
    for (const match of text.matchAll(/\b(\w+) as (\w+)\b/g)) {
      if (!types.has(match[2]!) && /^[A-Z]/.test(match[1]!)) add(match[2]!, match[1]!);
    }
  }

  const fieldsOf = (typeText: string, seen = new Set<string>(), depth = 0): Set<string> => {
    const plain = typeText.replace(/\/\*[\s\S]*?\*\/|\/\/[^\n]*/g, "");
    const found = new Set([...plain.matchAll(/(?:^|[{;,(\n])\s*(?:readonly\s+)?(\w+)\??\s*:/g)].map((m) => m[1]!));
    if (depth > 2) return found;
    for (const name of new Set([...plain.matchAll(/\b([A-Z]\w+)\b/g)].map((m) => m[1]!))) {
      if (seen.has(name)) continue;
      seen.add(name);
      for (const body of types.get(name) ?? []) for (const field of fieldsOf(body, seen, depth + 1)) found.add(field);
    }
    return found;
  };

  /** What stops a method's BODY (its parameters) being read to the end, or `null`. */
  const opaqueBody = (signature: string, seen = new Set<string>(), depth = 0): string | null => {
    const text = signature.replace(/\/\*[\s\S]*?\*\/|\/\/[^\n]*/g, "").replace(/"[^"]*"|'[^']*'/g, '""');
    const params = depth === 0 ? text.slice(0, text.lastIndexOf(")") + 1) : text;
    if (/\bRecord</.test(params)) return "Record<…>";
    if (/:\s*unknown\b|\bunknown\s*[;,)}\]>]/.test(params)) return "unknown";
    if (depth > 2) return null;
    // `typeof X` names a value, not a type to read.
    for (const name of new Set([...params.matchAll(/(?<!typeof\s+)\b([A-Z]\w+)\b/g)].map((m) => m[1]!))) {
      if (BUILTIN_TYPES.has(name) || seen.has(name)) continue;
      seen.add(name);
      const bodies = types.get(name);
      if (bodies === undefined) return `${name} (no declaration found)`;
      for (const body of bodies) {
        const inner = opaqueBody(body, seen, depth + 1);
        if (inner !== null) return `${name} > ${inner}`;
      }
    }
    return null;
  };

  const sites: Site[] = files.flatMap(({ file, text }) =>
    [...text.matchAll(/\b(useMutation|useSecretMutation)\s*(?:<[^(]*?>)?\s*\(/g)].map((match) => {
      const options = balanced(text, (match.index ?? 0) + match[0].length - 1, "()");
      const direct = [...options.matchAll(/\bapi\.(\w+)\(/g)].map((m) => m[1]!);
      const handed = [...options.matchAll(/\b(\w+)\(api[,)]/g)].map((m) => m[1]!);
      const all = [...new Set([...direct, ...handed])];
      return {
        file,
        hook: match[1] as Site["hook"],
        options,
        methods: all.filter((name) => methods.has(name)),
        handedTo: [...new Set(handed.filter((name) => !methods.has(name)))],
      };
    }),
  );

  const why = new Map<string, string>();
  const siteName = (site: Site, method: string) => `${site.file} ${method}`;
  const secret = sites.flatMap((site) => site.methods.filter((m) => m in SECRET_METHODS).map((method) => ({ site, method })));
  const called = new Set(sites.flatMap((site) => site.methods));

  return {
    sites,
    methods,
    unsafe: secret
      .filter(({ site, method }) => {
        if (site.hook === "useSecretMutation") return false;
        const reviewed = SECRET_SITES[siteName(site, method)]?.reviewed;
        return reviewed === undefined || !/\bgcTime:\s*0\b/.test(site.options);
      })
      .map(({ site, method }) => siteName(site, method)),
    secretSiteNames: [...new Set(secret.map(({ site, method }) => siteName(site, method)))].sort(),
    unclassified: [...called]
      .filter((method) => {
        const named = [...fieldsOf(methods.get(method)!)].filter((field) => SECRET_FIELD.test(field));
        if (named.length > 0) why.set(method, named.join(", "));
        return named.length > 0;
      })
      .filter((method) => !(method in SECRET_METHODS) && !(method in NOT_SECRET))
      .sort(),
    opaqueSites: [
      ...new Set(
        sites
          .filter((site) => site.methods.length === 0 || site.handedTo.length > 0)
          .map((site) => site.file)
          .filter((file) => !(file in OPAQUE_SITES)),
      ),
    ].sort(),
    unresolved: [...called]
      .filter((method) => {
        const reason = opaqueBody(methods.get(method)!);
        if (reason !== null) why.set(`${method}#body`, reason);
        return reason !== null;
      })
      .filter((method) => !(method in SECRET_METHODS) && !(method in NOT_SECRET) && !(method in OPAQUE_BODIES))
      .sort(),
    why,
  };
}

const FILES: Source[] = sources(SRC).map((path) => ({ file: relative(SRC, path), text: readFileSync(path, "utf8") }));
const API = readFileSync(join(SRC, "lib/api.ts"), "utf8");
const CONSOLE = audit(FILES, API);

describe("the secret-bearing mutations", () => {
  it("are found at all: the scan reads the console's mutations", () => {
    // Non-vacuity: a regex that matched nothing would pass every check below.
    expect(CONSOLE.sites.length).toBeGreaterThan(250);
    expect(CONSOLE.methods.size).toBeGreaterThan(400);
    expect(CONSOLE.secretSiteNames.length).toBe(Object.keys(SECRET_SITES).length);
  });

  it("are the listed sites, and no other (check 2)", () => {
    expect(CONSOLE.secretSiteNames).toEqual(Object.keys(SECRET_SITES).sort());
  });

  it("each forget their secret: useSecretMutation, or 60cf695's gcTime 0 and reset with its test (check 1)", () => {
    expect(CONSOLE.unsafe).toEqual([]);
  });

  it("name a test for every site fixed by hand, and that test reads the mutation cache", () => {
    for (const { reviewed } of Object.values(SECRET_SITES)) {
      if (!reviewed) continue;
      expect(readFileSync(join(SRC, reviewed), "utf8")).toMatch(/mutationCacheText|getMutationCache\(\)/);
    }
  });

  it("are every mutation whose body or answer has a field named like a secret, or that one is reviewed (check 3)", () => {
    expect(CONSOLE.unclassified.map((method) => `${method}: ${CONSOLE.why.get(method)}`)).toEqual([]);
  });

  it("are every mutation the scan can follow: none hands `api` to a helper it cannot see into (check 4)", () => {
    expect(CONSOLE.opaqueSites).toEqual([]);
  });

  it("are every mutation whose body the scan can read to the end, or that one is reviewed (check 5)", () => {
    expect(CONSOLE.unresolved.map((method) => `${method}: ${CONSOLE.why.get(`${method}#body`)}`)).toEqual([]);
  });

  it("keep the lists honest: every entry is still there to be found", () => {
    const called = new Set(CONSOLE.sites.flatMap((site) => site.methods));
    expect(Object.keys(SECRET_METHODS).filter((method) => !called.has(method))).toEqual([]);
    expect(Object.keys(NOT_SECRET).filter((method) => !called.has(method))).toEqual([]);
    expect(Object.keys(OPAQUE_BODIES).filter((method) => !called.has(method))).toEqual([]);
    const opaque = new Set(CONSOLE.sites.filter((site) => site.methods.length === 0 || site.handedTo.length > 0).map((s) => s.file));
    expect(Object.keys(OPAQUE_SITES).filter((file) => !opaque.has(file))).toEqual([]);
  });
});

/**
 * THE REVIEWER'S THREE PROBES, KEPT (final re-review, I2). Each is a mutation
 * the old guard let through with the default `gcTime`; each must go red here,
 * through the same `audit` the console is held to.
 */
describe("the guard against what slipped through it", () => {
  const probeApi = (method: string, body: string) => `${API}\nclass Probe {\n  ${method}(body: ${body}): Promise<void> {\n    return Promise.resolve();\n  }\n}\n`;
  const page = (options: string): Source => ({
    file: "modules/probe/ProbePage.tsx",
    text: `export function useProbe() {\n  const api = useApi();\n  return useMutation({ ${options} });\n}\n`,
  });

  it("goes red on a body with an `api_key`", () => {
    const result = audit([...FILES, page("mutationFn: (body: ProbeBody) => api.saveIntegration(body)")], probeApi("saveIntegration", "{ api_key: string }"));
    expect(result.unclassified).toContain("saveIntegration");
  });

  it("goes red on a body with an `access_token`, a `private_key` or a `passphrase`", () => {
    for (const field of ["access_token", "private_key", "passphrase", "signing_pin", "wifi_psk", "backup_codes"]) {
      const result = audit([...FILES, page("mutationFn: (body: ProbeBody) => api.connectVendor(body)")], probeApi("connectVendor", `{ name: string; ${field}: string }`));
      expect(result.unclassified, field).toContain("connectVendor");
    }
  });

  it("goes red on a listed secret method called through a local helper", () => {
    const helper: Source = {
      file: "modules/probe/save.ts",
      text: "export function save(api: ApiClient, body: EmailSettingsUpdate) {\n  return api.updateEmailSettings(body);\n}\n",
    };
    const result = audit([...FILES, helper, page("mutationFn: (body: EmailSettingsUpdate) => save(api, body)")], API);
    expect(result.opaqueSites).toContain("modules/probe/ProbePage.tsx");
    // ...and when the options call a method the scan does know, beside it.
    const beside = audit(
      [...FILES, helper, page("mutationFn: async (body: EmailSettingsUpdate) => { await api.getEmailSettings(); return save(api, body); }")],
      API,
    );
    expect(beside.opaqueSites).toContain("modules/probe/ProbePage.tsx");
  });

  it("goes red on a body it cannot read to the end", () => {
    const result = audit([...FILES, page("mutationFn: (body: Record<string, unknown>) => api.saveBlob(body)")], probeApi("saveBlob", "Record<string, unknown>"));
    expect(result.unresolved).toContain("saveBlob");
  });

  it("stays green on what is not a secret (non-vacuity for the four above)", () => {
    const result = audit([...FILES, page("mutationFn: (body: ProbeBody) => api.renameThing(body)")], probeApi("renameThing", "{ name: string; keyboard_layout: string }"));
    expect(result.unclassified).not.toContain("renameThing");
    expect(result.unresolved).not.toContain("renameThing");
    expect(result.opaqueSites).not.toContain("modules/probe/ProbePage.tsx");
  });
});
