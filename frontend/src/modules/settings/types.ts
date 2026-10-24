// The org-profile contract, and only that.
//
// Every other surface this page edits already has types written against the
// backend's own schema module (`../ai-admin/types`, `../notification/types`,
// `../sla/types`, `../health/types`, `../budget/types`); those are imported
// rather than re-declared, so a backend change breaks one file and not two.
//
// `GET/PATCH /api/v1/orgs/current` is the exception: it is being written by a
// concurrent workstream, so this file states the contract this page was built
// against. If the route is absent the page says so (see `OrganizationTab`); it
// does not invent a shape and it does not pretend a save happened.

import { getLanguage, translate, type MessageKey } from "../../i18n/core";
import { lazyFields } from "../../i18n/lazy";
import { intlLocale } from "../../lib/datetime";
import { humaniseEnum, humaniseStatus } from "../../lib/labels";

/**
 * What an org setting may hold on the wire.
 *
 * The server owns the key ALLOWLIST — this console does not carry a copy of it.
 * That is deliberate: a hardcoded key list here would drift from the server's
 * the first time somebody adds a key, and the failure mode is a field that
 * looks editable and is silently dropped on write. The form renders exactly
 * the keys `GET /orgs/current` returns, so what is on screen is what the
 * server admits exists.
 */
export type OrgSettingValue = string | number | boolean | null;

export interface CurrentOrgOut {
  id?: string;
  name: string;
  /** Key-allowlisted server-side. Absent is legal and means "no settings yet". */
  settings?: Record<string, unknown>;
}

export interface CurrentOrgUpdate {
  name?: string;
  settings?: Record<string, OrgSettingValue>;
}

/** How a single allowlisted setting is edited, derived from the value the server sent. */
export interface OrgSettingField {
  key: string;
  /**
   * The control to render.
   *
   * Derived from the STORED value's type, not guessed from the key name --
   * with one named exception, `"choice"`, for the handful of keys whose
   * vocabulary the SERVER declares as a closed set (`SettingSpec.choices` in
   * `app/api/v1/orgs.py`). See `SETTING_CHOICES` below for why that exception
   * is not the drift this file otherwise refuses.
   */
  kind: "text" | "number" | "boolean" | "choice";
  value: OrgSettingValue;
  /** For `"choice"` only: the offered values, wire token first, in server order. */
  choices?: readonly { value: string; label: string }[];
}

/**
 * THE TWO KEYS WHOSE VALUES ARE A CLOSED VOCABULARY, IN WORDS.
 *
 * `date_format` rendered as a text box containing `dmy`. Every other value on
 * the panel is something a person wrote and can read back -- Toeshee, 50,
 * en-GB, it@toeshee.example, Europe/Lisbon -- and this one was a wire token
 * with nothing on screen saying what else it would accept. An operator who
 * wants day-first dates has to already know the server spells it `dmy`.
 *
 * **WHY THIS IS NOT THE HARDCODED ALLOWLIST THIS FILE REFUSES TO CARRY.** The
 * refusal above is about WHICH KEYS EXIST: a key list here drifts from the
 * server's the first time somebody adds one, and the failure mode is a field
 * that looks editable and is silently dropped on write. Nothing here decides
 * that. The fields are still exactly the keys `GET /orgs/current` returned; a
 * key the server stops sending renders nothing, and a key it starts sending
 * renders as text without a console change. This table only changes HOW an
 * already-rendered key is edited.
 *
 * **AND A VALUE THIS TABLE DOES NOT NAME IS NEVER LOST.** `orgSettingChoices`
 * appends the stored value as its own option when it is not in the list, so a
 * server that adds a fourth date format shows it, selected, spelled as it is
 * stored -- rather than silently re-pointing the control at the first option
 * and writing a value nobody chose. That is the only drift that would matter
 * here, and it is answered rather than argued about.
 */
