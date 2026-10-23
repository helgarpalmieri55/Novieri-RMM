import { useState } from "react";
import { ConfirmDialog } from "../../components/confirm";
import { RowMenu } from "../../components/rowmenu";
import { Form, FormActions, attemptProblems, describeSubmitError } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Badge, Button, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { ApiError } from "../../lib/api";
import { Panel } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { KIND_ICON } from "../../shell/viz";
import {
  useCloneHealthProfileMutation,
  useCreateHealthProfileMutation,
  useDeleteHealthProfileMutation,
  useHealthAssignments,
  useHealthProfiles,
  useUpdateHealthProfileMutation,
} from "./api";
import { formatSeconds } from "./healthState";
import type { ProfileOut } from "./types";

// Profiles and their assignments (spec §2.1, §2.3, §13).
//
// TWO NUMBERS ARE ON EVERY ROW AND NEITHER IS DECORATION.
//
//   * `check_count` is LIVE checks only. A profile with zero of them alarms on
//     nothing, and an operator must be able to see that from the list rather
//     than by opening it.
//   * `assignment_count` is what makes a profile actually apply to anything. A
//     profile with checks and no assignment is configuration nobody is running,
//     which reads exactly like a configured fleet unless it is shown.
//
// DELETING A PROFILE IS REFUSED, WITH ITS BLOCKERS NAMED, while an assignment
// or an alert still references it (409). The error is rendered verbatim for
// that reason: "in use" alone leaves the operator to find which of a hundred
// devices, and the server's message carries the ids.
//
// THE ROW ACTIONS ARE WHERE THE AUDIT'S BLOCKER LIVED. `describeError` is
// `error.message`, and `ApiError` is built as `new ApiError(status, await
// response.text())`, so a bodyless 503 — what a proxy in front of the control
// plane returns — carries `""`. This block rendered an EMPTY paragraph and the
// operator watched a Delete appear to succeed. `describeSubmitError` is driven
// off `status` first, so a refusal has a sentence whether or not the server
// wrote one, and `role="alert"` means it is announced rather than merely
// coloured red in a corner.

// Auditor: "'Create profile' with an empty name is a silent no-op" and
// "profile names accept arbitrary markup-like strings with no format
// validation". Both are the same failure — the control refused and said
// nothing — so both answer through one channel, `requiredProblems`.
//
// The name is not escaped anywhere on the way to a screen; React does that.
// The rule is here because a profile name is an OPERATOR-FACING IDENTIFIER
// echoed into alert text, ticket subjects and audit entries, and one that
// reads as markup is unusable in all three long before it is dangerous.
//
// A function, so the label and the sentences are worded when the problems are
// shown, in the language then active (see `Attempt`).
const profileRules = (): readonly FieldRule[] => [
  {
    name: "name",
    label: translate("health.profiles.ruleName"),
    validate: (value) => {
      if (/[<>]/.test(value)) return translate("health.profiles.ruleMarkup");
      if (value.length > 120) return translate("health.profiles.ruleLength");
      return null;
    },
  },
];

