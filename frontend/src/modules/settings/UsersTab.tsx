// Users — the identity directory and its full lifecycle.
//
//   read   `GET /api/v1/users`, `GET /api/v1/users/{id}/effective-permissions`   `users.read`
//   write  `POST /api/v1/users/invite`, `POST|DELETE /users/{id}/roles[...]`,
//          `POST /users/{id}/disable|enable`, `POST /users/{id}/invite/resend`,
//          `DELETE /users/{id}/invite`, `POST /users/{id}/factors/reset`,
//          `DELETE /users/{id}/sessions`, `DELETE /users/{id}`   `users.manage` on every client
//
// THE SECOND AXIS OF "WHAT LEVEL" IS THE SCOPE, and the assign control takes
// it. It was a device group (`user_roles.device_group_id`, NULL org-wide) until
// the MSP clients model; it is a CLIENT now (`user_roles.client_id`, NULL for
// every client including clients created later -- MSP clients spec §2.1), and
// device groups no longer scope grants. "Technician everywhere" and
// "technician on Warehouse only" are different answers to the question this
// tab exists to answer.
//
// The wire shape this editor sends is final (MSP clients design §3.4); phase 3
// adds the per-client view of effective permissions (design §4.2).
//
// THE DIRECTORY COLLAPSES IDENTICAL GRANTS; THE ROLES PANEL DOES NOT.
// `user_roles` accepted the same org-wide grant without limit until migration
// 0073 — a unique constraint over a nullable scope column enforces
// nothing when the column is NULL — and one deployment held `super-admin`
// three times. The directory drew three identical lines, which reads as a
// rendering fault and says nothing about what is stored, so it now draws one
// line per (role, scope) and states the count. The Roles panel keeps one row
// per grant, because a revoke is addressed to an `assignment_id` and this is
// the only surface that can clear the extra rows; it labels them "duplicate 2
// of 3" instead. Same role at a DIFFERENT scope is never collapsed — it is a
// different grant, and the scope suffix is what says so.
//
// EVERY DESTRUCTIVE ACTION CONFIRMS, AND THE CONFIRMATION NAMES THE PERSON.
// "Are you sure?" over a table of nine people is not a confirmation — it is a
// dialog that gets clicked through. Disable, delete, resetting the second
// factors, signing someone out everywhere, cancelling an invitation and role
// removal each name the email address in the prompt and in the confirm button.
// Enable and resending an invitation do not confirm: neither takes anything
// away.
//
// NO CREDENTIAL IS RENDERED, EVER (invariant 3). `UserOut` carries none, and
// this file renders named fields only — never the response object — so a
// server that starts returning something credential-shaped does not put it on
// a screen. `users.test.tsx` plants a secret in the directory fixture and
// asserts the DOM never contains it. The one value this tab shows that opens
// an account -- an invitation link, when no mail is configured -- is held in
// this component's state only: the mutation that brought it is reset at once
// and, with `gcTime: 0` (`./api.ts`), dropped from the mutation cache with it,
// and the link is gone when the operator says it was passed on.
//
// AN ACCOUNT STARTS AS AN INVITATION (own authentication design §7). The form
// names the person and what they may reach on which client -- the same
// (role, client) pairs the roles panel grants -- and the server creates the
// account with no password and a single-use link that expires after 72
// hours, from which the person chooses a password and registers a second
// factor. No password is ever typed here. Without mail, the server answers
// with the link instead, ONCE, and this tab shows it for the operator to pass
// on another way; resending shows a fresh one the same way.
//
// RESETTING SOMEONE'S SECOND FACTORS NEEDS YOUR OWN (§2.5, §7), and so, since
// the operator widened §2.5 on 2026-11-08 (S1), do inviting, sending an
// invitation again, granting and removing a role -- and, since 2026-11-09
// (S5), enabling a disabled person again. The server answers 403
// `step_up_required` unless the operator confirmed their own second factor in
// the last 10 minutes; `useStepUp` asks for it -- over the confirmation, where
// there is one -- and sends the act once more only after the server accepted
// it. Cancelling an invitation, disabling, signing out and deleting take
// access away, and are never asked (S4).
// There is no password reset here: a forgotten password is reset by the
// person, by email (§2.8), and a second factor never is.
//
// THE MAIL BANNER IS HERE BECAUSE THIS IS WHERE IT BITES. Without mail an
// invitation still works -- its link is shown once -- but it reaches nobody
// on its own, and nobody can reset a forgotten password by email. So an
// unconfigured relay is announced HERE, before the button is pressed rather
// than after. It is the same rule the unconfigured states elsewhere in this
// platform follow: visible where it bites, not discovered from a failure.
//
// The banner needs `orgs.manage` to know anything, because that is what reads
// the email settings — so `canReadEmail` gates the query. An operator who
// cannot open Settings → Email is not shown a banner pointing at it; that is a
// dead end, not a warning, and the honest fix for them is that somebody with
// `orgs.manage` sees it.
//
// A ROW THAT NAMES A PERSON LEADS WITH `Person` (v3's `person(ini, i, name)`):
// the avatar tone is chosen from the user's id so the same person is the same
// colour on every page that draws them.

import { useMemo, useState, type CSSProperties } from "react";
import { Link } from "react-router-dom";