const SETTING_CHOICES: Record<string, readonly { value: string; label: string }[]> = {
  // `SettingSpec(kind="str", choices={"iso", "dmy", "mdy"})`. The example date
  // is the same date in all three, which is the only way "mdy" and "dmy" are
  // told apart by somebody who has not met them before.
  // The labels are accessors over the catalogue (`lazyFields`), read when a
  // choice is drawn, so a language switch shows on the next render.
  date_format: [
    lazyFields({ value: "iso" }, { label: "settings.organization.choices.iso" }),
    lazyFields({ value: "dmy" }, { label: "settings.organization.choices.dmy" }),
    lazyFields({ value: "mdy" }, { label: "settings.organization.choices.mdy" }),
  ],
  // `SettingSpec(kind="str", choices={"info", "warning", "critical"})`.
  console_banner_severity: [
    lazyFields({ value: "info" }, { label: "settings.organization.choices.info" }),
    lazyFields({ value: "warning" }, { label: "settings.organization.choices.warning" }),
    lazyFields({ value: "critical" }, { label: "settings.organization.choices.critical" }),
  ],
};

/**
 * The options for one enumerated key, with the stored value guaranteed to be
 * among them. Returns `null` for a key with no declared vocabulary.
 */
export function orgSettingChoices(
  key: string,
  stored: OrgSettingValue,
): readonly { value: string; label: string }[] | null {
  const declared = SETTING_CHOICES[key];
  if (declared === undefined) return null;
  const current = typeof stored === "string" ? stored : "";
  if (current === "" || declared.some((choice) => choice.value === current)) return declared;
  return [...declared, { value: current, label: current }];
}

/**
 * Keys this console refuses to render, whatever the server sends.
 *
 * Invariant 3 says vault-held material is never returned by a normal API
 * response and never displayed. The org settings allowlist is server-owned and
 * is not supposed to contain anything credential-shaped — the notification
 * module's `_reject_secret_keys` enforces the same rule on channel config for
 * the same reason. This is the console's half of that: a key whose NAME reads
 * like a credential is never put in an input, never pre-filled, and never
 * echoed back on save. If a real setting is ever named unluckily, it is
 * renamed server-side; the alternative is a console that will display a secret
 * the day one lands in that dictionary.
 */
const SECRET_SHAPED_KEY = /(secret|password|passwd|token|api[_-]?key|credential|private[_-]?key)/i;

export function isSecretShapedKey(key: string): boolean {
  return SECRET_SHAPED_KEY.test(key);
}

function isScalar(value: unknown): value is OrgSettingValue {
  return (
    value === null ||
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean"
  );
}

/**
 * A setting key AS A LABEL.
 *
 * The fields on this tab are derived from the server's allowlist -- that is
 * deliberate and does not change (see `OrganizationTab`'s header) -- but
 * deriving the FIELDS from the server does not mean showing an operator the
 * server's KEY NAMES. Six of seven captions read `date_format`,
 * `default_page_size`, `show_device_serials`, `support_email`, `locale`,
 * `timezone`, sitting beside the one human label on the panel, "Organization
 * name". The key is still shown, as help text under the control, because the
 * panel's whole subject is which keys this server admits exist.
 *
 * Sentence case, never title case: "Default page size", not "Default Page
 * Size". A key with no letters in it (a server that allowlists `2fa`) falls
 * back to itself rather than to an empty caption.
 */
const KNOWN_SETTING_LABELS: Readonly<Record<string, MessageKey>> = {
  timezone: "settings.organization.keys.timezone",
  locale: "settings.organization.keys.locale",
  date_format: "settings.organization.keys.dateFormat",
  support_email: "settings.organization.keys.supportEmail",
  support_phone: "settings.organization.keys.supportPhone",
  support_url: "settings.organization.keys.supportUrl",
  console_banner: "settings.organization.keys.consoleBanner",
  console_banner_severity: "settings.organization.keys.consoleBannerSeverity",
  brand_primary_color: "settings.organization.keys.brandPrimaryColor",
  default_page_size: "settings.organization.keys.defaultPageSize",
  show_device_serials: "settings.organization.keys.showDeviceSerials",
  console_email_domain: "settings.organization.keys.consoleEmailDomain",
};