export function ProfileList({
  canAdmin,
  selectedProfileId,
  onSelect,
}: {
  canAdmin: boolean;
  selectedProfileId?: string;
  onSelect: (profileId: string) => void;
}) {
  const t = useT();
  const profiles = useHealthProfiles();
  const assignments = useHealthAssignments();
  const create = useCreateHealthProfileMutation();
  const update = useUpdateHealthProfileMutation();
  const remove = useDeleteHealthProfileMutation();
  const clone = useCloneHealthProfileMutation();
  const [name, setName] = useState("");
  // The profile a Delete click has NAMED but not yet destroyed (UX remediation
  // cycle 8). Auditor A, HIGH: "Delete destroys a record instantly with no
  // confirmation, and is styled identically to the non-destructive actions
  // beside it" -- `btn ghost`, 68px from Clone. Holding the row here is what
  // turns one click into a decision about a named profile.
  const [pendingDelete, setPendingDelete] = useState<ProfileOut | null>(null);
  // Populated ON SUBMIT, never while typing: a message that appears before the
  // user has finished their first word is the other way to be unhelpful.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  const forbidden = profiles.error instanceof ApiError && profiles.error.status === 403;
  // `profiles.data ?? []` renders "No health profiles. Nothing in this
  // organisation is being checked." — a claim about the fleet. A 500 made that
  // claim on no evidence at all, which is the same swallowed-failure class as
  // the audit's BLOCKER and lands on a sentence an operator would act on.
  const loadFailed = Boolean(profiles.error) && !forbidden;
  const rows = profiles.data ?? [];
  // The assignment breakdown is a SECOND query, and it fails separately. When
  // it does, `describeAssignments` over an empty list returns "unassigned" for
  // a profile whose own `assignment_count` says otherwise — the row
  // contradicting itself. Say the count and admit the breakdown is missing.
  const assignmentsFailed = Boolean(assignments.error);

  // Delete, retire and clone are row actions rather than forms, so they cannot
  // go through `FormActions`; what they take from it is the sentence and the
  // announcement. Labelled, because a bare red line under a table of twelve
  // rows does not say which button produced it.
  const rowActions: ReadonlyArray<{ label: string; mutation: { error: unknown } }> = [
    { label: t("health.profiles.failedRetire"), mutation: update },
    { label: t("health.profiles.failedClone"), mutation: clone },
    { label: t("health.profiles.failedDelete"), mutation: remove },
  ];

  function submitCreate() {
    const next: Attempt = { rules: profileRules, values: { name } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    // Cleared only on the server's 2xx. Clearing it beside `mutate` threw away
    // what the operator typed the moment the request failed, leaving them
    // nothing to correct.
    create.mutate({ name: name.trim() }, { onSuccess: () => setName("") });
  }

  return (
    <Panel
      title={t("health.profiles.title")}
      subtitle={t("health.profiles.subtitle")}
    >
      {forbidden ? (
        <p role="alert" className="note">
          {t("health.profiles.forbidden")}
        </p>
      ) : loadFailed ? (
        <div role="alert" className="notice red">
          <Icon name={KIND_ICON.crit} size={17} />
          <div>{t("health.profiles.loadFailed", { detail: describeSubmitError(profiles.error) })}</div>
        </div>
      ) : (
        <>
          {rows.length === 0 ? (
            <p className="note mb-3">{t("health.profiles.empty")}</p>
          ) : (
            <Table>
              <thead>
                <tr>
                  <Th>{t("health.profiles.colName")}</Th>
                  <Th>{t("health.profiles.colCheckedEvery")}</Th>
                  <Th>{t("health.profiles.colChecks")}</Th>
                  <Th>{t("health.profiles.colAppliedTo")}</Th>
                  <Th>{t("health.profiles.colVersion")}</Th>
                  <Th>{t("health.profiles.colActive")}</Th>
                  <Th> </Th>
                </tr>
              </thead>
              <tbody>
                {rows.map((profile) => (
                  <tr
                    key={profile.id}
                    aria-current={profile.id === selectedProfileId ? "true" : undefined}
                  >
                    {/* A MEASURE, NOT A PERCENTAGE OF WHATEVER IS LEFT. Seven
                        columns in a 350px scroller with nothing to stop them
                        collapsed this cell to one or two words a line: a
                        six-word description became a seven-line ribbon at
                        390px. The floor makes the TABLE outgrow the scroller
                        instead, which is what `.table-wrap`'s horizontal
                        scroll (and its tab stop) is for. */}
                    <Td lead={<Icon name="adjustments" size={18} />}>
                      <div className="cell-title" style={{ minWidth: "18ch" }}>
                        {profile.name}
                      </div>
                      {profile.description ? (
                        <div className="cell-sub">{profile.description}</div>
                      ) : null}
                    </Td>
                    <Td>
                      {/* TWO CADENCES, TWO LINES, AND NEITHER WRAPS. One
                          nowrap line demanded ~190px of the table's intrinsic
                          width and helped push the actions column out of the
                          panel; stacked, each reads in half that and "5 / min
                          / tick" broken over three lines still cannot happen. */}
                      <div className="mono" style={{ whiteSpace: "nowrap" }}>
                        {t("health.profiles.live", { every: formatSeconds(profile.tick_interval_s) })}
                      </div>
                      <div className="cell-sub mono" style={{ whiteSpace: "nowrap" }}>
                        {t("health.profiles.configuration", { every: formatSeconds(profile.posture_interval_s) })}
                      </div>
                    </Td>
                    {/* THE WARNING IS THE BADGE; THE REASON IS THE LINE
                        UNDER IT. Both of these were one long badge ("no checks
                        — alarms on nothing", "unassigned — applies to
                        nothing"), and two ~200px pills in a seven-column table
                        are what pushed the row actions outside the panel at
                        1440px, where nothing but a sideways scroll of the table
                        could reach them. Nothing is dropped: the same sentence
                        is said, one line lower, and it no longer sets the
                        column's width. (The em dashes go with them, which the
                        port's copy rule asks for anyway.) */}
                    <Td>
                      <Badge variant={profile.check_count === 0 ? "warning" : "muted"}>
                        {profile.check_count === 0 ? t("health.profiles.noChecks") : String(profile.check_count)}
                      </Badge>
                      {profile.check_count === 0 && (
                        <div className="cell-sub">{t("health.profiles.alarmsOnNothing")}</div>
                      )}
                    </Td>
                    <Td>
                      <Badge variant={profile.assignment_count === 0 ? "warning" : "muted"}>
                        {profile.assignment_count === 0
                          ? t("health.profiles.unassigned")
                          : assignmentsFailed
                            ? t("health.profiles.assignments", { count: profile.assignment_count })
                            : describeAssignments(profile, assignments.data ?? [])}
                      </Badge>
                      {profile.assignment_count === 0 ? (
                        <div className="cell-sub">{t("health.profiles.appliesToNothing")}</div>
                      ) : assignmentsFailed ? (
                        <div className="cell-sub">{t("health.profiles.breakdownUnavailable")}</div>
                      ) : null}
                    </Td>
                    <Td>
                      <span className="mono">v{profile.policy_version}</span>
                    </Td>
                    <Td>
                      {profile.active ? (
                        <Badge variant="success">{t("health.profiles.active")}</Badge>
                      ) : (
                        <Badge variant="muted">{t("health.profiles.retired")}</Badge>
                      )}
                    </Td>
                    <Td>
                      {/* NOWRAP, AND THAT IS THE POINT. Wrapped, these four
                          controls stacked into a ~165px row with ~90px of
                          white under the content beside them, and the column
                          they sit in still ended up off the panel. On one line
                          the column declares the width it actually needs, the
                          table fits inside the panel at 1440px, and the row is
                          the height of its text. */}
                      <div className="flex flex-nowrap items-center gap-1">
                        <Button variant="outline" onClick={() => onSelect(profile.id)}>
                          {t("health.profiles.checks")}
                        </Button>
                        {canAdmin && (
                          <>
                            <Button
                              variant="outline"
                              onClick={() =>
                                update.mutate({
                                  id: profile.id,
                                  body: { active: !profile.active },
                                })
                              }
                            >
                              {profile.active ? t("health.profiles.retire") : t("health.profiles.reactivate")}
                            </Button>
                            {/* Clone and Delete live in the row menu, apart
                                from each other, and Delete is the menu's red:
                                a dialog alone would still let the misclick
                                happen and then ask about it. */}
                            <RowMenu
                              subject={profile.name}
                              items={[
                                {
                                  label: t("health.profiles.clone"),
                                  onSelect: () =>
                                    clone.mutate({
                                      id: profile.id,
                                      name: t("health.profiles.cloneName", { name: profile.name }),
                                    }),
                                },
                                {
                                  label: t("health.profiles.delete"),
                                  destructive: true,
                                  onSelect: () => {
                                    // A previous refusal would read as a refusal of THIS delete.
                                    remove.reset();
                                    setPendingDelete(profile);
                                  },
                                },
                              ]}
                            />
                          </>
                        )}
                      </div>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}

          {canAdmin && (
            <Form className="mt-4" onSubmit={submitCreate}>
              <div className="flex flex-wrap items-end gap-2">
                <Field label={t("health.profiles.fieldNew")}>
                  <input
                    className={inputClass}
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                  />
                </Field>
                {/* Enabled with an empty name ON PURPOSE. The disabled button
                    was the silent no-op: pressing it moved nothing and named
                    no field. Now the press produces the reason.

                    "Create", not "Create profile": `FormActions` builds its
                    pending text as `label.replace(/e$/, "") + "ing…"`, which
                    only reads as English for a single verb. */}
                <FormActions
                  label={t("health.profiles.create")}
                  pending={create.isPending}
                  error={create.error}
                  saved={create.isSuccess}
                  problems={attemptProblems(attempt)}
                  savedMessage={t("health.profiles.created")}
                />
              </div>
            </Form>
          )}

          {/* NOT `requireTyping`. A health profile is recoverable by rebuilding
              it and the blast radius is one org's monitoring definition, not a
              wiped endpoint; typing the name here would be friction that
              teaches people to copy words without reading them. What the
              dialog owes is the CONSEQUENCE -- devices stop being checked --
              and the profile's own name, so a wrong row is visible. */}
          <ConfirmDialog
            open={pendingDelete !== null}
            title={t("health.profiles.deleteTitle")}
            subject={pendingDelete?.name ?? ""}
            body={
              pendingDelete
                ? t("health.profiles.deleteBody", { impact: describeDeleteImpact(pendingDelete) })
                : ""
            }
            confirmLabel={t("health.profiles.deleteLabel")}
            destructive
            onCancel={() => setPendingDelete(null)}
            onConfirm={() => {
              const profile = pendingDelete;
              setPendingDelete(null);
              if (profile) remove.mutate(profile.id);
            }}
          />

          {rowActions.map(({ label, mutation }) =>
            mutation.error ? (
              <div key={label} role="alert" className="notice red mt-2">
                <Icon name={KIND_ICON.crit} size={17} />
                <div>
                  {t("health.profiles.mutationFailed", { label, detail: describeSubmitError(mutation.error) })}
                </div>
              </div>
            ) : null,
          )}
        </>
      )}
    </Panel>
  );
}

/**
 * What deleting this profile costs, in the numbers the row already carries.
 *
 * A profile assigned to nothing is a definition nobody is running; one
 * assigned to the org is what every unclaimed device in the fleet is checked
 * by. "Are you sure?" cannot tell those apart, and they are not the same
 * decision.
 */
export function describeDeleteImpact(profile: Pick<ProfileOut, "assignment_count" | "check_count">): string {
  if (profile.assignment_count === 0) {
    return translate("health.profiles.impactNone", { count: profile.check_count });
  }
  return translate("health.profiles.impactSome", {
    assignments: profile.assignment_count,
    checks: profile.check_count,
    count: profile.assignment_count,
  });
}

/**
 * "org default", "2 groups", "3 devices" — the resolution levels this profile
 * is attached at (spec §0.2), rather than a bare count.
 *
 * The level comes from the SERVER (`AssignmentOut.level`); this only groups.
 */
export function describeAssignments(
  profile: Pick<ProfileOut, "id">,
  assignments: ReadonlyArray<{ profile_id: string; level: string }>,
): string {
  const mine = assignments.filter((a) => a.profile_id === profile.id);
  if (mine.length === 0) return translate("health.profiles.unassigned");
  let device = 0;
  let group = 0;
  let org = 0;
  for (const assignment of mine) {
    if (assignment.level === "device") device += 1;
    else if (assignment.level === "group") group += 1;
    else org += 1;
  }
  const parts: string[] = [];
  if (org > 0) parts.push(translate("health.profiles.orgDefault"));
  if (group > 0) parts.push(translate("health.profiles.groups", { count: group }));
  if (device > 0) parts.push(translate("health.profiles.devices", { count: device }));
  return parts.join(", ");
}