import { describeSubmitError } from "../../components/form";
import { useStepUp } from "../../components/stepUp";
import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import { Trans } from "../../i18n/Trans";
import { EntityPicker } from "../../components/picker";
import { ShownOnceLink, type ShownOnceLinkText } from "../../components/shownOnce";
import { Badge, Button, Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { Btn, FieldGroup, KIND_ICON, Panel, QueryState } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { Person } from "../../shell/viz";
import { initialsOf, personIndex } from "../../lib/people";
import {
  useAssignRoleMutation,
  useCancelInviteMutation,
  useClients,
  useEmailSettings,
  useDeleteUserMutation,
  useEffectivePermissions,
  useEndUserSessionsMutation,
  useInviteUserMutation,
  useRemoveRoleMutation,
  useResendInviteMutation,
  useResetUserFactorsMutation,
  useSetUserEnabledMutation,
  useRoles,
  useUsers,
  useGlobalWriteReason,
  useMe,
} from "./api";
import {
  EVERY_CLIENT_CHOICE,
  clientPickerEmptyHint,
  grantClientIdFor,
  grantScopeOptions,
} from "./pickerOptions";
import { isForbidden } from "./settingsState";
import {
  annotateDuplicateGrants,
  assignableRoles,
  describeGrantScope,
  roleDisplayName,
  summariseRoleGrants,
  userStatusLabel,
} from "./types";
import type { InviteGrant, RoleAssignmentOut, UserOut } from "./types";
import { FOLDING_FORM_GRID } from "./formGrid";
import { settingsHref } from "./tabs";
import { groupGrantsByClient } from "./grantGroups";

export const USERS_READ = "users.read";
export const USERS_MANAGE = "users.manage";

type Confirmable =
  | { kind: "disable"; user: UserOut }
  | { kind: "delete"; user: UserOut }
  | { kind: "resetFactors"; user: UserOut }
  | { kind: "endSessions"; user: UserOut }
  | { kind: "cancelInvite"; user: UserOut }
  | { kind: "revoke"; user: UserOut; assignment: RoleAssignmentOut };

/** An invitation link the server handed back because no mail is configured: shown once, here. */
interface ShownLink {
  email: string;
  link: string;
}

/** What an action did, held as data so a change of language re-words it. */
interface Done {
  key: "inviteEmailed" | "inviteResent" | "factorsReset" | "sessionsEnded";
  email: string;
}

const DONE: Record<Done["key"], MessageKey> = {
  inviteEmailed: "settings.users.done.inviteEmailed",
  inviteResent: "settings.users.done.inviteResent",
  factorsReset: "settings.users.done.factorsReset",
  sessionsEnded: "settings.users.done.sessionsEnded",
};

/**
 * The words of the link shown once (`components/shownOnce.tsx`, which also
 * says when a copy failed: a copy that fails silently would let an operator
 * dismiss the only copy of an invitation link believing it was on the
 * clipboard).
 */
const LINK_TEXT: ShownOnceLinkText = {
  title: "settings.users.link.title",
  once: "settings.users.link.once",
  label: "settings.users.link.label",
  copy: "settings.users.link.copy",
  copied: "settings.users.link.copied",
  copyFailed: "settings.users.link.copyFailed",
  done: "settings.users.link.done",
};

function statusTone(status: string): "success" | "warning" | "muted" {
  if (status === "active") return "success";
  if (status === "disabled") return "warning";
  return "muted";
}

/** Two letters for the avatar: the first of the first two words, or of the address. */
/**
 * A person's initials, from the display name where there is one.
 *
 * The name-to-initials rule and the id-to-tone hash are `lib/people`'s: this
 * file had its own, which took the first TWO words rather than the first and
 * the last, so "Ada King Lovelace" was "AK" here and "AL" on every other page
 * showing the same person. What stays local is only which FIELD to read.
 */
function userInitials(user: UserOut): string {
  return initialsOf(user.display_name.trim() || user.email);
}

/** The "Give them access to" set: a fieldset on the control edge, the legend as its caption. */
const GROUP: CSSProperties = { border: "1px solid var(--line)", borderRadius: "var(--r-ctl)", padding: "8px 12px 10px", margin: "14px 0 0" };
const GROUP_LEGEND: CSSProperties = { fontWeight: 600, padding: "0 4px" };
/** Lines under a bare panel's table sit on the panel's own padding. */
const UNDER_TABLE: CSSProperties = { padding: "12px 18px" };
/**
 * Sets Delete apart from the row's other actions (spec: "a danger-tinted text
 * action, set apart from the rest rather than a bordered button shouting
 * alongside them") -- a hairline first, so the eye reads it as its own group
 * rather than a fourth button in the run of five.
 */
const DANGER_SEPARATOR: CSSProperties = { borderLeft: "1px solid var(--line)", marginLeft: 2, paddingLeft: 10 };

export function UsersTab({
  canRead,
  canManage,
  canReadEmail,
}: {
  canRead: boolean;
  canManage: boolean;
  /** Whether this operator may read the email settings the banner reports on. */
  canReadEmail: boolean;
}) {
  const t = useT();
  const users = useUsers(canRead);
  // The caller, whose own row offers no second-factor reset.
  const selfId = useMe().data?.user_id;
  // Shares the Email tab's cache key, so saving a relay clears this banner
  // without a reload.
  const emailSettings = useEmailSettings(canReadEmail);
  // The role catalogue is read with `roles.read`, which a `users.manage`
  // holder is not guaranteed to hold. A failure here must not block the rest
  // of the tab, so the role controls degrade rather than the page failing.
  const roles = useRoles(canRead);
  // Clients are read with `clients.read`, another permission a
  // `users.manage` holder is not guaranteed to hold -- the same degrade: the
  // scope picker falls back to its `emptyHint` rather than blocking the tab.
  // ARCHIVED ONES TOO (final review): a grant on a client archived since
  // reads that client's name, not its id. The CHOICES leave them out
  // (`clientPickerOptions`): an archived client takes no new grant.
  const clients = useClients(canRead, { include_archived: true });
  // The options carry a worded sub-label ("3 devices"), so they are built at
  // render, never remembered: a memo would keep the language they were built in.
  // The names alone are recorded data, and those can be remembered. EVERY
  // client the list served is named, the unassigned one included: a grant
  // that names it cannot be made, but a name is still better than an id.
  // "Every client" is the first option (T11R-I1): a choice, never an empty box.
  const clientOptions = grantScopeOptions(clients.data ?? []);
  const clientNames = useMemo(
    () => new Map((clients.data ?? []).map((client) => [client.id, client.name])),
    [clients.data],
  );

  const stepUp = useStepUp();
  const invite = useInviteUserMutation(stepUp.run);
  const assignRole = useAssignRoleMutation(stepUp.run);
  const removeRole = useRemoveRoleMutation(stepUp.run);
  const setEnabled = useSetUserEnabledMutation(stepUp.run);
  const resendInvite = useResendInviteMutation(stepUp.run);
  const cancelInvite = useCancelInviteMutation();
  const resetFactors = useResetUserFactorsMutation(stepUp.run);
  const endSessions = useEndUserSessionsMutation();
  const deleteUser = useDeleteUserMutation();

  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  // What the invitation grants: (role, client) pairs, the roles panel's shape.
  const [inviteGrants, setInviteGrants] = useState<readonly InviteGrant[]>([]);
  // The pair being chosen, before "Add this access" puts it in the list.
  const [draftRoleId, setDraftRoleId] = useState("");
  const [draftClientId, setDraftClientId] = useState("");
  const [draftTyping, setDraftTyping] = useState(false);
  const [shownLink, setShownLink] = useState<ShownLink | null>(null);
  const [done, setDone] = useState<Done | null>(null);

  const [rolesForId, setRolesForId] = useState<string | null>(null);
  const [permissionsForId, setPermissionsForId] = useState<string | null>(null);
  const [grantRoleId, setGrantRoleId] = useState("");
  // The committed scope choice: "" (none yet), `EVERY_CLIENT_CHOICE`, or a
  // client id. `grantScopeTyping`: the picker holds text that is not a choice.
  const [grantClientId, setGrantClientId] = useState("");
  const [grantScopeTyping, setGrantScopeTyping] = useState(false);
  const [confirming, setConfirming] = useState<Confirmable | null>(null);

  const effective = useEffectivePermissions(permissionsForId);

  const grantable = useMemo(() => assignableRoles(roles.data?.roles), [roles.data]);
  const listed = users.data?.users ?? [];
  const rolesTarget = listed.find((user) => user.id === rolesForId) ?? null;
  const permissionsTarget = listed.find((user) => user.id === permissionsForId) ?? null;

  const forbidden = isForbidden(users.error);
  const reason = useGlobalWriteReason(USERS_MANAGE);
  // A pair chosen and not added would not be sent; the send waits for it.
  const draftPending = draftRoleId !== "" || draftClientId !== "" || draftTyping;

  if (forbidden) {
    return (
      <Panel title={t("settings.users.title")}>
        <p className="note">
          {t("settings.users.forbidden", { code: USERS_READ })}
        </p>
      </Panel>
    );
  }

  const roleNameOf = (roleId: string): string => grantable.find((role) => role.id === roleId)?.name ?? roleId;

  /** The chosen pair joins the invitation; the same pair twice is one grant. */
  function addGrant() {
    if (draftRoleId === "" || draftClientId === "" || draftTyping) return;
    const grant: InviteGrant = { role_id: draftRoleId, client_id: grantClientIdFor(draftClientId) };
    setInviteGrants((current) =>
      current.some((held) => held.role_id === grant.role_id && held.client_id === grant.client_id)
        ? current
        : [...current, grant],
    );
    setDraftRoleId("");
    setDraftClientId("");
  }

  function removeGrant(index: number) {
    setInviteGrants((current) => current.filter((_, at) => at !== index));
  }

  /**
   * THE LINK IS MOVED INTO THIS COMPONENT'S STATE AND THE MUTATION EMPTIED,
   * so the one value that opens an account is held in exactly one place, and
   * "Done" is what removes it.
   */
  function sendInvite() {
    const address = email.trim();
    invite.mutate(
      { email: address, display_name: displayName.trim(), grants: [...inviteGrants] },
      {
        onSuccess: (out) => {
          if (out.invite_link) {
            setShownLink({ email: address, link: out.invite_link });
            setDone(null);
          } else {
            setDone({ key: "inviteEmailed", email: address });
          }
          setEmail("");
          setDisplayName("");
          setInviteGrants([]);
          setDraftRoleId("");
          setDraftClientId("");
          invite.reset();
        },
      },
    );
  }

  function resend(target: UserOut) {
    resendInvite.mutate(target.id, {
      onSuccess: (out) => {
        if (out.invite_link) {
          setShownLink({ email: target.email, link: out.invite_link });
          setDone(null);
        } else {
          setDone({ key: "inviteResent", email: target.email });
        }
        resendInvite.reset();
      },
    });
  }

  /**
   * The four mutations a confirmation dialog can fire, by the kind that fires
   * them. Used to show the RIGHT failure in the dialog, and to clear a stale
   * one when the dialog reopens.
   */
  const confirmMutation = {
    disable: setEnabled,
    delete: deleteUser,
    resetFactors,
    endSessions,
    cancelInvite,
    revoke: removeRole,
  } as const;

  /**
   * OPENING A CONFIRMATION RESETS THE MUTATION BEHIND IT. Without this, a
   * delete that failed leaves `deleteUser.error` set for the life of the page:
   * the next delete dialog opens with a red sentence about a user the operator
   * is no longer looking at, and — worse — a SUCCESSFUL retry leaves it there,
   * because nothing clears an error but another attempt.
   */
  function openConfirm(target: Confirmable) {
    confirmMutation[target.kind].reset();
    setConfirming(target);
  }

  function runConfirmed() {
    const target = confirming;
    if (!target) return;
    const close = { onSuccess: () => setConfirming(null) };
    if (target.kind === "disable") {
      setEnabled.mutate({ userId: target.user.id, enabled: false }, close);
    } else if (target.kind === "delete") {
      deleteUser.mutate(target.user.id, close);
    } else if (target.kind === "resetFactors") {
      resetFactors.mutate(target.user.id, {
        onSuccess: () => {
          setConfirming(null);
          setDone({ key: "factorsReset", email: target.user.email });
        },
      });
    } else if (target.kind === "endSessions") {
      endSessions.mutate(target.user.id, {
        onSuccess: () => {
          setConfirming(null);
          setDone({ key: "sessionsEnded", email: target.user.email });
        },
      });
    } else if (target.kind === "cancelInvite") {
      cancelInvite.mutate(target.user.id, close);
    } else {
      removeRole.mutate(
        { userId: target.user.id, assignmentId: target.assignment.assignment_id },
        close,
      );
    }
  }

  // Worded at render. `question` and `action` take the whole sentence's
  // variables (not a verb to glue an address onto) so Spanish can put its own
  // opening mark and preposition around them.
  type ConfirmCopy = {
    title: string;
    question: (email: string, role: string) => string;
    action: (email: string) => string;
    body: string;
    destructive: boolean;
  };
  const confirmCopy: Record<Confirmable["kind"], ConfirmCopy> = {
    disable: {
      title: t("settings.users.confirm.disable.title"),
      question: (email) => t("settings.users.confirm.disable.question", { email }),
      action: (email) => t("settings.users.confirm.disable.action", { email }),
      body: t("settings.users.confirm.disable.body"),
      destructive: true,
    },
    delete: {
      title: t("settings.users.confirm.delete.title"),
      question: (email) => t("settings.users.confirm.delete.question", { email }),
      action: (email) => t("settings.users.confirm.delete.action", { email }),
      body: t("settings.users.confirm.delete.body"),
      destructive: true,
    },
    resetFactors: {
      title: t("settings.users.confirm.resetFactors.title"),
      question: (email) => t("settings.users.confirm.resetFactors.question", { email }),
      action: (email) => t("settings.users.confirm.resetFactors.action", { email }),
      body: t("settings.users.confirm.resetFactors.body"),
      destructive: true,
    },
    endSessions: {
      title: t("settings.users.confirm.endSessions.title"),
      question: (email) => t("settings.users.confirm.endSessions.question", { email }),
      action: (email) => t("settings.users.confirm.endSessions.action", { email }),
      body: t("settings.users.confirm.endSessions.body"),
      destructive: true,
    },
    cancelInvite: {
      title: t("settings.users.confirm.cancelInvite.title"),
      question: (email) => t("settings.users.confirm.cancelInvite.question", { email }),
      action: (email) => t("settings.users.confirm.cancelInvite.action", { email }),
      body: t("settings.users.confirm.cancelInvite.body"),
      destructive: true,
    },
    revoke: {
      title: t("settings.users.confirm.revoke.title"),
      question: (email, role) => t("settings.users.confirm.revoke.question", { email, role }),
      action: (email) => t("settings.users.confirm.revoke.action", { email }),
      // Meaning unchanged (docs/design/settings-language.md glossary applied
      // to the words only): a role held twice, once for a different client
      // and once everywhere, is two separate rows, and removing this one
      // leaves the other in place.
      body: t("settings.users.confirm.revoke.body"),
      destructive: true,
    },
  };

  const emailMissing = emailSettings.data != null && !emailSettings.data.configured;

  return (
    <>
      {emailMissing ? (
        // `role="alert"` and named, so it is announced rather than merely
        // coloured — and so a test can find it without matching on styling.
        <div
          role="alert"
          aria-label={t("settings.users.emailMissingAria")}
          className="notice gold"
          style={{ marginBottom: 16 }}
        >
          <Icon name={KIND_ICON.warn} size={16} />
          {/* The way to fix it is a link, not a direction to go and find it
              (docs/design/cross-module-guidance.md §2, Settings). */}
          <span>
            {t("settings.users.emailMissing")}{" "}
            <Link to={settingsHref("email")}>{t("settings.users.emailSetUp")}</Link>
          </span>
        </div>
      ) : null}
      {shownLink ? (
        // SHOWN ONCE: the server keeps only the link's hash, so this is the
        // one copy there is. It stays until the operator says it was passed on.
        <ShownOnceLink
          email={shownLink.email}
          link={shownLink.link}
          text={LINK_TEXT}
          onDone={() => setShownLink(null)}
        />
      ) : null}
      <Panel
        title={t("settings.users.title")}
        subtitle={t("settings.users.subtitle")}
        bare
      >
        <QueryState
          loading={users.isLoading}
          error={users.error}
          empty={listed.length === 0}
          emptyLabel={t("settings.users.empty")}
          emptyIcon="portal"
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("settings.users.columns.person")}</Th>
                <Th>{t("settings.users.columns.status")}</Th>
                <Th>{t("settings.users.columns.roles")}</Th>
                <Th>{t("settings.users.columns.actions")}</Th>
              </tr>
            </thead>
            <tbody>
              {listed.map((user) => (
                <tr key={user.id}>
                  <Td>
                    <div className="cell-title">
                      <Person initials={userInitials(user)} index={personIndex(user.id)} name={user.display_name || user.email} />
                    </div>
                    <div className="cell-sub">{user.email}</div>
                  </Td>
                  <Td>
                    <Badge variant={statusTone(user.status)}>{userStatusLabel(user.status)}</Badge>
                    {user.invite_pending === true ? (
                      <>
                        {" "}
                        <Badge variant="info">{t("settings.users.invitePending")}</Badge>
                      </>
                    ) : null}
                  </Td>
                  <Td>
                    {user.roles.length === 0 ? (
                      <span className="note">{t("settings.users.noRoles")}</span>
                    ) : (
                      // A CHIP PER (ROLE, SCOPE), NOT A LIST OF DATABASE
                      // FIELDS JOINED BY AN EM DASH. `.chip`/`.chips` is v3's
                      // STATIC treatment here -- not the `Chips` component
                      // from `shell/viz.tsx`, which renders each option as a
                      // real, `aria-pressed` button for a single-select
                      // filter. This is a read-only list of what someone
                      // holds, not a control to press, so it stays a plain,
                      // non-interactive list item.
                      // The scope is the quiet suffix (`.note`, same wash the
                      // rest of the console uses for secondary text) -- and it
                      // is what keeps a role held twice at TWO DIFFERENT
                      // scopes on two visibly different chips, never folded
                      // together. See the file-top comment before touching
                      // this: `summariseRoleGrants` already keyed on (role,
                      // scope), only the rendering changes here.
                      <ul className="chips" style={{ margin: 0, padding: 0, listStyle: "none" }}>
                        {summariseRoleGrants(user.roles).map((grant) => (
                          <li key={grant.key} className="chip">
                            {roleDisplayName(grant.roleName)}
                            <span className="note">{` · ${describeGrantScope(grant.clientId, clientNames)}`}</span>
                            {grant.count > 1 ? (
                              <span className="note">{` ×${grant.count}`}</span>
                            ) : null}
                          </li>
                        ))}
                      </ul>
                    )}
                  </Td>
                  <Td>
                    {/* FOUR OF SIX ACTIONS SIT BEHIND A DISCLOSURE -- NOT SIX
                        BUTTONS IN A ROW (design review at 390x844: row height
                        262px, of which ~200px was blank, because ACTIONS was
                        squeezed to 169px and six buttons stacked about five
                        deep while the column itself sat off the edge of a
                        360px scroller). Roles and Permissions stay inline,
                        ordinary bordered buttons -- what most rows are worked
                        on for, and narrow enough at two buttons to fit
                        without wrapping the row tall.

                        The lifecycle actions -- Disable, Reset second
                        factors and Sign out everywhere for an account in use;
                        Enable for a disabled one; Resend and Cancel for an
                        invitation -- and Delete move inside a native `<details>`, closed by
                        default. Not an invented overflow menu -- there is no
                        menu or popover component in this kit -- a `<details>`
                        needs no floating panel, no positioning and no focus
                        trap: it is keyboard-operable and exposes its own
                        open state for free, the same idiom this console
                        already uses for a closed-by-default account
                        (`AssistantTab.tsx`'s egress disclosure).

                        QUIETER MUST NOT MEAN EASIER TO HIT: closed is not
                        hidden-forever, and nothing about what any of the four
                        DO changed -- every one that takes something away
                        opens the same named, confirming dialog; Enable and
                        Resend fire without one. `DANGER_SEPARATOR`
                        still sets Delete apart with a hairline inside the
                        disclosure, so it is never mistaken for the ghost
                        button beside it. */}
                    <div className="flex flex-wrap items-center gap-2">
                      <Button
                        type="button"
                        variant="outline"
                        aria-label={t("settings.users.manageRolesAria", { email: user.email })}
                        onClick={() =>
                          setRolesForId((current) => (current === user.id ? null : user.id))
                        }
                      >
                        {t("settings.users.manageRoles")}
                      </Button>
                      <Button
                        type="button"
                        variant="outline"
                        aria-label={t("settings.users.permissionsAria", { email: user.email })}
                        onClick={() =>
                          setPermissionsForId((current) =>
                            current === user.id ? null : user.id,
                          )
                        }
                      >
                        {t("settings.users.permissions")}
                      </Button>
                    </div>
                    <details style={{ marginTop: 6 }}>
                      <summary
                        className="note cursor-pointer"
                        aria-label={t("settings.users.moreActionsAria", { email: user.email })}
                      >
                        {t("settings.users.moreActions")}
                      </summary>
                      <div className="flex flex-wrap items-center gap-2" style={{ marginTop: 6 }}>
                        {user.invite_pending === true ? (
                          <>
                            <Button
                              type="button"
                              variant="ghost"
                              aria-label={t("settings.users.resendAria", { email: user.email })}
                              disabled={!canManage || resendInvite.isPending}
                              title={canManage ? undefined : reason}
                              onClick={() => resend(user)}
                            >
                              {t("settings.users.resend")}
                            </Button>
                            <Button
                              type="button"
                              variant="ghost"
                              aria-label={t("settings.users.cancelInviteAria", { email: user.email })}
                              disabled={!canManage}
                              title={canManage ? undefined : reason}
                              onClick={() => openConfirm({ kind: "cancelInvite", user })}
                            >
                              {t("settings.users.cancelInvite")}
                            </Button>
                          </>
                        ) : user.status === "active" ? (
                          <>
                            <Button
                              type="button"
                              variant="ghost"
                              aria-label={t("settings.users.disableAria", { email: user.email })}
                              disabled={!canManage}
                              title={canManage ? undefined : reason}
                              onClick={() => openConfirm({ kind: "disable", user })}
                            >
                              {t("settings.users.disable")}
                            </Button>
                            {/* NOT ON YOUR OWN ROW (final review D2, Minor 2).
                                The server refuses it (`_refuse_self`) -- but
                                only after the step-up, which may spend a
                                single-use recovery code first. One's own
                                factors are changed in Your security. */}
                            {user.id === selfId ? (
                              <>
                                <Button
                                  type="button"
                                  variant="ghost"
                                  aria-label={t("settings.users.resetFactorsAria", { email: user.email })}
                                  aria-describedby={`own-factors-${user.id}`}
                                  disabled
                                >
                                  {t("settings.users.resetFactors")}
                                </Button>
                                <span id={`own-factors-${user.id}`} className="note">
                                  <Trans
                                    k="settings.users.resetOwnFactors"
                                    // Underlined: a link inside a sentence is told apart by more than its colour (SC 1.4.1).
                                    components={{ link: <Link className="underline" to="/profile/security" /> }}
                                  />
                                </span>
                              </>
                            ) : (
                              <Button
                                type="button"
                                variant="ghost"
                                aria-label={t("settings.users.resetFactorsAria", { email: user.email })}
                                disabled={!canManage}
                                title={canManage ? undefined : reason}
                                onClick={() => openConfirm({ kind: "resetFactors", user })}
                              >
                                {t("settings.users.resetFactors")}
                              </Button>
                            )}
                            <Button
                              type="button"
                              variant="ghost"
                              aria-label={t("settings.users.endSessionsAria", { email: user.email })}
                              disabled={!canManage}
                              title={canManage ? undefined : reason}
                              onClick={() => openConfirm({ kind: "endSessions", user })}
                            >
                              {t("settings.users.endSessions")}
                            </Button>
                          </>
                        ) : (
                          <Button
                            type="button"
                            variant="ghost"
                            aria-label={t("settings.users.enableAria", { email: user.email })}
                            disabled={!canManage || user.status === "deleted"}
                            title={canManage ? undefined : reason}
                            onClick={() =>
                              setEnabled.mutate({ userId: user.id, enabled: true })
                            }
                          >
                            {t("settings.users.enable")}
                          </Button>
                        )}
                        <span style={DANGER_SEPARATOR}>
                          <Button
                            type="button"
                            variant="ghost"
                            className="danger"
                            aria-label={t("settings.users.deleteAria", { email: user.email })}
                            disabled={!canManage}
                            title={canManage ? undefined : reason}
                            onClick={() => openConfirm({ kind: "delete", user })}
                          >
                            {t("settings.users.delete")}
                          </Button>
                        </span>
                      </div>
                    </details>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </QueryState>

        {!canManage ? (
          <p className="note" style={UNDER_TABLE}>{reason}</p>
        ) : null}
        {setEnabled.error || resendInvite.error || deleteUser.error ? (
          <p role="alert" className="notice red" style={{ margin: 14 }}>
            <Icon name={KIND_ICON.crit} size={16} />
            <span>
              {describeSubmitError(setEnabled.error ?? resendInvite.error ?? deleteUser.error)}
            </span>
          </p>
        ) : null}
        {done ? (
          <p role="status" className="form-saved" style={UNDER_TABLE}>
            <Icon name={KIND_ICON.ok} size={13} /> {t(DONE[done.key], { email: done.email })}
          </p>
        ) : null}
      </Panel>

      {rolesTarget ? (
        <section aria-label={t("settings.users.rolesFor", { email: rolesTarget.email })}>
          <Panel
            title={t("settings.users.rolesFor", { email: rolesTarget.email })}
            subtitle={t("settings.users.rolesSubtitle")}
          >
            {rolesTarget.roles.length === 0 ? (
              <p className="note">
                {t("settings.users.holdsNone", { email: rolesTarget.email })}
              </p>
            ) : (
              <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 6 }}>
                {annotateDuplicateGrants(rolesTarget.roles).map((assignment) => (
                  <li key={assignment.assignment_id} className="flex items-center gap-3">
                    {/* THE SAME `·` SUFFIX THE DIRECTORY CHIP USES, NOT AN
                        EM DASH. The chip a row away from this drawer reads
                        "Technician · everywhere" -- this list read "Technician
                        — everywhere" until the same rewrite that killed the
                        em dash there left it standing here, so a role
                        genuinely read two ways on one tab, one click apart.
                        `describeGrantScope`, `roleDisplayName`: same two
                        transforms, same join, same tab. */}
                    <span>
                      {roleDisplayName(assignment.role_name)}
                      <span className="note">{` · ${describeGrantScope(assignment.client_id, clientNames)}`}</span>
                      {assignment.duplicate ? (
                        <span className="note">
                          {` ${t("settings.users.duplicate", { index: assignment.duplicate.index, total: assignment.duplicate.total })}`}
                        </span>
                      ) : null}
                    </span>
                    <Button
                      type="button"
                      variant="outline"
                      // THE NAME HAS TO NAME THE GRANT, not the role. Three
                      // buttons reading "Remove super-admin from …" are three
                      // controls a screen reader cannot tell apart, and each
                      // one revokes a DIFFERENT row — which is precisely the
                      // confusion the duplicate rows caused visually. The
                      // NAME it reads is the humanized one, same as the
                      // visible text beside it -- a screen reader hearing
                      // "technician" over a screen reading "Technician" is
                      // the exact inconsistency this pass exists to remove.
                      aria-label={
                        assignment.duplicate
                          ? t("settings.users.removeDuplicateAria", {
                              role: roleDisplayName(assignment.role_name),
                              scope: describeGrantScope(assignment.client_id, clientNames),
                              index: assignment.duplicate.index,
                              total: assignment.duplicate.total,
                              email: rolesTarget.email,
                            })
                          : t("settings.users.removeAria", {
                              role: roleDisplayName(assignment.role_name),
                              scope: describeGrantScope(assignment.client_id, clientNames),
                              email: rolesTarget.email,
                            })
                      }
                      disabled={!canManage}
                      title={canManage ? undefined : reason}
                      onClick={() =>
                        openConfirm({ kind: "revoke", user: rolesTarget, assignment })
                      }
                    >
                      {t("settings.users.remove")}
                    </Button>
                  </li>
                ))}
              </ul>
            )}

            <div className="form-grid" style={{ ...FOLDING_FORM_GRID, marginTop: 14 }}>
              <Field label={t("settings.users.role")}>
                <select
                  className="select-field"
                  value={grantRoleId}
                  disabled={!canManage}
                  onChange={(event) => setGrantRoleId(event.target.value)}
                >
                  <option value="">{t("settings.users.selectRole")}</option>
                  {grantable.map((role) => (
                    // Humanized here too (`roleDisplayName`) -- the value
                    // sent on submit is still `role.id`, so the wire is
                    // untouched; only the label a person reads changes.
                    <option key={role.id} value={role.id}>
                      {roleDisplayName(role.name)}
                    </option>
                  ))}
                </select>
              </Field>
              <EntityPicker
                label={t("settings.users.clientLabel")}
                options={clientOptions}
                value={grantClientId}
                onChange={setGrantClientId}
                loading={clients.isLoading}
                emptyHint={clientPickerEmptyHint()}
                placeholder={t("settings.users.clientPlaceholder")}
                disabled={!canManage}
                onPendingChange={setGrantScopeTyping}
              />
            </div>

            <div className="form-actions">
              <Btn
                // THE BUTTON NAMES THE SCOPE IT GRANTS (final review,
                // T11R-I1): "Assign role on Warehouse", "... on every
                // client". The click that sends a grant says where it
                // applies, whatever the box above happens to show.
                label={
                  assignRole.isPending
                    ? t("settings.users.assigning")
                    : grantClientId === EVERY_CLIENT_CHOICE
                      ? t("settings.users.assignEvery")
                      : grantClientId !== ""
                        ? t("settings.users.assignOn", {
                            client: clientNames.get(grantClientId) ?? grantClientId,
                          })
                        : t("settings.users.assign")
                }
                icon="plus"
                variant="primary"
                // Disabled until a scope is CHOSEN, and while the picker
                // holds typed text that is not a choice: "Ware" typed and
                // never clicked is not Warehouse -- and it used to be every
                // client.
                disabled={
                  !canManage ||
                  grantRoleId === "" ||
                  grantClientId === "" ||
                  grantScopeTyping ||
                  assignRole.isPending
                }
                onClick={() =>
                  assignRole.mutate(
                    {
                      userId: rolesTarget.id,
                      body: {
                        role_id: grantRoleId,
                        // `null` for the "Every client" choice -- every
                        // client, including those created later -- else the
                        // chosen client's id. Never `""`, never the
                        // picker's own sentinel.
                        client_id: grantClientIdFor(grantClientId),
                      },
                    },
                    {
                      onSuccess: () => {
                        setGrantRoleId("");
                        setGrantClientId("");
                      },
                    },
                  )
                }
              />
              {!canManage ? <span className="form-hint">{reason}</span> : null}
              {roles.error ? (
                <span className="form-hint">
                  {t("settings.users.catalogueFailed")}
                </span>
              ) : null}
              {assignRole.error ? (
                <span role="alert" className="form-error">
                  <Icon name={KIND_ICON.crit} size={13} />
                  {describeSubmitError(assignRole.error)}
                </span>
              ) : null}
              {removeRole.error ? (
                <span role="alert" className="form-error">
                  <Icon name={KIND_ICON.crit} size={13} />
                  {describeSubmitError(removeRole.error)}
                </span>
              ) : null}
            </div>
          </Panel>
        </section>
      ) : null}

      {permissionsTarget ? (
        <section aria-label={t("settings.users.effectiveFor", { email: permissionsTarget.email })}>
          <Panel
            title={t("settings.users.effectiveFor", { email: permissionsTarget.email })}
            subtitle={t("settings.users.effectiveSubtitle")}
            bare
          >
            <QueryState
              loading={effective.isLoading}
              error={effective.error}
              empty={(effective.data?.codes.length ?? 0) === 0}
              emptyLabel={t("settings.users.effectiveEmpty")}
              emptyIcon="certificate"
            >
              {/* BY CLIENT (MSP clients phase 3, design §4.2): what holds on
                  every client first, then one table per client, by name. */}
              {groupGrantsByClient(effective.data?.codes ?? [], (id) => clientNames.get(id) ?? id).map((group) => {
                const label =
                  group.clientId === null
                    ? t("settings.users.effectiveEveryClient")
                    : (clientNames.get(group.clientId) ?? group.clientId);
                return (
                  <FieldGroup key={group.clientId ?? "every"} title={label} icon={group.clientId === null ? "world" : "building"}>
                    <Table label={label}>
                      <thead>
                        <tr>
                          <Th>{t("settings.users.effectiveColumns.permission")}</Th>
                          <Th>{t("settings.users.effectiveColumns.grantedBy")}</Th>
                        </tr>
                      </thead>
                      <tbody>
                        {group.grants.map((grant) => (
                          <tr key={`${grant.code}:${grant.via_role}`}>
                            <Td>
                              <span className="mono">{grant.code}</span>
                            </Td>
                            <Td>{roleDisplayName(grant.via_role)}</Td>
                          </tr>
                        ))}
                      </tbody>
                    </Table>
                  </FieldGroup>
                );
              })}
            </QueryState>
          </Panel>
        </section>
      ) : null}

      <section aria-label={t("settings.users.invite.title")}>
        <Panel
          title={t("settings.users.invite.title")}
          subtitle={t("settings.users.invite.subtitle")}
        >
          <div className="form-grid" style={FOLDING_FORM_GRID}>
            <Field label={t("settings.users.invite.email")}>
              <input
                className={inputClass}
                type="email"
                value={email}
                disabled={!canManage}
                onChange={(event) => setEmail(event.target.value)}
              />
            </Field>
            <Field label={t("settings.users.invite.displayName")}>
              <input
                className={inputClass}
                value={displayName}
                disabled={!canManage}
                onChange={(event) => setDisplayName(event.target.value)}
              />
            </Field>
          </div>

          <fieldset style={GROUP}>
            <legend style={GROUP_LEGEND}>{t("settings.users.invite.access")}</legend>
            {inviteGrants.length === 0 ? (
              <p className="note">{t("settings.users.invite.noGrants")}</p>
            ) : (
              <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 6 }}>
                {inviteGrants.map((grant, index) => (
                  <li key={`${grant.role_id}:${grant.client_id ?? "every"}`} className="flex items-center gap-3">
                    {/* The roles panel's own `·` suffix and the same two
                        transforms: a pair reads the same before and after it
                        is granted. */}
                    <span>
                      {roleDisplayName(roleNameOf(grant.role_id))}
                      <span className="note">{` · ${describeGrantScope(grant.client_id, clientNames)}`}</span>
                    </span>
                    <Button
                      type="button"
                      variant="outline"
                      aria-label={t("settings.users.invite.removeGrantAria", {
                        role: roleDisplayName(roleNameOf(grant.role_id)),
                        scope: describeGrantScope(grant.client_id, clientNames),
                      })}
                      disabled={!canManage}
                      onClick={() => removeGrant(index)}
                    >
                      {t("settings.users.remove")}
                    </Button>
                  </li>
                ))}
              </ul>
            )}
            {grantable.length === 0 ? (
              <p className="note" style={{ marginTop: 10 }}>{t("settings.users.invite.noRoles")}</p>
            ) : (
              <div className="form-grid" style={{ ...FOLDING_FORM_GRID, marginTop: 10 }}>
                <Field label={t("settings.users.role")}>
                  <select
                    className="select-field"
                    value={draftRoleId}
                    disabled={!canManage}
                    onChange={(event) => setDraftRoleId(event.target.value)}
                  >
                    <option value="">{t("settings.users.selectRole")}</option>
                    {grantable.map((role) => (
                      <option key={role.id} value={role.id}>
                        {roleDisplayName(role.name)}
                      </option>
                    ))}
                  </select>
                </Field>
                <EntityPicker
                  label={t("settings.users.clientLabel")}
                  options={clientOptions}
                  value={draftClientId}
                  onChange={setDraftClientId}
                  loading={clients.isLoading}
                  emptyHint={clientPickerEmptyHint()}
                  placeholder={t("settings.users.clientPlaceholder")}
                  disabled={!canManage}
                  onPendingChange={setDraftTyping}
                />
              </div>
            )}
            <div className="form-actions">
              {/* Disabled until a role and a scope are CHOSEN, and while the
                  picker holds typed text that is not a choice -- the same rule
                  as "Assign role" (final review, T11R-I1). */}
              <Btn
                label={t("settings.users.invite.addGrant")}
                icon="plus"
                disabled={!canManage || draftRoleId === "" || draftClientId === "" || draftTyping}
                onClick={addGrant}
              />
            </div>
          </fieldset>

          <p className="note" style={{ marginTop: 10 }}>
            {t("settings.users.invite.noPassword")}
          </p>

          <div className="form-actions">
            <Btn
              label={invite.isPending ? t("settings.users.invite.sending") : t("settings.users.invite.submit")}
              icon="mail"
              variant="primary"
              disabled={!canManage || email.trim() === "" || draftPending || invite.isPending}
              onClick={sendInvite}
            />
            {!canManage ? <span className="form-hint">{reason}</span> : null}
            {canManage && draftPending ? (
              <span className="form-hint">{t("settings.users.invite.draftPending")}</span>
            ) : null}
            {invite.error ? (
              <span role="alert" className="form-error">
                <Icon name={KIND_ICON.crit} size={13} />
                {describeSubmitError(invite.error)}
              </span>
            ) : null}
          </div>
        </Panel>
      </section>

      <Dialog
        open={confirming != null}
        onClose={() => setConfirming(null)}
        title={confirming ? confirmCopy[confirming.kind].title : ""}
        // The recent confirmation can stand over this one (resetting someone's
        // factors): Escape then answers that dialog only.
        dismissible={!stepUp.open}
      >
        {confirming ? (
          <>
            <p>
              {`${confirmCopy[confirming.kind].question(
                confirming.user.email,
                confirming.kind === "revoke" ? roleDisplayName(confirming.assignment.role_name) : "",
              )} `}
              {confirmCopy[confirming.kind].body}
            </p>
            {/* THE FAILURE IS SAID INSIDE THE DIALOG, which is the fix for a
                destructive action that could fail invisibly. These
                mutations rendered their error into the Panel BEHIND this
                modal: on a 503 the dialog simply stayed open with its button
                still offered, the operator clicked "Delete casey@example.com"
                again, and the only account of what went wrong was underneath
                the overlay. The dialog is what the operator is looking at, so
                the dialog is where the refusal goes — `role="alert"`, so it is
                announced rather than merely appearing. `RolesTab`'s delete
                dialog already did this; this is that rule applied to the
                other destructive paths -- a recent confirmation that was
                closed says so here too. */}
            {confirmMutation[confirming.kind].error ? (
              <p role="alert" className="notice red" style={{ marginTop: 12 }}>
                <Icon name={KIND_ICON.crit} size={16} />
                <span>{describeSubmitError(confirmMutation[confirming.kind].error)}</span>
              </p>
            ) : null}
            <div className="form-actions">
              {/* Disabled while in flight, so a slow delete cannot be fired
                  twice by an operator who thinks the first click missed --
                  and while the recent confirmation waits over it. Every verb
                  here takes something away, so each takes the critical edge. */}
              <Button
                type="button"
                variant={confirmCopy[confirming.kind].destructive ? "outline" : "default"}
                className={confirmCopy[confirming.kind].destructive ? "danger" : undefined}
                disabled={confirmMutation[confirming.kind].isPending || stepUp.open}
                onClick={runConfirmed}
              >
                {confirmCopy[confirming.kind].action(confirming.user.email)}
              </Button>
              <Button type="button" variant="outline" onClick={() => setConfirming(null)}>
                {t("common.actions.cancel")}
              </Button>
            </div>
          </>
        ) : null}
      </Dialog>
      {/* After the dialog it may stand over, and outside every form. */}
      {stepUp.dialog}
    </>
  );
}