export function orgSettingLabel(key: string): string {
  // A key the allowlist is known to carry reads in the active language; any
  // other key the server starts sending is its own name in words, as recorded.
  if (Object.hasOwn(KNOWN_SETTING_LABELS, key)) return translate(KNOWN_SETTING_LABELS[key] as MessageKey);
  const words = key.replace(/[_-]+/g, " ").trim();
  if (words === "") return key;
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/**
 * Org settings that are a SWITCH WITH A TAB OF ITS OWN, and so are never a
 * field on the Organization tab.
 *
 * `mcp_enabled` opens a remote path into the organization (invariant 1c), and
 * Settings -> AI connections asks before turning it on. A second, generic
 * toggle here would be the same write without that question -- one
 * server-side setting, one form. These keys are not "unrenderable" either:
 * they are rendered, elsewhere, so the Organization tab does not name them as
 * something it could not edit.
 */
export const SWITCHES_WITH_OWN_TAB: ReadonlySet<string> = new Set(["mcp_enabled"]);

/**
 * The settings that can be rendered as a control, in stable key order.
 *
 * A `null` value has no type to derive a control from, so it is edited as
 * text — the server re-validates, and a text box that round-trips the typed
 * value is better than a control chosen by guessing at the key's name.
 */
export function orgSettingFields(settings: Record<string, unknown> | undefined): OrgSettingField[] {
  if (!settings) return [];
  return Object.keys(settings)
    .sort()
    .flatMap((key) => {
      const value = settings[key];
      if (isSecretShapedKey(key)) return [];
      if (SWITCHES_WITH_OWN_TAB.has(key)) return [];
      if (!isScalar(value)) return [];
      const choices = orgSettingChoices(key, value);
      const kind: OrgSettingField["kind"] =
        typeof value === "number"
          ? "number"
          : typeof value === "boolean"
            ? "boolean"
            : choices !== null
              ? "choice"
              : "text";
      return choices !== null && kind === "choice"
        ? [{ key, kind, value, choices }]
        : [{ key, kind, value }];
    });
}

/**
 * Keys the server sent that this form will NOT edit — structured values, and
 * anything credential-shaped.
 *
 * They are NAMED on screen rather than dropped silently, because a settings
 * form that quietly discards part of what the server holds is the exact
 * failure this page was rebuilt to remove. Naming a key is not displaying its
 * value: the VALUE of a credential-shaped key never reaches the DOM.
 */
export function unrenderableOrgSettingKeys(settings: Record<string, unknown> | undefined): string[] {
  if (!settings) return [];
  return Object.keys(settings)
    .filter((key) => isSecretShapedKey(key) || !isScalar(settings[key]))
    .sort();
}

// ---------------------------------------------------------------------------
// Access control — the roles surface (`/api/v1/roles`, `/api/v1/permissions`)
// ---------------------------------------------------------------------------
//
// These mirror `backend/app/api/v1/roles.py`'s response models. They are
// declared HERE rather than in `lib/api.ts` for the reason the header of that
// file's siblings gives: the module owns its contract, and `lib/api.ts`
// imports it.

/** One cell of the matrix, as `GET /api/v1/permissions` serves it. */
export interface PermissionOut {
  code: string;
  /** The part before the first dot — the matrix's row. */
  module: string;
  /** The rest of the code — the matrix's column. */
  action: string;
  description: string;
}

export interface PermissionCatalogueOut {
  permissions: PermissionOut[];
}

export interface RoleOut {
  id: string;
  name: string;
  description: string;
  /** A platform-seeded role shared by every organization. Editing one copies it first. */
  is_global: boolean;
  /** How many users in THIS organization hold it — the "who does this affect" number. */
  assigned_users: number;
  codes: string[];
}

export interface RoleListOut {
  roles: RoleOut[];
}

export interface RolePermissionsOut {
  /**
   * NOT the requested id when a platform-wide role was copied into this
   * organization first. The console says so when it differs, because "I edited
   * technician" and "a copy of technician now exists and holds my edit" are
   * different facts and only one of them is true.
   */
  role_id: string;
  codes: string[];
  affected_users: number;
}

export interface RoleCreate {
  name: string;
  description: string;
  codes: string[];
}

export interface RoleUpdate {
  name?: string;
  description?: string;
}

/**
 * Cross-org platform administration (spec §1.3).
 *
 * The server never lists it, never grants it and never edits it. This console
 * ALSO refuses to render it: two independent refusals, because the one that
 * matters is the server's and the one an operator sees is this one. If a future
 * server regression started listing it, an "Edit permissions for super-admin"
 * button on this page would be an invitation to try.
 */
export const SUPER_ADMIN_ROLE_NAME = "super-admin";

/** The roles this console will offer, in the order it offers them. */
export function assignableRoles(roles: readonly RoleOut[] | undefined): RoleOut[] {
  return (roles ?? [])
    .filter((role) => role.name !== SUPER_ADMIN_ROLE_NAME)
    .slice()
    .sort((a, b) => a.name.localeCompare(b.name, intlLocale()));
}

/** The catalogue as the grid draws it: one group per module, actions within. */
export interface PermissionModuleGroup {
  module: string;
  permissions: PermissionOut[];
}

/**
 * Group the SERVER'S catalogue by module prefix.
 *
 * There is no module list in this file and there must never be one: 92 codes
 * over 32 modules today, and a code added to `app/core/rbac.py` has to appear
 * here with no second edit (spec §1.1). The grouping key is the `module` field
 * the server sent, not a prefix this console re-derives.
 */
export function groupPermissionsByModule(
  permissions: readonly PermissionOut[] | undefined,
): PermissionModuleGroup[] {
  const byModule = new Map<string, PermissionOut[]>();
  for (const permission of permissions ?? []) {
    const bucket = byModule.get(permission.module);
    if (bucket) bucket.push(permission);
    else byModule.set(permission.module, [permission]);
  }
  return [...byModule.entries()]
    .sort(([a], [b]) => a.localeCompare(b, intlLocale()))
    .map(([module, group]) => ({
      module,
      permissions: group.slice().sort((a, b) => a.action.localeCompare(b.action, intlLocale())),
    }));
}

// ---------------------------------------------------------------------------
// Access control — the users surface (`/api/v1/users`)
// ---------------------------------------------------------------------------

export interface RoleAssignmentOut {
  /**
   * The `user_roles` row. A revoke is addressed to THIS, never to `role_id`:
   * the same role may be held twice at different scopes, so "remove
   * technician" is ambiguous.
   */
  assignment_id: string;
  role_id: string;
  role_name: string;
  /**
   * `null` for a grant on every client, including clients created later; a
   * client id for one scoped to that client (MSP clients spec §2.1). It was a
   * device group until then; device groups no longer scope grants.
   */
  client_id: string | null;
}

/**
 * How one grant's scope reads on screen: "everywhere", or "limited to
 * <client>" (`docs/design/settings-language.md` glossary — the operator never
 * sees the words "grant", "scope" or "organization-wide").
 *
 * `null` is a real answer, not a missing one — it means the grant covers
 * every client, including clients created later — so it is spelled out rather
 * than left blank. A blank suffix beside a scoped grant's suffix reads as data
 * that failed to load.
 *
 * `clientNames`, WHEN GIVEN, NAMES THE CLIENT RATHER THAN ITS ID. Falls back
 * to the id when the map has nothing for it -- a client the caller cannot
 * list, or an archived one -- never to a blank suffix. (The scope was a
 * device group until the MSP clients model, spec §2.1.)
 */
export function describeGrantScope(
  clientId: string | null,
  clientNames?: ReadonlyMap<string, string>,
): string {
  if (clientId === null) return translate("settings.roles.scopeEverywhere");
  return translate("settings.roles.scopeLimited", { client: clientNames?.get(clientId) ?? clientId });
}

/**
 * A role name as the console shows it: "org-admin" -> "Org admin"
 * (`docs/design/settings-language.md` "Users"). The wire value is unchanged
 * everywhere else -- the server matches on `role_name`/`role.name` as sent,
 * never on this string.
 *
 * THE ONE FUNCTION THIS TRANSFORM LIVES IN -- not the one place it is
 * called, which is a claim about call sites and therefore goes stale the
 * moment a new one is added or one is missed. It used to be a private
 * `roleChipLabel` inside `UsersTab.tsx`, reaching only the directory chip,
 * while the Roles tab's own table, the assignment drawer's `<select>`, the
 * create-user checkboxes, the effective-permissions "Granted by" column and
 * the revoke-confirmation dialog each either read the raw value or grew
 * their own copy -- so the same role read as many as three different ways
 * on the Users tab alone. Exported here, beside `describeGrantScope` (the
 * same tab's other wire-to-screen transform), so every surface that names a
 * role imports this instead of writing its own version --
 * `notification/labels.ts` is the precedent: duplicate maps there were
 * deleted rather than kept in sync. THE INVARIANT THIS BUYS: any element on
 * screen that shows `role.name` or `role_name` without this wrapped around
 * it is the defect this note exists to prevent recurring -- checking that
 * stays true is a matter of grepping call sites, not trusting a list here.
 */
export function roleDisplayName(roleName: string): string {
  // The seeded roles (migrations 0003 and 0028) have names the product owns, so
  // they read in the active language: `enums.SeededRole.<name as stored>`. The
  // catalogue answers with the key itself for a name it does not hold, which is
  // how a role an operator made is told apart -- it keeps the derivation below
  // in either language, because the words are theirs. English is that same
  // derivation, so the English page did not change.
  //
  // CALLED FROM NON-REACT CODE, so this reads the language when it is called.
  // Every component that calls it also calls `useT()`, which is what makes it
  // run again on a language switch; do not memoise its result.
  const seeded = seededRoleName(roleName);
  if (seeded !== null) return seeded;
  const spaced = roleName.replace(/-/g, " ").trim();
  if (spaced === "") return roleName;
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** A seeded role's name in the active language, or null for any other role. */
export function seededRoleName(roleName: string): string | null {
  const key = `enums.SeededRole.${roleName}` as MessageKey;
  const label = translate(key);
  return label !== key ? label : null;
}

/**
 * A role name where the console has always shown it AS STORED in English (the
 * shell's account chip, the person page's sign-in roles): "org-admin". English
 * stays exactly that; in another language a seeded role reads in that language
 * and any other role keeps the name its author gave it. Like `roleDisplayName`
 * this reads the language when called, so its callers must call `useT()`.
 */
export function roleChipName(roleName: string): string {
  return getLanguage() === "en" ? roleName : (seededRoleName(roleName) ?? roleName);
}

/** One line of the directory's role summary: a role at a scope, and how many rows say so. */
export interface RoleGrantSummary {
  key: string;
  roleName: string;
  clientId: string | null;
  /**
   * How many `user_roles` rows carry this exact (role, scope) pair. One,
   * always, on a database that has run migration 0073 — see the note on
   * `summariseRoleGrants` for why the console still counts.
   */
  count: number;
}

/**
 * Collapse identical grants into one line, and say how many there were.
 *
 * WHY THE CONSOLE COUNTS AT ALL. Until migration 0073 the database accepted
 * the same org-wide grant without limit (`UNIQUE (user_id, role_id,
 * device_group_id)` over a nullable column enforces nothing when the column is
 * NULL), and the operator's deployment held `super-admin` three times. (The
 * scope column is `client_id` now, under the same two partial indexes.) The
 * directory rendered one line per row, so the symptom on screen was three
 * identical lines — which reads as a rendering bug and tells the operator
 * nothing about what is actually stored.
 *
 * The index makes new duplicates impossible, so this is belt to that braces.
 * It is kept anyway because a list that repeats itself is a defect in its own
 * right, and because collapsing SILENTLY would be the opposite mistake: a
 * console that draws one line over three rows has stopped describing the
 * database. One line, with the count, is the honest rendering of both.
 *
 * SAME ROLE, DIFFERENT SCOPE, IS NOT A DUPLICATE. `technician` on every
 * client and `technician` on one client are different answers to "what may
 * this person reach", so the scope is part of the key and they stay two lines.
 *
 * Server order is preserved (the API sorts by role name, then row id), so the
 * first grant of a pair decides where its line sits.
 */
export function summariseRoleGrants(
  assignments: readonly RoleAssignmentOut[] | undefined,
): RoleGrantSummary[] {
  const byScope = new Map<string, RoleGrantSummary>();
  for (const assignment of assignments ?? []) {
    const key = `${assignment.role_name} ${assignment.client_id ?? ""}`;
    const seen = byScope.get(key);
    if (seen) seen.count += 1;
    else
      byScope.set(key, {
        key,
        roleName: assignment.role_name,
        clientId: assignment.client_id,
        count: 1,
      });
  }
  return [...byScope.values()];
}

/** One revocable row, plus its position among grants identical to it. */
export interface RoleGrantRow extends RoleAssignmentOut {
  /** `null` unless the same role is held more than once at the same scope. */
  duplicate: { index: number; total: number } | null;
}

/**
 * The management list: EVERY grant, each still individually revocable, with
 * duplicates labelled.
 *
 * DELIBERATELY NOT COLLAPSED, unlike the directory summary. A revoke is
 * addressed to one `assignment_id`; collapsing three rows into one line would
 * leave the operator no way to clear the two extra rows from the console, and
 * the only surface that can remove them is this one. So the panel shows what
 * is stored and names the repetition — "duplicate 2 of 3" — rather than
 * rendering three lines that look identical and hoping the operator works out
 * why.
 */
export function annotateDuplicateGrants(
  assignments: readonly RoleAssignmentOut[] | undefined,
): RoleGrantRow[] {
  const totals = new Map<string, number>();
  const scopeKey = (a: RoleAssignmentOut): string =>
    `${a.role_name} ${a.client_id ?? ""}`;
  for (const assignment of assignments ?? []) {
    totals.set(scopeKey(assignment), (totals.get(scopeKey(assignment)) ?? 0) + 1);
  }
  const seen = new Map<string, number>();
  return (assignments ?? []).map((assignment) => {
    const key = scopeKey(assignment);
    const total = totals.get(key) ?? 1;
    const index = (seen.get(key) ?? 0) + 1;
    seen.set(key, index);
    return { ...assignment, duplicate: total > 1 ? { index, total } : null };
  });
}

export type UserStatus = "active" | "disabled" | "deleted";

/** The statuses the directory lists (the same three words as `UserStatus`, labelled lower case). */
export type DirectoryUserStatus = "active" | "disabled" | "deleted";

const USER_STATUSES: ReadonlySet<string> = new Set<DirectoryUserStatus>(["active", "disabled", "deleted"]);

/**
 * A user's status as the directory shows it. A status this console has not met
 * reads through the shared status vocabulary, never as the wire word.
 */
export function userStatusLabel(status: string): string {
  return USER_STATUSES.has(status) ? humaniseEnum(status, "DirectoryUserStatus") : humaniseStatus(status);
}

export interface UserOut {
  id: string;
  email: string;
  display_name: string;
  /** `active`, `disabled` or `deleted`. A deleted row is RETAINED, so it is listed. */
  status: string;
  roles: RoleAssignmentOut[];
}

export interface UserListOut {
  users: UserOut[];
}

export interface CreatedUserOut extends UserOut {
  /**
   * The shipped realm has no `smtpServer` (spec §2.3), so this is regularly
   * false on a real deployment and the account is unusable until it is fixed.
   * Rendering it as an alert is the whole reason the field exists.
   */
  enrolment_email_sent: boolean;
  enrolment_email_error: string | null;
}

export interface CodeGrantOut {
  code: string;
  via_role: string;
  /** `null` when the grant covers every client, else the client it is scoped to. */
  client_id: string | null;
}

export interface EffectivePermissionsOut {
  user_id: string;
  codes: CodeGrantOut[];
}

export interface UserCreate {
  email: string;
  display_name: string;
  role_ids: string[];
}

/**
 * `POST /users/{id}/roles`. `client_id` scopes the grant to one client, `null`
 * grants it on every client (MSP clients spec §2.1). The server's model is
 * closed (`extra="forbid"`): the old `device_group_id` key is a 422.
 */
export interface RoleAssign {
  role_id: string;
  client_id: string | null;
}

/** A mail this platform asked Keycloak to send. Carries no credential. */
export interface MailResultOut {
  sent: boolean;
}

// ---------------------------------------------------------------------------
// The account profile (`PATCH /api/v1/me`) — spec §7.3
// ---------------------------------------------------------------------------

/**
 * What the profile form writes, and the complete list of it.
 *
 * NOT `email` (Keycloak owns it, and changing it there is a re-authenticated,
 * verified flow this console must not reimplement), NOT `status`, and nothing
 * role-shaped — spec falsifier 12 calls a self-service form that writes those
 * a privilege-escalation path. The route takes no `user_id` either: the target
 * is the calling token's own user.
 */
export interface MeProfileUpdate {
  display_name: string;
  phone: string | null;
  timezone: string | null;
  locale: string | null;
}

// ---------------------------------------------------------------------------
// Email — `GET/PUT /api/v1/settings/email`, `POST /api/v1/settings/email/test`
// ---------------------------------------------------------------------------
//
// These mirror `backend/app/api/v1/realm_settings.py`'s models. The shape is
// the interesting part: there is NO password field on the way out, on purpose
// and structurally. The server has no key that can carry one, so this console
// has none to forget to hide.

export interface EmailSettingsOut {
  /** False until a relay is set — the state this platform ships in. */
  configured: boolean;
  host: string;
  /** As the identity provider stores it: a string, empty when unset. */
  port: string;
  from_address: string;
  from_display_name: string;
  reply_to: string;
  /** `none`, `starttls` or `ssl`. */
  encryption: string;
  username: string;
  /**
   * A sign-in is configured for the relay. This is the ONLY thing the console
   * can say about the stored password, and it is why the form shows
   * "Password: set" rather than a masked value: a masked value implies the
   * console received something, and it did not.
   */
  authenticated: boolean;
  /**
   * `password` or `token`. `token` is a Microsoft 365 sign-in: the realm's
   * sign-in service fetches the token itself, so the console never sees one.
   * Absent from a server older than this field, which means `password`.
   */
  auth_type?: string;
  /** The Microsoft application (client) ID. Its secret has no field here. */
  microsoft_client_id?: string;
  /** Where the Microsoft sign-in fetches its access. Not a secret. */
  microsoft_sign_in_url?: string;
  /** What the Microsoft sign-in asks for. Not a secret. */
  microsoft_scope?: string;
  /**
   * The caller's own address with `+itms-test` before the @ — where the
   * console proposes sending a test. The identity provider will not send a
   * test to a sign-in account's own address; a tagged one reaches the same
   * inbox. Empty or absent when there is nothing to suggest.
   */
  suggested_test_recipient?: string;
}

/** `none`, `starttls` or `ssl` — one of three, never two flags at once. */
export type EmailEncryption = "none" | "starttls" | "ssl";

export interface EmailSettingsUpdate {
  host: string;
  port: number;
  from_address: string;
  from_display_name: string;
  reply_to: string;
  encryption: EmailEncryption;
  username: string;
  /**
   * OMITTED unless the operator typed one, and the omission is load-bearing:
   * the server reads an absent field as "keep what is stored". Sending `""`
   * for an untouched box would clear a working credential the first time
   * somebody corrected the port, and the only symptom would be invitations
   * that quietly stop arriving.
   */
  password?: string;
  /** Defaults to `password` on the server. */
  auth_type?: "password" | "token";
  token_url?: string;
  token_scope?: string;
  token_client_id?: string;
  /** Only when the operator typed one; absent means "keep the stored one". */
  token_client_secret?: string;
}

/** What came of asking the identity provider to send a test message. */
export interface TestSendOut {
  /**
   * The identity provider ACCEPTED the message. Acceptance is not delivery —
   * a relay can take a message from an unknown sender and drop it silently —
   * so nothing in this console renders this as a green tick.
   */
  sent: boolean;
  /**
   * Where the test went: the address asked for, or the saved sending address
   * when none was. Absent from a server older than this field.
   */
  recipient?: string | null;
}

/**
 * Where to send a test. The server accepts only an address at the sending
 * address's domain or the caller's own sign-in domain; an empty body sends to
 * the sending address.
 */
export interface TestSendIn {
  recipient?: string;
}
